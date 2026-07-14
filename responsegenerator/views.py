from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
import os
import re
from datetime import timedelta
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie
import json
from django.views.decorators.http import require_http_methods
from responsegenerator.models import (
    AdminPonderSec,
    Avaliacao,
    AvaliacaoFormulario,
    AvaliacaoJuiz,
    AvaliacaoPublicaLLM,
    Avaliador,
    Categoria,
    Formulario,
    Historico,
    LLM,
    LLMPublica,
    Metrica,
    PerguntaPublica,
    Questao,
    Resposta,
    RespostaPublica,
)
from functools import wraps
from django.db.models import Avg, Count, Prefetch, Q
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


# ===== VIEWS PÚBLICAS (SEM LOGIN) =====

@ensure_csrf_cookie
def usuario_final_chat(request):
    """Renderiza a página pública de chat para usuários finais."""
    return render(request, 'chat/chatpublico.html')


@require_http_methods(["POST"])
def usuario_final_chat_api(request):
    """
    API pública para processar perguntas de usuários finais.
    Recebe: {"pergunta": "..."}
    Retorna: {"status": "ok/erro", "respostas": [...], "mensagem": "..."}
    """
    try:
        dados = json.loads(request.body)
        pergunta = dados.get('pergunta', '').strip()
        
        if not pergunta:
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'Pergunta não pode estar vazia.'
            }, status=400)
        
        # Apenas LLMs cadastradas pelo admin no painel /admin-pondersec/ atendem o chat público.
        # As LLMs dos pesquisadores (model LLM) NUNCA são usadas aqui.
        llms_ativos = list(LLMPublica.objects.filter(ativo=True).order_by("nome"))

        if not llms_ativos:
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'Nenhuma LLM foi configurada pelo administrador para o chat público.'
            }, status=400)
        
        # Contexto para as LLMs
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
        prompt_final = f"{contexto}\n\n{pergunta}"
        
        pergunta_publica = PerguntaPublica.objects.create(conteudo=pergunta)

        # Gera respostas em paralelo
        respostas = []
        respostas_publicas = []
        
        def gerar_resposta_llm(llm):
            """Gera resposta para um LLM específico."""
            try:
                texto_resposta = _judgeai_call_configured_llm(llm, prompt_final)
                return {
                    'llm_id': llm.id,
                    'modelo': llm.nome,
                    'resposta': texto_resposta.strip(),
                    'ok': True
                }
            
            except Exception as e:
                return {
                    'llm_id': llm.id,
                    'modelo': llm.nome,
                    'resposta': f"Erro ao consultar: {str(e)}",
                    'ok': False
                }
        
        # Executa em paralelo
        with ThreadPoolExecutor(max_workers=min(3, len(llms_ativos))) as executor:
            futures = {executor.submit(gerar_resposta_llm, llm): llm for llm in llms_ativos}
            
            for future in as_completed(futures):
                llm = futures[future]
                try:
                    resultado = future.result()
                except Exception as e:
                    print(f"Erro ao processar LLM: {str(e)}")
                    resultado = {
                        'llm_id': llm.id,
                        'modelo': llm.nome,
                        'resposta': f"Erro ao consultar: {str(e)}",
                        'ok': False,
                    }

                resposta_publica = RespostaPublica.objects.create(
                    pergunta=pergunta_publica,
                    llm_id=resultado["llm_id"],
                    conteudo_resposta=resultado["resposta"],
                    ok=resultado["ok"],
                )
                resultado["resposta_id"] = resposta_publica.id
                respostas_publicas.append(resposta_publica)
                respostas.append(resultado)

        avaliacao_status = _executar_avaliacao_cruzada_publica(
            pergunta_publica,
            respostas_publicas,
            llms_ativos,
            _metricas_publicas_ativas(),
        )
        avaliacoes_por_resposta = _resumo_avaliacoes_publicas_por_resposta(
            [resposta.id for resposta in respostas_publicas]
        )
        tabela_avaliacao_cruzada = _tabela_avaliacoes_publicas(
            [resposta.id for resposta in respostas_publicas]
        )

        for resultado in respostas:
            resultado["avaliacao"] = avaliacoes_por_resposta.get(
                resultado["resposta_id"],
                {"status": "sem_dados", "media_geral": None, "notas_total": 0, "metricas": []},
            )
        
        return JsonResponse({
            'status': 'ok',
            'respostas': respostas,
            'avaliacao_cruzada': avaliacao_status,
            'tabela_avaliacao_cruzada': tabela_avaliacao_cruzada,
            'mensagem': 'Respostas geradas com sucesso.'
        })
    
    except json.JSONDecodeError:
        return JsonResponse({
            'status': 'erro',
            'mensagem': 'Erro ao processar JSON.'
        }, status=400)
    except Exception as e:
        print(f"Erro em usuario_final_chat_api: {str(e)}")
        return JsonResponse({
            'status': 'erro',
            'mensagem': f'Erro no servidor: {str(e)}'
        }, status=500)


# ===== FIM VIEWS PÚBLICAS =====

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
        humanas_encontradas = []
        respostas_qs = Resposta.objects.filter(questao=questao).select_related('llm')

        total_llms = LLM.objects.filter(usuario=request.user, ativo=True).count()

        for r in respostas_qs:
            if not getattr(r, 'llm', None):
                humanas_encontradas.append({
                    'id': r.id,
                    'texto': r.conteudo_resposta,
                })
                continue
            nome_ia = r.llm.nome
            nome_ia_lower = nome_ia.lower()

            if 'gemini' in nome_ia_lower or 'google' in nome_ia_lower: cor = '#4285F4'
            elif 'groq' in nome_ia_lower or 'llama' in nome_ia_lower or 'mixtral' in nome_ia_lower: cor = '#f55036'
            elif 'chatgpt' in nome_ia_lower or 'gpt' in nome_ia_lower or 'openai' in nome_ia_lower: cor = '#10a37f'
            else: cor = '#00ff9f'

            respostas_encontradas.append({
                'id': r.id,
                'ia': nome_ia,
                'texto': r.conteudo_resposta,
                'cor': cor
            })

        if not humanas_encontradas and questao.resposta_humana and questao.resposta_humana.strip():
            humanas_encontradas.append({
                'id': None,
                'texto': questao.resposta_humana,
            })

        tem_resposta_humana = len(humanas_encontradas) > 0

        return JsonResponse({
            'pergunta': questao.conteudo,
            'questao_id': questao.id,
            'data': '',
            'respostas': respostas_encontradas,
            'respostas_humanas': humanas_encontradas,
            'resposta_humana': humanas_encontradas[0]['texto'] if humanas_encontradas else None,
            'total_llms': total_llms,
            'tem_resposta_humana': tem_resposta_humana,
            'tem_respostas_ia': len(respostas_encontradas) > 0,
        })
    except Exception as e:
        print(f"Erro em ver_detalhes_questao: {str(e)}")
        return JsonResponse({'erro': str(e)}, status=500)


@login_required
def adicionar_resposta_humana_questao(request, id):
    if request.method != 'POST':
        return JsonResponse({'erro': 'Método não permitido'}, status=405)

    questao = get_object_or_404(Questao, id=id, usuario=request.user)
    texto = request.POST.get('resposta_humana', '').strip()

    if not texto:
        return JsonResponse({'erro': 'Resposta não pode estar vazia.'}, status=400)

    resposta = Resposta.objects.create(
        questao=questao,
        llm=None,
        conteudo_resposta=texto,
    )

    return JsonResponse({'ok': True, 'resposta_id': resposta.id, 'texto': texto})


@login_required
@require_http_methods(["DELETE"])
def deletar_resposta_ia(request, resposta_id):
    resposta = get_object_or_404(
        Resposta.objects.select_related('questao'),
        id=resposta_id,
        questao__usuario=request.user
    )
    questao_id = resposta.questao_id
    resposta.delete()
    return JsonResponse({'ok': True, 'questao_id': questao_id})


@login_required
def gerar_resposta_ia_unica(request, questao_id, llm_id):
    questao = get_object_or_404(Questao, id=questao_id, usuario=request.user)
    llm = get_object_or_404(LLM, id=llm_id, usuario=request.user, ativo=True)

    ja_existe = Resposta.objects.filter(questao=questao, llm=llm).exists()
    if ja_existe:
        return JsonResponse({'status': 'ja_existe'})

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
            return JsonResponse({'status': 'erro', 'mensagem': f"Provedor '{llm.descricao}' não reconhecido."}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'erro', 'mensagem': f"Erro na IA {llm.nome}: {str(e)}"}, status=500)

    Resposta.objects.create(
        questao_id=questao_id,
        llm=llm,
        conteudo_resposta=texto_ia_limpa.strip()
    )

    return JsonResponse({'status': 'ok'})


@login_required
def gerar_respostas_ia_faltantes(request, questao_id):
    if request.method != 'POST':
        return JsonResponse({'erro': 'Método não permitido'}, status=405)

    questao = get_object_or_404(Questao, id=questao_id, usuario=request.user)
    llms_ativos = list(LLM.objects.filter(usuario=request.user, ativo=True))

    if not llms_ativos:
        return JsonResponse({'status': 'erro', 'mensagem': 'Nenhuma IA configurada.'}, status=400)

    respostas_existentes = set(
        Resposta.objects.filter(questao=questao).values_list('llm_id', flat=True)
    )

    ias_faltantes = [llm for llm in llms_ativos if llm.id not in respostas_existentes]

    if not ias_faltantes:
        return JsonResponse({'status': 'ja_completo'})

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

    erros = []

    def _gerar(llm):
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
                return
        except Exception as e:
            erros.append(f"{llm.nome}: {str(e)}")
            return

        Resposta.objects.create(
            questao_id=questao_id,
            llm=llm,
            conteudo_resposta=texto_ia_limpa.strip()
        )

    with ThreadPoolExecutor(max_workers=min(4, len(ias_faltantes))) as executor:
        list(executor.map(_gerar, ias_faltantes))

    if erros:
        return JsonResponse({'status': 'parcial', 'erros': erros})

    return JsonResponse({'status': 'ok', 'geradas': len(ias_faltantes)})


@login_required
def limpar_questoes(request):
    if request.method == 'POST':   
        Questao.objects.filter(usuario=request.user).delete()
        Categoria.objects.filter(usuario=request.user).delete()
        django_messages.success(request, _("O histórico de questões e categorias foi limpo!"))
    return redirect('questoes')
 
@login_required
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
        resposta_humana = request.POST.get('resposta_humana', '').strip()
        
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
                resposta_humana=resposta_humana if resposta_humana else None,
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
                        resposta = item.get("resposta", "").strip() or item.get("RESPOSTA", "").strip()
                        nome_categoria_item = item.get("categoria", "").strip() or item.get("Categoria", "").strip()

                        if texto_pergunta:
                            cat_obj = categoria_padrao
                            if nome_categoria_item:
                                cat_obj, _created = Categoria.objects.get_or_create(
                                    nome_categoria=nome_categoria_item,
                                    usuario=request.user,
                                    defaults={'descricao_categoria': ''}
                                )

                            perguntas.append(texto_pergunta)
                            Questao.objects.create(
                                conteudo=texto_pergunta,
                                usuario=request.user,
                                categoria=cat_obj,
                                resposta_humana=resposta if resposta else None,
                            )

                except (json.JSONDecodeError, AttributeError):
                    django_messages.error(request, _("Arquivo JSON inválido ou mal formatado."))
                    return redirect('questoes')

            else:
                linhas = conteudo_texto.splitlines()
                categoria_atual = categoria_padrao
                i = 0
                while i < len(linhas):
                    linha = linhas[i].strip()

                    if not linha:
                        i += 1
                        continue

                    if linha.lower().startswith("eixo"):
                        nome_cat = re.sub(r'^eixo\s*\d+\s*[-–—:]\s*', '', linha, flags=re.IGNORECASE).strip()
                        if nome_cat:
                            categoria_atual, _created = Categoria.objects.get_or_create(
                                nome_categoria=nome_cat,
                                usuario=request.user,
                                defaults={'descricao_categoria': ''}
                            )
                        i += 1
                        continue

                    texto_pergunta = re.sub(r'^\d+[\.\)]\s*', '', linha).strip()
                    resposta = None

                    if texto_pergunta:
                        if i + 1 < len(linhas):
                            proxima_linha = linhas[i + 1].strip()
                            if proxima_linha.upper().startswith("RESPOSTA:"):
                                resposta = proxima_linha[len("RESPOSTA:"):].strip()
                                i += 1

                        perguntas.append(texto_pergunta)
                        Questao.objects.create(
                            conteudo=texto_pergunta,
                            usuario=request.user,
                            categoria=categoria_atual,
                            resposta_humana=resposta if resposta else None,
                        )

                    i += 1

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
                texto_ia_limpa = _("Provedor '%(provedor)s' não reconhecido para execução automática.") % {
                    "provedor": llm.descricao
                }

        except Exception as e:
            texto_ia_limpa = _("Erro na IA %(nome)s: %(erro)s") % {
                "nome": llm.nome,
                "erro": str(e),
            }

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
            raise RuntimeError(_("Biblioteca google-genai não instalada."))
        client = genai.Client(api_key=llm.api_key)
        response = client.models.generate_content(model=llm.nome, contents=prompt)
        return (getattr(response, "text", "") or "").strip()

    if "groq" in provider or "llama" in provider or "mixtral" in provider:
        if Groq is None:
            raise RuntimeError(_("Biblioteca groq não instalada."))
        client = Groq(api_key=llm.api_key)
        completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=llm.nome,
        )
        return completion.choices[0].message.content.strip()

    if "deepseek" in provider:
        if openai is None:
            raise RuntimeError(_("Biblioteca openai não instalada."))
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
            raise RuntimeError(_("Biblioteca openai não instalada."))
        client = openai.OpenAI(api_key=llm.api_key)
        response = client.chat.completions.create(
            model=llm.nome,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    raise RuntimeError(_("Provedor '%(provedor)s' não reconhecido.") % {
        "provedor": llm.descricao or llm.nome
    })

def _judgeai_prompt(questao, resposta, juiz, metricas):
    metricas_txt = "\n".join(
        f"- {m.nome} (0 a {_metric_max(m)}): {m.descricao or m.criterio_texto or 'Avalie este critério.'}"
        for m in metricas
    )
    return (
        "Você é uma LLM atuando como juiz técnico especializado em cibersegurança no PonderSec.\n"
        "Sua tarefa é avaliar, de forma objetiva e criteriosa, a resposta de outro modelo (o 'respondente') "
        "a uma pergunta técnica de cibersegurança.\n\n"

        "REGRAS DE AVALIAÇÃO:\n"
        "1. Para CADA métrica listada abaixo, leia atentamente a DESCRIÇÃO da métrica. A descrição define o critério exato que você deve avaliar.\n"
        "2. Avalie a resposta EXCLUSIVAMENTE com base no que a descrição da métrica pede. Não invente critérios próprios.\n"
        "3. Antes de decidir a nota, releia a descrição da métrica e depois releia a resposta em busca de evidências concretas "
        "(trechos, afirmações, exemplos, comandos, códigos) que sustentem ou contradigam o critério descrito.\n"
        "4. Evite viés de verbosidade: uma resposta mais longa não é automaticamente melhor. Avalie se a resposta "
        "atende ao critério da métrica, não o tamanho do texto.\n"
        "5. Evite viés de complacência: não dê notas altas por padrão. Se a resposta não atender ao critério descrito "
        "na métrica, a nota deve refletir isso.\n"
        "6. Se a resposta avaliada for vazia, sem sentido, um refusal sem justificativa técnica, ou completamente fora "
        "do escopo da pergunta, atribua nota mínima em todas as métricas aplicáveis e explique isso na justificativa.\n"
        "7. Se a métrica não for aplicável ao tipo de pergunta ou resposta (ex.: métrica sobre código quando não "
        "há código na resposta), atribua a nota mínima da escala e explique por que não há evidência para nota maior.\n\n"

        "FORMATO DAS JUSTIFICATIVAS:\n"
        "- Escreva em português, entre 20 e 45 palavras por métrica.\n"
        "- Comece citando evidência concreta da resposta (trecho, afirmação, exemplo, comando).\n"
        "- Conecte essa evidência ao critério descrito na métrica, explicando por que a nota foi aquela.\n"
        "- Não use frases genéricas como 'a resposta foi boa', 'faltou detalhamento' ou 'está correta' sem "
        "explicar o quê especificamente foi bom, faltou ou está correto.\n"
        "- Não repita o enunciado da métrica como se fosse a justificativa.\n\n"

        "FORMATO DE SAÍDA:\n"
        "Retorne SOMENTE um JSON válido, sem markdown, sem texto antes ou depois, sem blocos de código (```).\n"
        "{\n"
        '  "notas": [\n'
        '    {"metrica": "Nome exato da métrica", "nota": 0, "justificativa": "Nota X/Y: frase completa explicando o motivo da nota com evidência da resposta."}\n'
        "  ],\n"
        '  "justificativa": "síntese geral em uma frase completa, mencionando o principal ponto forte e a principal fraqueza da resposta avaliada"\n'
        "}\n"
        "- O campo 'nota' deve ser um número dentro da escala definida para cada métrica.\n"
        "- A ordem das métricas no array 'notas' deve seguir a ordem em que foram apresentadas.\n"
        "- Não inclua métricas que não estejam na lista fornecida.\n"
        "- Não adicione campos extras ao JSON.\n\n"

        f"Juiz: {juiz.nome}\n\n"
        f"Métricas (nome, escala e DESCRIÇÃO — avalie conforme o descrito):\n{metricas_txt}\n\n"
        f"Pergunta de cibersegurança avaliada:\n{questao.conteudo}\n\n"
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


def _formatar_justificativa_avaliacao(texto, nota=None, maximo=5):
    texto = re.sub(r"\s+", " ", (texto or "").strip())

    if not texto:
        texto = "a LLM avaliadora não retornou uma justificativa detalhada para esta métrica"

    if texto and texto[-1] not in ".!?":
        texto += "."

    if nota is not None:
        prefixo = f"Nota {nota}/{maximo}: "
        if not re.match(r"^nota\s+\d+\s*/\s*\d+\s*:", texto, re.IGNORECASE):
            texto = prefixo + texto[:1].upper() + texto[1:]

    return texto


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
                "avaliacao_quali": _formatar_justificativa_avaliacao(
                    item.get("justificativa"),
                    nota,
                    _metric_max(metrica),
                ),
                "justificativa_geral": justificativa,
                "erro": False,
            }
        )

PUBLIC_CROSS_EVAL_MAX_TASKS = 40


def _metricas_publicas_ativas():
    return list(Metrica.objects.filter(usuario__isnull=True, ativa=True).order_by("id"))


def _public_judge_prompt(pergunta_publica, resposta_publica, juiz, metricas):
    metricas_txt = "\n".join(
        f"- {m.nome} (0 a {_metric_max(m)}): {m.descricao or m.criterio_texto or 'Avalie este critério.'}"
        for m in metricas
    )
    respondente = resposta_publica.llm.nome if resposta_publica.llm else "LLM removida"
    return (
        "Você é uma LLM atuando como juiz no chat público do PonderSec.\n"
        "O usuário final é leigo em cibersegurança. Avalie se a resposta de outro modelo é correta, clara, útil e segura para esse público.\n\n"

        "REGRAS DE AVALIAÇÃO:\n"
        "1. Para CADA métrica listada abaixo, leia atentamente a DESCRIÇÃO da métrica. A descrição define o critério exato que você deve avaliar.\n"
        "2. Avalie a resposta EXCLUSIVAMENTE com base no que a descrição da métrica pede. Não invente critérios próprios.\n"
        "3. Se a descrição da métrica pede, por exemplo, 'clareza', avalie especificamente se a resposta é clara para um leigo. "
        "Se pede 'segurança', avalie se a resposta não induz a práticas perigosas. Siga à risca o que está descrito.\n"
        "4. Antes de atribuir cada nota, releia a descrição da métrica e verifique se a resposta atende ou não ao que ela descreve.\n"
        "5. Evite viés de complacência: não dê notas altas por padrão. Se a resposta não atender ao critério descrito na métrica, a nota deve ser baixa.\n"
        "6. Se a resposta for vazia, uma recusa (refusal), ou completamente fora do escopo, atribua nota mínima em todas as métricas.\n\n"

        "FORMATO DAS JUSTIFICATIVAS:\n"
        "- Escreva em português, entre 20 e 45 palavras por métrica.\n"
        "- Comece citando evidência concreta da resposta (trecho, afirmação, exemplo).\n"
        "- Conecte essa evidência ao critério descrito na métrica, explicando por que a nota foi aquela.\n"
        "- Não use frases genéricas como 'é clara' ou 'está completa'; detalhe o quê especificamente foi bom ou ruim.\n\n"

        "FORMATO DE SAÍDA:\n"
        "Retorne SOMENTE um JSON válido, sem markdown, sem texto antes ou depois, sem blocos de código (```).\n"
        "{\n"
        '  "notas": [\n'
        '    {"metrica": "Nome exato da métrica", "nota": 0, "justificativa": "Nota X/Y: frase completa explicando o motivo da nota com evidência da resposta."}\n'
        "  ],\n"
        '  "justificativa": "síntese geral em uma frase completa"\n'
        "}\n"
        "- O campo 'nota' deve ser um número dentro da escala definida para cada métrica.\n"
        "- A ordem das métricas no array 'notas' deve seguir a ordem em que foram apresentadas.\n\n"

        f"Juiz: {juiz.nome}\n\n"
        f"Métricas (nome, escala e DESCRIÇÃO — avalie conforme o descrito):\n{metricas_txt}\n\n"
        f"Pergunta do usuário público:\n{pergunta_publica.conteudo}\n\n"
        f"Modelo respondente: {respondente}\n"
        f"Resposta avaliada:\n{resposta_publica.conteudo_resposta}"
    )


def _salvar_avaliacoes_publicas(resposta_publica, juiz, metricas, notas, justificativa):
    metricas_por_nome = {metrica.nome.lower(): metrica for metrica in metricas}
    AvaliacaoPublicaLLM.objects.filter(
        juiz=juiz,
        resposta=resposta_publica,
        metrica__in=metricas,
    ).delete()

    for item in notas:
        metrica = metricas_por_nome.get((item.get("metrica") or "").lower())
        nota = item.get("nota")

        if not metrica or nota is None:
            continue

        AvaliacaoPublicaLLM.objects.update_or_create(
            juiz=juiz,
            resposta=resposta_publica,
            metrica=metrica,
            defaults={
                "avaliacao_quanti": nota,
                "avaliacao_quali": _formatar_justificativa_avaliacao(
                    item.get("justificativa"),
                    nota,
                    _metric_max(metrica),
                ),
                "justificativa_geral": justificativa,
                "erro": False,
            },
        )


def _executar_avaliacao_cruzada_publica(pergunta_publica, respostas_publicas, juizes, metricas):
    if not metricas:
        return {"status": "sem_metricas", "total": 0, "notas_total": 0, "mensagem": "Nenhuma métrica pública ativa."}

    if len(juizes) < 2:
        return {"status": "sem_pares", "total": 0, "notas_total": 0, "mensagem": "É preciso ter ao menos duas LLMs públicas ativas."}

    respostas_validas = [resposta for resposta in respostas_publicas if resposta.ok and resposta.llm_id]
    tarefas = []
    for resposta_publica in respostas_validas:
        for juiz in juizes:
            if resposta_publica.llm_id == juiz.id:
                continue
            tarefas.append((resposta_publica, juiz))

    if not tarefas:
        return {"status": "sem_pares", "total": 0, "notas_total": 0, "mensagem": "Não há pares válidos para avaliação cruzada."}

    tarefas_limitadas = tarefas[:PUBLIC_CROSS_EVAL_MAX_TASKS]
    limitou = len(tarefas_limitadas) < len(tarefas)

    resultados = []
    with ThreadPoolExecutor(max_workers=min(4, len(tarefas_limitadas))) as executor:
        futures = {}
        for resposta_publica, juiz in tarefas_limitadas:
            prompt = _public_judge_prompt(pergunta_publica, resposta_publica, juiz, metricas)
            futures[executor.submit(_judgeai_call_configured_llm, juiz, prompt)] = (resposta_publica, juiz)

        for future in as_completed(futures):
            resposta_publica, juiz = futures[future]
            try:
                raw = future.result()
                notas, justificativa = _parse_judgeai_result(raw, metricas)
                _salvar_avaliacoes_publicas(resposta_publica, juiz, metricas, notas, justificativa)
                resultados.append({"erro": False, "notas": notas})
            except Exception as exc:
                resultados.append({"erro": True, "mensagem": str(exc), "notas": []})

    notas_total = sum(
        len([nota for nota in item.get("notas", []) if nota.get("nota") is not None])
        for item in resultados
        if not item.get("erro")
    )
    erros = sum(1 for item in resultados if item.get("erro"))

    return {
        "status": "ok" if notas_total else "sem_notas",
        "total": len(resultados),
        "notas_total": notas_total,
        "erros": erros,
        "limitado": limitou,
    }


def _resumo_avaliacoes_publicas_por_resposta(resposta_ids):
    if not resposta_ids:
        return {}

    resumo = {
        resposta_id: {
            "status": "sem_dados",
            "media_geral": None,
            "notas_total": 0,
            "metricas": [],
        }
        for resposta_id in resposta_ids
    }

    gerais = {
        item["resposta_id"]: item
        for item in (
            AvaliacaoPublicaLLM.objects
            .filter(resposta_id__in=resposta_ids, erro=False, avaliacao_quanti__isnull=False)
            .values("resposta_id")
            .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        )
    }
    por_metrica = (
        AvaliacaoPublicaLLM.objects
        .filter(resposta_id__in=resposta_ids, erro=False, avaliacao_quanti__isnull=False)
        .values("resposta_id", "metrica_id", "metrica__nome", "metrica__pontuacao_maxima")
        .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        .order_by("metrica__nome")
    )

    for resposta_id, item in gerais.items():
        resumo[resposta_id].update({
            "status": "ok",
            "media_geral": round(item["media"], 2) if item["media"] is not None else None,
            "notas_total": item["total"],
        })

    for item in por_metrica:
        resposta_id = item["resposta_id"]
        resumo[resposta_id]["metricas"].append({
            "id": item["metrica_id"],
            "nome": item["metrica__nome"] or "Métrica removida",
            "media": round(item["media"], 2) if item["media"] is not None else None,
            "max": item["metrica__pontuacao_maxima"] or 5,
            "total": item["total"],
        })

    return resumo


def _tabela_avaliacoes_publicas(resposta_ids, limite=None):
    if not resposta_ids:
        return []

    qs = (
        AvaliacaoPublicaLLM.objects
        .filter(resposta_id__in=resposta_ids, erro=False, avaliacao_quanti__isnull=False)
        .select_related("juiz", "resposta__llm", "metrica")
        .order_by("resposta_id", "juiz__nome", "metrica__nome")
    )
    if limite:
        qs = qs[:limite]

    linhas = []
    for avaliacao in qs:
        maximo = _metric_max(avaliacao.metrica) if avaliacao.metrica else 5
        linhas.append({
            "resposta_id": avaliacao.resposta_id,
            "modelo_respondente": avaliacao.resposta.llm.nome if avaliacao.resposta.llm else "LLM removida",
            "modelo_avaliador": avaliacao.juiz.nome if avaliacao.juiz else "LLM avaliadora removida",
            "metrica": avaliacao.metrica.nome if avaliacao.metrica else "Métrica removida",
            "nota": avaliacao.avaliacao_quanti,
            "max": maximo,
            "justificativa": _formatar_justificativa_avaliacao(
                avaliacao.avaliacao_quali or avaliacao.justificativa_geral,
                avaliacao.avaliacao_quanti,
                maximo,
            ),
        })

    return linhas


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
        .only("id", "nome", "tipo_respostas")
        .order_by("-id")
    )
    questoes_respondidas = (
        Questao.objects
        .filter(usuario=request.user)
        .select_related("categoria")
        .prefetch_related(
            Prefetch(
                "respostas",
                queryset=Resposta.objects.only("id", "questao_id"),
                to_attr="respostas_cache",
            )
        )
        .only("id", "conteudo", "categoria_id", "categoria__id", "categoria__nome_categoria", "resposta_humana")
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
        formulario = Formulario.objects.create(nome=nome, usuario=request.user)
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
        queryset=Questao.objects.only("id", "conteudo", "resposta_humana").prefetch_related(respostas_prefetch),
        to_attr="questoes_cache",
    )
    formulario = get_object_or_404(
        Formulario.objects.only("id", "nome", "usuario_id", "tipo_respostas").prefetch_related(questoes_prefetch),
        id=formulario_id
    )
    
    metricas = list(Metrica.objects.filter(usuario_id=formulario.usuario_id, ativa=True))

    escala_padrao = [
        ('😞', _('Muito Ruim')),
        ('😕', _('Ruim')),
        ('😐', _('Regular')),
        ('🙂', _('Bom')),
        ('😄', _('Excelente')),
    ]

    for metrica in metricas:
        maximo = max(2, min(metrica.pontuacao_maxima or 5, 5))
        metrica.pontuacao_maxima = maximo

        if maximo == 2:
            label_1 = getattr(metrica, 'label_opcao_1', 'Ruim') or 'Ruim'
            label_2 = getattr(metrica, 'label_opcao_2', 'Bom')  or 'Bom'
            
            metrica.opcoes_likert = [
                (1, '👎', label_1),
                (2, '👍', label_2),
            ]
        else:
            indices_escala = [round(i * 4 / (maximo - 1)) for i in range(maximo)]
            metrica.opcoes_likert = [
                (valor, *escala_padrao[indice])
                for valor, indice in enumerate(indices_escala, start=1)
            ]

    for questao in formulario.questoes_cache:
        if questao.resposta_humana:
            ja_existe = any(r.llm is None for r in questao.respostas_cache)
            if not ja_existe:
                resp_humana = Resposta.objects.create(
                    questao=questao,
                    llm=None,
                    conteudo_resposta=questao.resposta_humana,
                )
                questao.respostas_cache.append(resp_humana)

    if request.method == 'POST':
        nome = request.POST.get('nome', '').strip()
        email = request.POST.get('email', '').strip()
        profissao = request.POST.get('profissao', '').strip()
        respostas_ids = [
            resposta.id
            for questao in formulario.questoes_cache
            for resposta in questao.respostas_cache
        ]
        metricas_quantitativas = [
            metrica for metrica in metricas if metrica.tipo == 'quantitativa'
        ]

        form_error = None
        if not nome or not email or not profissao:
            form_error = _('Preencha seus dados de identificação antes de iniciar a avaliação.')
        elif not respostas_ids or not metricas_quantitativas:
            form_error = _('Este formulário não possui respostas e métricas quantitativas para avaliar.')

        avaliacoes = []
        if not form_error:
            for resposta_id in respostas_ids:
                for metrica in metricas_quantitativas:
                    chave = f'quanti_{resposta_id}_{metrica.id}'
                    valor = request.POST.get(chave, '').strip()

                    try:
                        nota = int(valor)
                    except (TypeError, ValueError):
                        form_error = _('Avalie todas as respostas antes de enviar o formulário.')
                        break

                    maximo = metrica.pontuacao_maxima or 5
                    if nota < 1 or nota > maximo:
                        form_error = _('Uma das notas informadas está fora da escala permitida.')
                        break

                    avaliacoes.append(AvaliacaoFormulario(
                        usuario_id=formulario.usuario_id,
                        resposta_id=resposta_id,
                        metrica_id=metrica.id,
                        avaliacao_quanti=nota,
                        avaliacao_quali=request.POST.get(
                            f'quali_{resposta_id}_{metrica.id}', ''
                        ).strip(),
                    ))
                if form_error:
                    break

        if form_error:
            contexto = {
                'formulario': formulario,
                'metricas': metricas,
                'blind_mode': request.GET.get('blind') == 'true',
                'form_error': form_error,
                'nome_informado': nome,
                'email_informado': email,
                'profissao_informada': profissao,
            }
            return render(
                request,
                'avaliacao/avaliacao_publica.html',
                contexto,
                status=400,
            )

        with transaction.atomic():
            avaliador, created = Avaliador.objects.get_or_create(
                email=email,
                defaults={
                    'nome': nome,
                    'profissao': profissao,
                    'formulario': formulario,
                },
            )

            if not created:
                avaliador.nome = nome
                avaliador.profissao = profissao
                avaliador.formulario = formulario
                avaliador.save(update_fields=['nome', 'profissao', 'formulario'])

            for avaliacao_formulario in avaliacoes:
                avaliacao_formulario.avaliador = avaliador
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


# ═══════════════════════════════════════════════════════════════════
# PAINEL /admin-pondersec/  —  auth separada via tabela AdminPonderSec
# ═══════════════════════════════════════════════════════════════════

ADMIN_SESSION_KEY = "admin_pondersec_id"


def _get_admin_logado(request):
    admin_id = request.session.get(ADMIN_SESSION_KEY)
    if not admin_id:
        return None
    try:
        return AdminPonderSec.objects.get(id=admin_id, ativo=True)
    except AdminPonderSec.DoesNotExist:
        request.session.pop(ADMIN_SESSION_KEY, None)
        return None


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        admin = _get_admin_logado(request)
        if admin is None:
            if request.method != "GET" or request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"status": "erro", "mensagem": "Sessão de admin expirada."}, status=401)
            return redirect("admin_pondersec_login")
        request.admin_pondersec = admin
        return view_func(request, *args, **kwargs)
    return wrapper


@ensure_csrf_cookie
def admin_pondersec_login(request):
    if _get_admin_logado(request):
        return redirect("admin_pondersec_home")

    sem_admins = not AdminPonderSec.objects.exists()

    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        senha = request.POST.get("senha") or ""
        try:
            admin = AdminPonderSec.objects.get(email=email, ativo=True)
        except AdminPonderSec.DoesNotExist:
            admin = None

        if admin and admin.verificar_senha(senha):
            request.session[ADMIN_SESSION_KEY] = admin.id
            admin.registrar_acesso()
            return redirect("admin_pondersec_home")

        return render(request, "admin_pondersec/login.html", {
            "erro": "E-mail ou senha inválidos.",
            "email": email,
            "sem_admins": sem_admins,
        })

    return render(request, "admin_pondersec/login.html", {
        "sem_admins": sem_admins,
    })


def admin_pondersec_logout(request):
    request.session.pop(ADMIN_SESSION_KEY, None)
    return redirect("admin_pondersec_login")


@admin_required
def admin_pondersec_home(request):
    total_llms = LLMPublica.objects.count()
    llms_ativas = LLMPublica.objects.filter(ativo=True).count()
    total_metricas_publicas = Metrica.objects.filter(usuario__isnull=True).count()
    metricas_publicas_ativas = Metrica.objects.filter(usuario__isnull=True, ativa=True).count()
    total_perguntas_publicas = PerguntaPublica.objects.count()
    total_avaliacoes_publicas = AvaliacaoPublicaLLM.objects.filter(
        erro=False,
        avaliacao_quanti__isnull=False,
    ).count()
    return render(request, "admin_pondersec/home.html", {
        "admin": request.admin_pondersec,
        "total_llms": total_llms,
        "llms_ativas": llms_ativas,
        "total_metricas_publicas": total_metricas_publicas,
        "metricas_publicas_ativas": metricas_publicas_ativas,
        "total_perguntas_publicas": total_perguntas_publicas,
        "total_avaliacoes_publicas": total_avaliacoes_publicas,
    })


def _normalizar_pontuacao_publica(valor):
    try:
        pontos = int(valor)
    except (TypeError, ValueError):
        pontos = 5
    return max(2, min(pontos, 5))


@admin_required
def admin_pondersec_metricas_publicas(request):
    if request.method == "POST":
        nome = (request.POST.get("nome") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        criterio_texto = (request.POST.get("criterio_texto") or "").strip()
        pontuacao_maxima = _normalizar_pontuacao_publica(request.POST.get("pontuacao_maxima"))

        if not nome:
            django_messages.error(request, "Nome da métrica é obrigatório.")
            return redirect("admin_pondersec_metricas_publicas")

        Metrica.objects.create(
            usuario=None,
            nome=nome,
            descricao=descricao,
            tipo="quantitativa",
            pontuacao_maxima=pontuacao_maxima,
            criterio_texto=criterio_texto,
            ativa=True,
        )
        django_messages.success(request, f"Métrica pública '{nome}' criada.")
        return redirect("admin_pondersec_metricas_publicas")

    metricas = Metrica.objects.filter(usuario__isnull=True).order_by("-id")
    return render(request, "admin_pondersec/metricas_publicas.html", {
        "admin": request.admin_pondersec,
        "metricas": metricas,
    })


@admin_required
@require_http_methods(["PUT"])
def admin_pondersec_metrica_publica_editar(request, id):
    try:
        dados = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "erro", "mensagem": "JSON inválido."}, status=400)

    metrica = get_object_or_404(Metrica, id=id, usuario__isnull=True)
    nome = (dados.get("nome") or "").strip()
    descricao = (dados.get("descricao") or "").strip()
    criterio_texto = (dados.get("criterio_texto") or "").strip()

    if not nome:
        return JsonResponse({"status": "erro", "mensagem": "Nome da métrica é obrigatório."}, status=400)

    metrica.nome = nome
    metrica.descricao = descricao
    metrica.criterio_texto = criterio_texto
    metrica.pontuacao_maxima = _normalizar_pontuacao_publica(dados.get("pontuacao_maxima"))
    if isinstance(dados.get("ativa"), bool):
        metrica.ativa = dados["ativa"]
    metrica.save()
    return JsonResponse({"status": "ok", "mensagem": "Métrica atualizada."})


@admin_required
@require_http_methods(["DELETE"])
def admin_pondersec_metrica_publica_deletar(request, id):
    deleted, _ = Metrica.objects.filter(id=id, usuario__isnull=True).delete()
    if not deleted:
        return JsonResponse({"status": "erro", "mensagem": "Métrica não encontrada."}, status=404)
    return JsonResponse({"status": "ok", "mensagem": "Métrica removida."})


@admin_required
@require_http_methods(["POST"])
def admin_pondersec_metrica_publica_toggle(request, id):
    try:
        metrica = Metrica.objects.get(id=id, usuario__isnull=True)
    except Metrica.DoesNotExist:
        return JsonResponse({"status": "erro", "mensagem": "Métrica não encontrada."}, status=404)
    metrica.ativa = not metrica.ativa
    metrica.save(update_fields=["ativa"])
    return JsonResponse({"status": "ok", "ativa": metrica.ativa})


@admin_required
def admin_pondersec_avaliacoes_publicas(request):
    metricas = list(
        Metrica.objects
        .filter(usuario__isnull=True, ativa=True)
        .order_by("id")
        .values("id", "nome", "pontuacao_maxima")
    )
    llms = list(
        LLMPublica.objects
        .order_by("nome")
        .values("id", "nome", "ativo")
    )
    agregados = {
        (item["metrica_id"], item["resposta__llm_id"]): item
        for item in (
            AvaliacaoPublicaLLM.objects
            .filter(erro=False, avaliacao_quanti__isnull=False)
            .values("metrica_id", "resposta__llm_id")
            .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        )
    }

    por_metrica = []
    for metrica in metricas:
        linhas = []
        for llm in llms:
            item = agregados.get((metrica["id"], llm["id"]), {"media": None, "total": 0})
            linhas.append({
                "llm": llm["nome"],
                "ativa": llm["ativo"],
                "media": round(item["media"], 2) if item["media"] is not None else None,
                "total": item["total"],
            })
        por_metrica.append({
            "nome": metrica["nome"],
            "max": metrica["pontuacao_maxima"] or 5,
            "linhas": linhas,
        })

    ranking = list(
        AvaliacaoPublicaLLM.objects
        .filter(erro=False, avaliacao_quanti__isnull=False, resposta__llm__isnull=False)
        .values("resposta__llm__nome")
        .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        .order_by("-media", "resposta__llm__nome")[:10]
    )
    ultimas_perguntas = (
        PerguntaPublica.objects
        .annotate(
            respostas_total=Count("respostas", distinct=True),
            avaliacoes_total=Count(
                "respostas__avaliacoes_cruzadas",
                filter=Q(
                    respostas__avaliacoes_cruzadas__erro=False,
                    respostas__avaliacoes_cruzadas__avaliacao_quanti__isnull=False,
                ),
            ),
        )
        .order_by("-criado_em")[:10]
    )
    media_geral = (
        AvaliacaoPublicaLLM.objects
        .filter(erro=False, avaliacao_quanti__isnull=False)
        .aggregate(media=Avg("avaliacao_quanti"))
        .get("media")
    )
    avaliacoes_detalhadas_qs = (
        AvaliacaoPublicaLLM.objects
        .filter(erro=False, avaliacao_quanti__isnull=False)
        .select_related("juiz", "resposta__llm", "resposta__pergunta", "metrica")
        .order_by("-atualizado_em")[:80]
    )
    avaliacoes_detalhadas = []
    for avaliacao in avaliacoes_detalhadas_qs:
        maximo = _metric_max(avaliacao.metrica) if avaliacao.metrica else 5
        avaliacoes_detalhadas.append({
            "pergunta": avaliacao.resposta.pergunta.conteudo,
            "modelo_respondente": avaliacao.resposta.llm.nome if avaliacao.resposta.llm else "LLM removida",
            "modelo_avaliador": avaliacao.juiz.nome if avaliacao.juiz else "LLM avaliadora removida",
            "metrica": avaliacao.metrica.nome if avaliacao.metrica else "Métrica removida",
            "nota": avaliacao.avaliacao_quanti,
            "max": maximo,
            "justificativa": _formatar_justificativa_avaliacao(
                avaliacao.avaliacao_quali or avaliacao.justificativa_geral,
                avaliacao.avaliacao_quanti,
                maximo,
            ),
            "atualizado_em": avaliacao.atualizado_em,
        })

    return render(request, "admin_pondersec/avaliacoes_publicas.html", {
        "admin": request.admin_pondersec,
        "metricas": metricas,
        "llms": llms,
        "por_metrica": por_metrica,
        "ranking": ranking,
        "avaliacoes_detalhadas": avaliacoes_detalhadas,
        "ultimas_perguntas": ultimas_perguntas,
        "total_perguntas": PerguntaPublica.objects.count(),
        "total_respostas": RespostaPublica.objects.count(),
        "total_avaliacoes": AvaliacaoPublicaLLM.objects.filter(erro=False, avaliacao_quanti__isnull=False).count(),
        "respostas_avaliadas": AvaliacaoPublicaLLM.objects.filter(erro=False, avaliacao_quanti__isnull=False).values("resposta_id").distinct().count(),
        "media_geral": round(media_geral, 2) if media_geral is not None else None,
    })


@admin_required
def admin_pondersec_llms_publicas(request):
    if request.method == "POST":
        nome = (request.POST.get("model") or "").strip()
        provedor = (request.POST.get("provider") or "").strip()
        api_key = (request.POST.get("apiKey") or "").strip()

        if not nome or not api_key:
            django_messages.error(request, "Nome do modelo e API key são obrigatórios.")
            return redirect("admin_pondersec_llms_publicas")

        LLMPublica.objects.create(
            nome=nome,
            descricao=provedor,
            api_key=api_key,
            criado_por=request.admin_pondersec,
        )
        django_messages.success(request, f"LLM pública '{nome}' configurada.")
        return redirect("admin_pondersec_llms_publicas")

    llms = LLMPublica.objects.all().order_by("-id")
    return render(request, "admin_pondersec/llms_publicas.html", {
        "admin": request.admin_pondersec,
        "llms": llms,
    })


@admin_required
@require_http_methods(["DELETE"])
def admin_pondersec_llm_publica_deletar(request, id):
    deleted, _ = LLMPublica.objects.filter(id=id).delete()
    if not deleted:
        return JsonResponse({"status": "erro", "mensagem": "LLM não encontrada."}, status=404)
    return JsonResponse({"status": "ok", "mensagem": "LLM removida."})


@admin_required
@require_http_methods(["PUT"])
def admin_pondersec_llm_publica_editar(request, id):
    try:
        dados = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "erro", "mensagem": "JSON inválido."}, status=400)

    try:
        llm = LLMPublica.objects.get(id=id)
    except LLMPublica.DoesNotExist:
        return JsonResponse({"status": "erro", "mensagem": "LLM não encontrada."}, status=404)

    nome = (dados.get("nome") or "").strip()
    api_key = (dados.get("api_key") or "").strip()
    descricao = dados.get("descricao")
    ativo = dados.get("ativo")

    if not nome or not api_key:
        return JsonResponse({"status": "erro", "mensagem": "Nome e API key são obrigatórios."}, status=400)

    llm.nome = nome
    llm.api_key = api_key
    if descricao is not None:
        llm.descricao = descricao.strip()
    if isinstance(ativo, bool):
        llm.ativo = ativo
    llm.save()
    return JsonResponse({"status": "ok", "mensagem": "LLM atualizada."})


@admin_required
@require_http_methods(["POST"])
def admin_pondersec_llm_publica_toggle(request, id):
    try:
        llm = LLMPublica.objects.get(id=id)
    except LLMPublica.DoesNotExist:
        return JsonResponse({"status": "erro", "mensagem": "LLM não encontrada."}, status=404)
    llm.ativo = not llm.ativo
    llm.save(update_fields=["ativo"])
    return JsonResponse({"status": "ok", "ativo": llm.ativo})
