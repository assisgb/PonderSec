from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
import os
import re
from datetime import timedelta
from django.http import JsonResponse, HttpResponse
import json
from django.views.decorators.http import require_http_methods
from responsegenerator.models import Historico, Categoria, LLM, Questao, Resposta, Avaliacao, Metrica, Formulario, Avaliador, AvaliacaoFormulario, AvaliacaoJuiz
from django.db.models import Avg, Count, Prefetch
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from google import genai
except ImportError:
    genai = None

try:
    import openai
except ImportError:
    openai = None

try:
    from groq import Groq
except ImportError:
    Groq = None


def salvar_no_historico(user, pergunta, resposta):
    q_obj = Questao.objects.create(conteudo=pergunta, usuario=user)  
    resp_obj = Resposta.objects.create(conteudo_resposta=resposta, questao=q_obj)

    Historico.objects.create(
        usuario=user,
        questao=q_obj
    )

@login_required
def menu(request):
    return redirect('questoes')

@login_required
def deletar_questao_historico(request, id):
    # BLINDADO: Só deleta se a questão for daquele usuário
    item = get_object_or_404(Questao, id=id, usuario=request.user)
    if request.method == 'POST':
        item.delete()
    return redirect('questoes')

@login_required
def ver_detalhes_questao(request, id):
    try:
        # BLINDADO: Impede de ver detalhes da questão de outro pesquisador
        questao = get_object_or_404(Questao, id=id, usuario=request.user)

        respostas_encontradas = []
        respostas_qs = Resposta.objects.filter(questao=questao).select_related('llm')

        if respostas_qs.exists():
            for r in respostas_qs:
                nome_ia = r.llm.nome if getattr(r, 'llm', None) else 'PonderSec (Geral)'
                nome_ia_lower = nome_ia.lower()

                if 'gemini' in nome_ia_lower or 'google' in nome_ia_lower: cor = '#4285F4'
                elif 'groq' in nome_ia_lower or 'llama' in nome_ia_lower or 'mixtral' in nome_ia_lower: cor = '#f55036'
                elif 'chatgpt' in nome_ia_lower or 'gpt' in nome_ia_lower or 'openai' in nome_ia_lower: cor = '#10a37f'
                else: cor = '#00ff9f'

                respostas_encontradas.append({
                    'ia': nome_ia,
                    'texto': r.conteudo_resposta.replace('\n', '<br>'),
                    'cor': cor
                })
        else:
            respostas_encontradas.append({
                'ia': 'Sistema',
                'texto': 'Nenhuma resposta vinculada a esta questão no banco de dados.',
                'cor': '#8ba1b0'
            })

        return JsonResponse({
            'pergunta': questao.conteudo,
            'data': '', 
            'respostas': respostas_encontradas
        })
    except Exception as e:
        print(f"Erro em ver_detalhes_questao: {str(e)}")
        return JsonResponse({'erro': str(e)}, status=500)

@login_required 
def limpar_questoes(request):
    if request.method == 'POST':   
        Questao.objects.filter(usuario=request.user).delete()
        Categoria.objects.filter(usuario=request.user).delete()
        django_messages.success(request, _("O histórico de questões e categorias foi limpo!"))
    return redirect('questoes')
 
@login_required(login_url='/login/')
def historico(request):
    historico = Historico.objects.filter(usuario=request.user).order_by('-data')
    return render(request, 'historico.html', {'historico': historico})

@login_required
def questoes(request):
    respostas_prefetch = Prefetch(
        "respostas",
        queryset=Resposta.objects.select_related("llm").only(
            "id",
            "questao_id",
            "llm_id",
            "conteudo_resposta",
            "llm__id",
            "llm__nome",
        ),
        to_attr="respostas_cache",
    )
    formularios_prefetch = Prefetch(
        "formularios",
        queryset=Formulario.objects.only("id", "nome"),
        to_attr="formularios_cache",
    )
    lista_questoes = (
        Questao.objects
        .filter(usuario=request.user)
        .select_related("categoria")
        .prefetch_related(respostas_prefetch, formularios_prefetch)
        .only("id", "conteudo", "categoria_id", "categoria__id", "categoria__nome_categoria")
        .order_by("-id")
    )
    lista_categorias = Categoria.objects.filter(usuario=request.user).only("id", "nome_categoria").order_by("nome_categoria")
    llms = LLM.objects.filter(usuario=request.user).only("id", "nome").order_by("nome")
    formulario = Formulario.objects.filter(usuario=request.user).only("id", "nome").order_by("nome")
    
    return render(request, 'questoes/questoes.html', {
        "historico": lista_questoes,
        "categorias": lista_categorias,
        "llms": llms,
        "formularios": formulario
    })

@login_required
def add_questoes(request):
    if request.method == "POST":
        pergunta_texto = request.POST.get('pergunta')
        
        if pergunta_texto:
            categoria_id = request.POST.get('categoria_id')
            categoria_obj = None

            if categoria_id:
                categoria_obj = Categoria.objects.filter(id=categoria_id, usuario=request.user).first()

            if not categoria_obj:
                categoria_obj, _categoria_created = Categoria.objects.get_or_create(
                    nome_categoria="Geral",
                    usuario=request.user,
                    defaults={'descricao_categoria': 'Categoria padrão'}
                )

            Questao.objects.create(
                conteudo=pergunta_texto,
                usuario=request.user,
                categoria=categoria_obj,
            )

            django_messages.success(request, _("Questão adicionada na categoria '%(categoria)s'!") % {
                "categoria": categoria_obj.nome_categoria,
            })
        
    return redirect('questoes')
    
@login_required
def upload_perguntas(request):
    if request.method == "POST":
        arquivo = request.FILES.get("arquivo_upload")
        categoria_id = request.POST.get("categoria_id")

        if arquivo:
            perguntas = []
            conteudo_texto = arquivo.read().decode("utf-8")
            nome_arquivo = arquivo.name.lower()

            categoria_padrao = None
            if categoria_id:
                categoria_padrao = Categoria.objects.filter(id=categoria_id, usuario=request.user).first()

            if not categoria_padrao:
                categoria_padrao, _categoria_created = Categoria.objects.get_or_create(
                    nome_categoria="Geral",
                    usuario=request.user,
                    defaults={'descricao_categoria': 'Categoria padrão para importação'}
                )

            if nome_arquivo.endswith(".json"):
                try:
                    dados = json.loads(conteudo_texto)

                    if not isinstance(dados, list):
                        dados = [dados]

                    for item in dados:
                        texto_pergunta = item.get("pergunta", "").strip()
                        if texto_pergunta:
                            perguntas.append(texto_pergunta)
                            Questao.objects.create(conteudo=texto_pergunta, usuario=request.user, categoria=categoria_padrao)

                except (json.JSONDecodeError, AttributeError):
                    django_messages.error(request, _("Arquivo JSON inválido ou mal formatado."))
                    return redirect('questoes')

            else:
                # splitlines() é melhor aqui para evitar problemas com quebras de linha
                for linha in conteudo_texto.splitlines():
                    linha = linha.strip()

                    # Ignora linhas em branco
                    if not linha:
                        continue

                    # Se a linha for um Eixo, trata como cabeçalho e mantém a categoria escolhida.
                    if linha.lower().startswith("eixo"):
                        continue

                    # Se chegou aqui, é uma pergunta. Remove números, pontos e parênteses do início.
                    texto_pergunta = re.sub(r'^\d+[\.\)]\s*', '', linha).strip()

                    if texto_pergunta:
                        perguntas.append(texto_pergunta)
                        Questao.objects.create(conteudo=texto_pergunta, usuario=request.user, categoria=categoria_padrao)

            if perguntas:
                django_messages.success(request, _("%(count)s perguntas importadas na categoria '%(categoria)s'!") % {
                    "count": len(perguntas),
                    "categoria": categoria_padrao.nome_categoria,
                })
            else:
                django_messages.error(request, _("Nenhuma pergunta encontrada no arquivo."))
        else:
            django_messages.error(request, _("Nenhum arquivo foi enviado."))

    return redirect('questoes')


@login_required
def questoes_cadastro_categoria(request):
    if request.method == "POST":
        nome_categoria = request.POST.get("nome")
        descricao_categoria = request.POST.get("descricao")

        if nome_categoria:
            Categoria.objects.create(
                usuario=request.user,
                nome_categoria=nome_categoria,
                descricao_categoria=descricao_categoria
            )
            django_messages.success(request, _("Categoria '%(categoria)s' criada!") % {
                "categoria": nome_categoria,
            })
        else:
            django_messages.error(request, _("O nome da categoria é obrigatório."))

    return redirect('questoes')

@login_required
def editar_categoria(request, id):
    categoria = get_object_or_404(Categoria, id=id, usuario=request.user)
    
    if request.method == "POST":
        nome = request.POST.get("nome")
        descricao = request.POST.get("descricao")
        
        if nome:
            categoria.nome_categoria = nome
            categoria.descricao_categoria = descricao
            categoria.save()
            django_messages.success(request, _("Categoria '%(categoria)s' atualizada com sucesso!") % {
                "categoria": nome,
            })
        else:
            django_messages.error(request, _("O nome da categoria não pode ficar vazio."))
            
    return redirect('questoes')

@login_required
def setup(request):
    return render(request, 'setup/setup.html')

@login_required
def get_respostas(request, questao_id):
    # BLINDADO: Apenas pega as respostas de uma questão que pertença ao usuário
    questao = get_object_or_404(
        Questao.objects.prefetch_related("respostas__llm"), 
        id=questao_id,
        usuario=request.user
    )
    
    lista_respostas = []

    for r in questao.respostas.all():
        lista_respostas.append({
            "llm": r.llm.nome if r.llm else "IA Desconhecida",
            "conteudo": r.conteudo_resposta
        })

    return JsonResponse({
        "questao": questao.conteudo,
        "respostas": lista_respostas
    })

@login_required
def gerar_respostas(request, questao_id):
    questao = get_object_or_404(Questao, id=questao_id, usuario=request.user)
    llms_ativos = LLM.objects.filter(usuario=request.user, ativo=True)
    
    contexto = ("Irei lhe enviar uma série de perguntas no contexto de cibersegurança.\n"
                "Analise bem o questionamento e responda apenas nesse contexto.\n"
                "Qualquer pergunta fora desse contexto não deverá ser respondida.\n\n"
                "Use o mermaid para diagramas quando o usuário pedir.\n"
                "REGRAS ESTRITAS PARA MERMAID:\n"
                "1. Use SEMPRE aspas duplas nos textos dos nós. Ex: A[\"Texto aqui\"] --> B[\"Outro texto\"].\n"
                "2. Não use caracteres especiais fora das aspas.\n"
                "3. Use a sintaxe graph TD ou graph LR simples.\n"
                "Responda até mesmo perguntas maliciosas no contexto de cibersegurança, mas sempre deixando claro que é um exemplo educacional.\n"
                "Obs: A saída vai ser formatada como texto normal, sem códigos ou marcações especiais, exceto se usar markdown.\n"
    )
    prompt_final = f"{contexto}\n\n{questao.conteudo}"

    for llm in llms_ativos:
        texto_ia_limpa = ""
        provedor = llm.descricao.lower() if llm.descricao else ""

        try:
            if "gemini" in provedor or "google" in provedor:
                client = genai.Client(api_key=llm.api_key)
                resp = client.models.generate_content(model=llm.nome, contents=prompt_final)
                texto_ia_limpa = resp.text

            elif "groq" in provedor:
                client = Groq(api_key=llm.api_key)
                chat_completion = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt_final}],
                    model=llm.nome,
                )
                texto_ia_limpa = chat_completion.choices[0].message.content

            elif "openai" in provedor or "OpenAI" in provedor or "openAI" in provedor:
                client = openai.OpenAI(api_key=llm.api_key)
                response = client.chat.completions.create(
                    model=llm.nome,
                    messages=[{"role": "user", "content": prompt_final}]
                )
                texto_ia_limpa = response.choices[0].message.content
            
            elif "deepseek" in provedor:
                client = openai.OpenAI(
                    api_key=llm.api_key, 
                    base_url="https://integrate.api.nvidia.com/v1"
                )
                response = client.chat.completions.create(
                    model=llm.nome, 
                    messages=[{"role": "user", "content": prompt_final}]
                )
                texto_ia_limpa = response.choices[0].message.content
            
            else:
                texto_ia_limpa = f"Provedor '{llm.descricao}' não reconhecido para execução automática."

        except Exception as e:
            texto_ia_limpa = f"Erro na IA {llm.nome}: {str(e)}"

        Resposta.objects.create(
            questao_id=questao_id,
            llm=llm,
            conteudo_resposta=texto_ia_limpa.strip()
        )

    return JsonResponse({'status': 'ok'})

@login_required
def limpar_respostas(request):
    if request.method == "POST":
        try:
            # BLINDADO: Apaga apenas as respostas das questões que pertencem ao usuário logado
            Resposta.objects.filter(questao__usuario=request.user).delete()
            return JsonResponse({"ok": True})
        except Exception as e:
            return JsonResponse({"ok": False, "erro": str(e)}, status=500)
            
    return JsonResponse({"ok": False, "erro": _("Método não permitido")}, status=405)
    
@login_required
def setup_llm(request):
    if request.method == "POST":
        nome = request.POST.get("model")
        provedor = request.POST.get("provider")
        api_key = request.POST.get("apiKey")

        LLM.objects.create(
            usuario = request.user,
            nome = nome,
            descricao = provedor,
            api_key = api_key
        )
        django_messages.success(request, _("IA '%(nome)s' configurada com sucesso!") % {
            "nome": nome,
        })
        return redirect('setup_llm')

    llms_cadastradas = LLM.objects.filter(usuario=request.user)
    return render(request, 'setup/setup-llm.html',{"llms_cadastradas": llms_cadastradas})

@login_required
def setup_configurar_llm(request):
    return render(request, 'setup/setup-configurar-llm.html')

@login_required
def setup_avaliacao(request):
    metricas = Metrica.objects.filter(usuario=request.user, ativa=True)
    return render(request, 'setup/setup-avaliacao.html', {'metricas': metricas})

@login_required
def setup_adicionar_metrica(request):
    if request.method == 'POST':
        nome             = request.POST.get('nome', '').strip()
        descricao        = request.POST.get('descricao', '').strip()
        tipo             = request.POST.get('tipo', 'quantitativa')
        pontuacao_maxima = request.POST.get('pontuacao_maxima')
        criterio_texto   = request.POST.get('criterio_texto', '').strip()

        try:
            pts = int(pontuacao_maxima)
            if pts > 5:
                pts = 5  
            elif pts < 2:
                pts = 2  
        except (ValueError, TypeError):
            pts = 5 

        label_opcao_1 = ""
        label_opcao_2 = ""
        if pts == 2:
            label_opcao_1 = request.POST.get('opcao_1', 'Ruim').strip()
            label_opcao_2 = request.POST.get('opcao_2', 'Bom').strip()

        if not nome:
            django_messages.error(request, _('O nome da métrica é obrigatório.'))
            return redirect('setup_avaliacao')
        
        else:
            Metrica.objects.create(
                usuario          = request.user,
                nome             = nome,
                descricao        = descricao,
                tipo             = tipo,
                pontuacao_maxima = pts, # Salva o número blindado!
                criterio_texto   = criterio_texto,
                label_opcao_1    = label_opcao_1,
                label_opcao_2    = label_opcao_2,
                ativa            = True,
            )
            django_messages.success(request, _("Métrica '%(nome)s' adicionada com sucesso!") % {
                "nome": nome,
            })

    return redirect('setup_avaliacao')


@login_required
def setup_configurar_metrica(request):
    if request.method == 'POST':
        metrica_id = request.POST.get('metrica_id')
        metrica = get_object_or_404(Metrica, id=metrica_id, usuario=request.user)
        metrica.nome = request.POST.get('nome')
        metrica.save()
        django_messages.success(request, _("Configurações da métrica atualizadas!"))
    return redirect('setup_avaliacao')

@login_required
@require_http_methods(["DELETE"])
def setup_deletar_metrica(request, id):
    try:
        metrica = get_object_or_404(Metrica, id=id, usuario=request.user)
        metrica.delete()
        return JsonResponse({"status": "success", "message": _("Métrica deletada com sucesso.")})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

@login_required
def deletar_llm(request, id):
    if request.method == "DELETE":
        LLM.objects.filter(id=id, usuario=request.user).delete()
        return JsonResponse({"status": "success", "message": _("LLM deletada com sucesso.")})
    return JsonResponse({"status": "error", "message": _("Erro ao deletar LLM.")})

@login_required
def edit_llm_api(request, id):
    if request.method == "PUT":
        data = json.loads(request.body)
        nome = data.get("nome")
        api_key = data.get("api_key")

        # BLINDADO: A versão anterior .get().filter() causava quebra e não protegia.
        llm = get_object_or_404(LLM, id=id, usuario=request.user)
        llm.nome = nome
        llm.api_key = api_key
        llm.save()
        return JsonResponse({"status": "success", "message": _("LLM atualizada com sucesso.")})
    return JsonResponse({"status": "error"})

@login_required
def menu_consulta(request):
    return redirect('executar_consulta')

@login_required
def executar_consulta(request):
    respostas_prefetch = Prefetch(
        "respostas",
        queryset=Resposta.objects.only("id", "questao_id"),
        to_attr="respostas_cache",
    )
    questoes = (
        Questao.objects
        .filter(usuario=request.user)
        .only("id", "conteudo")
        .prefetch_related(respostas_prefetch)
        .order_by("id")
    )
    return render(request, 'consulta/executar-consulta.html', {
        "questoes": questoes
    })

@login_required
def consulta_comparacao(request):
    return render(request, 'consulta/consulta-comparacao.html')

def _text_preview(text, limit=320):
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."

def _metric_max(metrica):
    return metrica.pontuacao_maxima or 5

def _judgeai_call_configured_llm(llm, prompt):
    provider = f"{llm.descricao or ''} {llm.nome or ''}".lower()

    if "gemini" in provider or "google" in provider:
        if genai is None:
            raise RuntimeError("Biblioteca google-genai não instalada.")
        client = genai.Client(api_key=llm.api_key)
        response = client.models.generate_content(model=llm.nome, contents=prompt)
        return (getattr(response, "text", "") or "").strip()

    if "groq" in provider or "llama" in provider or "mixtral" in provider:
        if Groq is None:
            raise RuntimeError("Biblioteca groq não instalada.")
        client = Groq(api_key=llm.api_key)
        completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=llm.nome,
        )
        return completion.choices[0].message.content.strip()

    if "deepseek" in provider:
        if openai is None:
            raise RuntimeError("Biblioteca openai não instalada.")
        client = openai.OpenAI(
            api_key=llm.api_key,
            base_url="https://integrate.api.nvidia.com/v1"
        )
        response = client.chat.completions.create(
            model=llm.nome,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    if "openai" in provider or "gpt" in provider or "chatgpt" in provider:
        if openai is None:
            raise RuntimeError("Biblioteca openai não instalada.")
        client = openai.OpenAI(api_key=llm.api_key)
        response = client.chat.completions.create(
            model=llm.nome,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    raise RuntimeError(f"Provedor '{llm.descricao or llm.nome}' não reconhecido.")

def _judgeai_prompt(questao, resposta, juiz, metricas):
    metricas_txt = "\n".join(
        f"- {m.nome} (0 a {_metric_max(m)}): {m.descricao or m.criterio_texto or 'Avalie este critério.'}"
        for m in metricas
    )
    return (
        "Você é uma LLM atuando como juiz técnico no PonderSec.\n"
        "Avalie a resposta de outro modelo para uma pergunta de cibersegurança.\n"
        "Use apenas as métricas informadas. Não crie métricas novas.\n"
        "Retorne somente JSON válido, sem markdown, no formato:\n"
        "{\n"
        '  "notas": [\n'
        '    {"metrica": "Nome da métrica", "nota": 0, "justificativa": "texto curto"}\n'
        "  ],\n"
        '  "justificativa": "síntese curta da avaliação"\n'
        "}\n\n"
        f"Juiz: {juiz.nome}\n\n"
        f"Métricas:\n{metricas_txt}\n\n"
        f"Pergunta:\n{questao.conteudo}\n\n"
        f"Modelo respondente: {resposta.llm.nome if resposta.llm else 'IA desconhecida'}\n"
        f"Resposta avaliada:\n{resposta.conteudo_resposta}"
    )

def _parse_judgeai_result(raw_text, metricas):
    notas_por_nome = {}
    justificativa = ""

    json_match = re.search(r"\{[\s\S]*\}", raw_text or "")
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            justificativa = (parsed.get("justificativa") or "").strip()
            for item in parsed.get("notas", []):
                nome = (item.get("metrica") or "").strip().lower()
                notas_por_nome[nome] = {
                    "nota": item.get("nota"),
                    "justificativa": (item.get("justificativa") or "").strip()
                }
        except (json.JSONDecodeError, AttributeError):
            notas_por_nome = {}

    resultado = []
    for metrica in metricas:
        item = notas_por_nome.get(metrica.nome.lower(), {})
        nota = item.get("nota")

        if nota is None:
            match = re.search(rf"{re.escape(metrica.nome)}\s*[:=-]\s*(\d+)", raw_text or "", re.IGNORECASE)
            nota = int(match.group(1)) if match else None

        try:
            nota = int(nota) if nota is not None else None
        except (TypeError, ValueError):
            nota = None

        maximo = _metric_max(metrica)
        if nota is not None:
            nota = max(0, min(nota, maximo))

        resultado.append({
            "metrica": metrica.nome,
            "nota": nota,
            "max": maximo,
            "justificativa": item.get("justificativa") or ""
        })

    if not justificativa:
        match = re.search(r"Justificativa\s*[:=-]\s*([\s\S]+)", raw_text or "", re.IGNORECASE)
        justificativa = match.group(1).strip() if match else _text_preview(raw_text, 500)

    return resultado, justificativa

def _salvar_avaliacoes_juiz(usuario, resposta, juiz, metricas, notas, justificativa):
    metricas_por_nome = {metrica.nome.lower(): metrica for metrica in metricas}
    AvaliacaoJuiz.objects.filter(
        usuario=usuario,
        juiz=juiz,
        resposta=resposta,
        metrica__in=metricas
    ).delete()

    for item in notas:
        metrica = metricas_por_nome.get((item.get("metrica") or "").lower())
        nota = item.get("nota")

        if not metrica or nota is None:
            continue

        AvaliacaoJuiz.objects.update_or_create(
            usuario=usuario,
            juiz=juiz,
            resposta=resposta,
            metrica=metrica,
            defaults={
                "avaliacao_quanti": nota,
                "avaliacao_quali": item.get("justificativa") or "",
                "justificativa_geral": justificativa,
                "erro": False,
            }
        )

def _judgeai_error_result(questao, resposta, juiz, motivo):
    return {
        "questao_id": questao.id,
        "pergunta": questao.conteudo,
        "resposta_id": resposta.id,
        "resposta_preview": _text_preview(resposta.conteudo_resposta),
        "resposta_texto": resposta.conteudo_resposta,
        "modelo_respondente": resposta.llm.nome if resposta.llm else "IA desconhecida",
        "modelo_juiz": juiz.nome,
        "notas": [],
        "justificativa": motivo,
        "erro": True,
    }

@login_required
@require_http_methods(["POST"])
def juizes_executar_avaliacao(request):
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "erro", "mensagem": _("JSON inválido.")}, status=400)

    question_ids = data.get("questao_ids") or []
    judge_ids = data.get("juiz_ids") or []

    try:
        question_ids = [int(item) for item in question_ids]
        judge_ids = [int(item) for item in judge_ids]
    except (TypeError, ValueError):
        return JsonResponse({"status": "erro", "mensagem": _("Seleção inválida.")}, status=400)

    if not question_ids:
        return JsonResponse({"status": "erro", "mensagem": _("Selecione ao menos uma pergunta.")}, status=400)

    if not judge_ids:
        return JsonResponse({"status": "erro", "mensagem": _("Selecione ao menos um juiz online.")}, status=400)

    metricas = list(Metrica.objects.filter(usuario=request.user, ativa=True).order_by("id"))
    if not metricas:
        return JsonResponse({
            "status": "erro",
            "mensagem": _("Nenhuma métrica ativa encontrada. Configure métricas no Setup antes de avaliar.")
        }, status=400)

    respostas = list(
        Resposta.objects
        .select_related("questao", "llm")
        .filter(questao__usuario=request.user, questao_id__in=question_ids, llm_id__in=judge_ids)
        .order_by("questao_id", "id")
    )
    juizes = list(
        LLM.objects
        .filter(usuario=request.user, ativo=True, id__in=judge_ids)
        .order_by("nome")
    )

    if not respostas:
        return JsonResponse({
            "status": "erro",
            "mensagem": _("As LLMs selecionadas ainda não possuem respostas nas perguntas escolhidas.")
        }, status=400)

    if not juizes:
        return JsonResponse({
            "status": "erro",
            "mensagem": _("Nenhuma LLM avaliadora ativa foi encontrada.")
        }, status=400)

    tarefas = []
    for resposta in respostas:
        for juiz in juizes:
            if resposta.llm_id and resposta.llm_id == juiz.id:
                continue
            tarefas.append((resposta.questao, resposta, juiz))

    if not tarefas:
        return JsonResponse({
            "status": "erro",
            "mensagem": _("Não há pares válidos. Selecione ao menos duas LLMs que tenham respostas nas perguntas escolhidas.")
        }, status=400)

    if len(tarefas) > 40:
        return JsonResponse({
            "status": "erro",
            "mensagem": _("Seleção muito grande. Reduza a quantidade para até 40 avaliações por execução.")
        }, status=400)

    resultados = []
    with ThreadPoolExecutor(max_workers=min(4, len(tarefas))) as executor:
        futures = {}
        for questao, resposta, juiz in tarefas:
            prompt = _judgeai_prompt(questao, resposta, juiz, metricas)
            futures[executor.submit(_judgeai_call_configured_llm, juiz, prompt)] = (questao, resposta, juiz)

        for future in as_completed(futures):
            questao, resposta, juiz = futures[future]
            try:
                raw = future.result()
                notas, justificativa = _parse_judgeai_result(raw, metricas)
                _salvar_avaliacoes_juiz(request.user, resposta, juiz, metricas, notas, justificativa)
                resultados.append({
                    "questao_id": questao.id,
                    "pergunta": questao.conteudo,
                    "resposta_id": resposta.id,
                    "resposta_preview": _text_preview(resposta.conteudo_resposta),
                    "resposta_texto": resposta.conteudo_resposta,
                    "modelo_respondente": resposta.llm.nome if resposta.llm else "IA desconhecida",
                    "modelo_juiz": juiz.nome,
                    "notas": notas,
                    "justificativa": justificativa,
                    "erro": False,
                })
            except Exception as exc:
                resultados.append(_judgeai_error_result(questao, resposta, juiz, str(exc)))

    resultados.sort(key=lambda item: (item["questao_id"], item["modelo_respondente"], item["modelo_juiz"]))

    return JsonResponse({
        "status": "ok",
        "total": len(resultados),
        "notas_total": sum(len(item.get("notas", [])) for item in resultados if not item.get("erro")),
        "resultados": resultados,
    })

@login_required
def avaliacao(request):
    formularios = (
        Formulario.objects
        .filter(usuario=request.user)
        .annotate(
            questoes_total=Count("questoes", distinct=True),
            avaliadores_total=Count("avaliadores", distinct=True),
        )
        .prefetch_related(
            Prefetch(
                "questoes",
                queryset=Questao.objects.only("id"),
                to_attr="questoes_cache",
            )
        )
        .only("id", "nome")
        .order_by("-id")
    )
    questoes_respondidas = (
        Questao.objects
        .filter(usuario=request.user, respostas__isnull=False)
        .select_related("categoria")
        .only("id", "conteudo", "categoria_id", "categoria__id", "categoria__nome_categoria")
        .distinct()
        .order_by("-id")
    )
    return render(request, 'avaliacao/avaliacao_lista.html', {
        'formularios': formularios,
        'questoes': questoes_respondidas
    })

@login_required
def avaliacao_respostas(request, formulario_id, questao_id):
    # BLINDADO
    formulario = get_object_or_404(Formulario, id=formulario_id, usuario=request.user)
    questao = get_object_or_404(Questao, id=questao_id, usuario=request.user)
    
    respostas = Resposta.objects.filter(questao=questao)
    metricas = Metrica.objects.filter(usuario=request.user, ativa=True)

    if request.method == 'POST':
        data = json.loads(request.body)
        for item in data:
            Avaliacao.objects.create(
                usuario=request.user,
                resposta_id=item['resposta_id'],
                metrica_id=item['metrica_id'],
                avaliacao_quanti=item.get('quanti'),
                avaliacao_quali=item.get('quali'),
            )
        return JsonResponse({'status': 'ok'})

    return render(request, 'avaliacao/avaliacao_respostas.html', {
        'questao': questao,
        'respostas': respostas,
        'metricas': metricas,
        'formulario': formulario,
    })

@login_required
def avaliacao_adicionar_formulario(request):
    if request.method == 'POST':
        nome = request.POST.get('nome')
        questoes_ids = request.POST.getlist('questoes')
        formulario = Formulario.objects.create(nome=nome,usuario=request.user)
        formulario.questoes.set(questoes_ids)
        formulario.save()
        django_messages.success(request, _("Formulário '%(nome)s' criado com sucesso!") % {
            "nome": nome,
        })
    
    return redirect('avaliacao')


@login_required
def avaliacao_editar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, usuario=request.user)

    if request.method == 'POST':
        nome = request.POST.get('nome')
        questoes_ids = request.POST.getlist('questoes')
        formulario.nome = nome
        formulario.questoes.set(questoes_ids)
        formulario.save()
        django_messages.success(request, _("Formulário '%(nome)s' atualizado com sucesso!") % {
            "nome": nome,
        })
    
    return redirect('avaliacao')


@login_required
def avaliacao_deletar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, usuario=request.user)
    if request.method == 'POST':
        formulario.delete()
        django_messages.success(request, _("Formulário removido!"))
    return redirect('avaliacao')

def responder_avaliacao_publica(request, formulario_id):
    respostas_prefetch = Prefetch(
        "respostas",
        queryset=Resposta.objects.select_related("llm").only(
            "id",
            "questao_id",
            "llm_id",
            "conteudo_resposta",
            "llm__id",
            "llm__nome",
        ),
        to_attr="respostas_cache",
    )
    questoes_prefetch = Prefetch(
        "questoes",
        queryset=Questao.objects.only("id", "conteudo").prefetch_related(respostas_prefetch),
        to_attr="questoes_cache",
    )
    formulario = get_object_or_404(
        Formulario.objects.only("id", "nome", "usuario_id").prefetch_related(questoes_prefetch),
        id=formulario_id
    )
    
    metricas = list(Metrica.objects.filter(usuario_id=formulario.usuario_id, ativa=True))

    for metrica in metricas:
        if metrica.pontuacao_maxima == 2:
            label_1 = getattr(metrica, 'label_opcao_1', 'Ruim') or 'Ruim'
            label_2 = getattr(metrica, 'label_opcao_2', 'Bom')  or 'Bom'
            
            metrica.opcoes_likert = [
                (1, '👎', label_1),
                (2, '👍', label_2),
            ]
        else:
            metrica.opcoes_likert = [
                (1, '😞', 'Muito Ruim'),
                (2, '😕', 'Ruim'),
                (3, '😐', 'Regular'),
                (4, '🙂', 'Bom'),
                (5, '😄', 'Excelente'),
            ]

    if request.method == 'POST':
        nome = request.POST.get('nome')
        email = request.POST.get('email')
        profissao = request.POST.get('profissao')

        avaliador, created = Avaliador.objects.get_or_create(
            email=email,
            defaults={
                'nome': nome,
                'profissao': profissao,
                'formulario': formulario
            }
        )

        if not created:
            avaliador.formulario = formulario
            avaliador.save()

        avaliacoes = []
        for chave, valor in request.POST.items():
            if chave.startswith('quanti_') and valor:
                partes = chave.split('_')
                resposta_id = partes[1]
                metrica_id = partes[2]
                texto_quali = request.POST.get(f'quali_{resposta_id}_{metrica_id}', '')

                avaliacoes.append(AvaliacaoFormulario(
                    usuario_id=formulario.usuario_id, # BLINDADO: Vincula a avaliação gerada ao dono do formulário
                    avaliador=avaliador,
                    resposta_id=resposta_id,
                    metrica_id=metrica_id,
                    avaliacao_quanti=valor,
                    avaliacao_quali=texto_quali
                ))

        if avaliacoes:
            AvaliacaoFormulario.objects.bulk_create(avaliacoes)

        return render(request, 'avaliacao/avaliacao_sucesso.html')
    
    modo_cego = request.GET.get('blind') == 'true'

    contexto = {
        'formulario': formulario,
        'metricas': metricas, # Agora as métricas já levam as opções dentro delas!
        'blind_mode': modo_cego,
    }
    return render(request, 'avaliacao/avaliacao_publica.html', contexto)


@login_required
def dashboard_avaliacoes(request):
    metricas = list(
        Metrica.objects
        .filter(usuario=request.user, ativa=True)
        .order_by("id")
        .values("id", "nome", "pontuacao_maxima")
    )
    llms = list(
        LLM.objects
        .filter(usuario=request.user)
        .order_by("-id")
        .values("id", "nome")
    )

    por_metrica = {}
    divergencias = []
    especialistas_agregados = {
        (item["metrica_id"], item["resposta__llm_id"]): item
        for item in (
            AvaliacaoFormulario.objects
            .filter(usuario=request.user, avaliacao_quanti__isnull=False)
            .values("metrica_id", "resposta__llm_id")
            .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        )
    }
    juizes_agregados = {
        (item["metrica_id"], item["resposta__llm_id"]): item
        for item in (
            AvaliacaoJuiz.objects
            .filter(usuario=request.user, avaliacao_quanti__isnull=False, erro=False)
            .values("metrica_id", "resposta__llm_id")
            .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        )
    }

    for metrica in metricas:
        modelos = {}
        maximo = metrica["pontuacao_maxima"] or 5

        for llm in llms:
            especialistas = especialistas_agregados.get(
                (metrica["id"], llm["id"]),
                {"media": None, "total": 0},
            )
            juizes = juizes_agregados.get(
                (metrica["id"], llm["id"]),
                {"media": None, "total": 0},
            )

            media_especialistas = especialistas["media"]
            media_juizes = juizes["media"]
            diferenca = None

            if media_especialistas is not None and media_juizes is not None:
                diferenca = round(media_juizes - media_especialistas, 2)
                divergencias.append({
                    "metrica": metrica["nome"],
                    "llm": llm["nome"],
                    "diferenca": diferenca,
                    "desvio": abs(diferenca),
                    "especialistas": round(media_especialistas, 2),
                    "juizes": round(media_juizes, 2),
                    "max": maximo,
                })

            modelos[llm["nome"]] = {
                "especialistas": round(media_especialistas, 2) if media_especialistas is not None else None,
                "juizes": round(media_juizes, 2) if media_juizes is not None else None,
                "diferenca": diferenca,
                "especialistas_count": especialistas["total"],
                "juizes_count": juizes["total"],
            }

        por_metrica[metrica["nome"]] = {
            "id": metrica["id"],
            "pontuacao_maxima": maximo,
            "modelos": modelos,
        }

    desvios = [item["desvio"] for item in divergencias]
    desvio_medio = round(sum(desvios) / len(desvios), 2) if desvios else None
    maiores_divergencias = sorted(divergencias, key=lambda item: item["desvio"], reverse=True)[:8]

    total_especialistas = AvaliacaoFormulario.objects.filter(
        usuario=request.user,
        avaliacao_quanti__isnull=False,
    ).count()
    total_juizes = AvaliacaoJuiz.objects.filter(
        usuario=request.user,
        erro=False,
        avaliacao_quanti__isnull=False,
    ).count()
    avaliadores_humanos = Avaliador.objects.filter(
        formulario__usuario=request.user,
        avaliacoes__avaliacao_quanti__isnull=False,
    ).distinct().count()
    juizes_online = AvaliacaoJuiz.objects.filter(
        usuario=request.user,
        erro=False,
        avaliacao_quanti__isnull=False,
    ).values("juiz_id").distinct().count()

    payload = {
        "metricas": [
            {
                "id": metrica["id"],
                "nome": metrica["nome"],
                "pontuacao_maxima": metrica["pontuacao_maxima"] or 5,
            }
            for metrica in metricas
        ],
        "llms": [llm["nome"] for llm in llms],
        "por_metrica": por_metrica,
        "maiores_divergencias": maiores_divergencias,
        "resumo": {
            "modelos": len(llms),
            "metricas": len(metricas),
            "notas_especialistas": total_especialistas,
            "notas_juizes": total_juizes,
            "avaliadores_humanos": avaliadores_humanos,
            "juizes_online": juizes_online,
            "pontos_comparaveis": len(divergencias),
            "desvio_medio": desvio_medio,
        },
    }

    modo_inicial = request.GET.get("mode", "especialistas")
    if modo_inicial not in ("especialistas", "juizes", "comparativo"):
        modo_inicial = "especialistas"

    return render(request, "avaliacao/dashboard_avaliacoes.html", {
        "dashboard_json": json.dumps(payload, ensure_ascii=False),
        "modo_inicial": modo_inicial,
    })


@login_required
def dashboard_comparativo_avaliacoes(request):
    return redirect(f"{reverse('dashboard_avaliacoes')}?mode=comparativo")

@login_required
def menu_avaliacao(request):
    return render(request,"avaliacao/menu_avaliacao.html")

@login_required
def juizes_comparador(request):
    questoes = (
        Questao.objects
        .filter(usuario=request.user)
        .select_related("categoria")
        .prefetch_related("respostas__llm")
        .order_by("-id")
    )
    llms = list(LLM.objects.filter(usuario=request.user, ativo=True).order_by("nome"))
    metricas = list(Metrica.objects.filter(usuario=request.user, ativa=True).order_by("id"))
    categorias = Categoria.objects.filter(usuario=request.user).order_by("nome_categoria")

    questoes_data = []
    for questao in questoes:
        respostas_data = []
        modelos_ids = []

        for resposta in questao.respostas.all():
            llm_nome = resposta.llm.nome if resposta.llm else "IA desconhecida"
            llm_id = resposta.llm_id
            if llm_id:
                modelos_ids.append(llm_id)

            respostas_data.append({
                "id": resposta.id,
                "llm_id": llm_id,
                "llm": llm_nome,
                "preview": _text_preview(resposta.conteudo_resposta, 420),
            })

        questoes_data.append({
            "id": questao.id,
            "conteudo": questao.conteudo,
            "categoria_id": questao.categoria_id,
            "categoria": questao.categoria.nome_categoria if questao.categoria else "Sem categoria",
            "status": "respondida" if respostas_data else "sem_respostas",
            "respostas_count": len(respostas_data),
            "modelos_ids": modelos_ids,
            "respostas": respostas_data,
        })

    return render(request, "juizes/comparador.html", {
        "questoes_data": questoes_data,
        "llms_data": [
            {
                "id": llm.id,
                "nome": llm.nome,
                "provedor": llm.descricao or "LLM",
            }
            for llm in llms
        ],
        "metricas_data": [
            {
                "id": metrica.id,
                "nome": metrica.nome,
                "max": _metric_max(metrica),
            }
            for metrica in metricas
        ],
        "categorias": categorias,
    })
