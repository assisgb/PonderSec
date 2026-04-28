from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from google import genai
import openai
import os
import re
from groq import Groq
from datetime import timedelta
from django.http import JsonResponse, HttpResponse
import json
from django.views.decorators.http import require_http_methods
from responsegenerator.models import Historico, Categoria, LLM, Questao, Resposta, Avaliacao, Metrica, Formulario, Avaliador, AvaliacaoFormulario
from django.db.models import Avg
from collections import defaultdict


def salvar_no_historico(user, pergunta, resposta):
    # BLINDADO: Garante que a Questao pertence ao usuário na criação automática
    q_obj = Questao.objects.create(conteudo=pergunta, usuario=user)  
    resp_obj = Resposta.objects.create(conteudo_resposta=resposta, questao=q_obj)

    Historico.objects.create(
        usuario=user,
        questao=q_obj
    )

@login_required
def menu(request):
    return render(request, 'menu.html')

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
        Historico.objects.filter(usuario=request.user).delete()
    return redirect('questoes')
 

@login_required(login_url='/login/')
def historico(request):
    historico = Historico.objects.filter(usuario=request.user).order_by('-data')
    return render(request, 'historico.html', {'historico': historico})

@login_required
def questoes(request):
    lista_questoes = Questao.objects.filter(usuario=request.user).select_related('categoria').order_by('-id').distinct()
    lista_categorias = Categoria.objects.filter(usuario=request.user)
    llms = LLM.objects.filter(usuario=request.user)
    formulario = Formulario.objects.filter(usuario=request.user)
    
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
            nome_categoria = "Geral"
            llm_ativo = LLM.objects.filter(usuario=request.user, ativo=True).first()
            
            if llm_ativo:
                prompt_classificacao = (
                    "Você é um classificador de dados estrito. Leia a pergunta abaixo "
                    "e responda APENAS com o nome de uma categoria de cibersegurança "
                    "(ex: Criptografia, Redes, Phishing, Engenharia Social). "
                    "NÃO use pontos finais, NÃO explique, NÃO escreva frases. "
                    "Apenas 1 ou 2 palavras definindo o tema.\n\n"
                    f"Pergunta: {pergunta_texto}"
                )

                provedor = llm_ativo.descricao.lower() if llm_ativo.descricao else ""

                try:
                    if "gemini" in provedor or "google" in provedor:
                        client = genai.Client(api_key=llm_ativo.api_key)
                        resp = client.models.generate_content(model=llm_ativo.nome, contents=prompt_classificacao)
                        nome_categoria = resp.text.strip().title()

                    elif "groq" in provedor:
                        client = Groq(api_key=llm_ativo.api_key)
                        chat_completion = client.chat.completions.create(
                            messages=[{"role": "user", "content": prompt_classificacao}],
                            model=llm_ativo.nome,
                        )
                        nome_categoria = chat_completion.choices[0].message.content.strip().title()

                    elif "openai" in provedor or "OpenAI" in provedor or "openAI" in provedor:
                        client = openai.OpenAI(api_key=llm_ativo.api_key)
                        response = client.chat.completions.create(
                            model=llm_ativo.nome,
                            messages=[{"role": "user", "content": prompt_classificacao}]
                        )
                        nome_categoria = response.choices[0].message.content.strip().title()

                    elif "deepseek" in provedor:
                        client = openai.OpenAI(
                            api_key=llm_ativo.api_key, 
                            base_url="https://api.deepseek.com"
                        )
                        response = client.chat.completions.create(
                            model=llm_ativo.nome, # Ex: "deepseek-chat" ou "deepseek-reasoner"
                            messages=[{"role": "user", "content": prompt_classificacao}]
                        )
                        nome_categoria = response.choices[0].message.content.strip().title()

                except Exception as e:
                    print(f"Erro ao classificar categoria com IA: {str(e)}")
                    nome_categoria = "Geral"

            nome_categoria = re.sub(r'[^\w\s]', '', nome_categoria)[:50]  
            categoria_obj, created = Categoria.objects.get_or_create(
                nome_categoria=nome_categoria,
                usuario=request.user,
                defaults={'descricao_categoria': f'Categoria gerada automaticamente: {nome_categoria}'}
            )

            Questao.objects.create(
                conteudo=pergunta_texto,
                usuario=request.user,
                categoria=categoria_obj,
            )

            django_messages.success(request, f"Questão adicionada com sucesso e classificada automaticamente como '{nome_categoria}'!")
        
    return redirect('questoes')
    
@login_required
def upload_perguntas(request):
    if request.method == "POST":
        arquivo = request.FILES.get("arquivo_upload")

        if arquivo:
            perguntas = []
            conteudo_texto = arquivo.read().decode("utf-8")
            nome_arquivo = arquivo.name.lower()

            categoria_geral, _ = Categoria.objects.get_or_create(
                nome_categoria="Geral",
                usuario=request.user,
                defaults={'descricao_categoria': 'Categoria importada via arquivo'}
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
                            Questao.objects.create(conteudo=texto_pergunta, usuario=request.user, categoria=categoria_geral)

                except (json.JSONDecodeError, AttributeError):
                    django_messages.error(request, "Arquivo JSON inválido ou mal formatado.")
                    return redirect('questoes')

            else:
                for linha in conteudo_texto.split("\n"):
                    match = re.search(r'PERGUNTA\s*:\s*"?(.+?)"?$', linha, re.IGNORECASE)

                    if match:
                        texto_pergunta = match.group(1).strip()
                        perguntas.append(texto_pergunta)
                        Questao.objects.create(conteudo=texto_pergunta, usuario=request.user, categoria=categoria_geral)

            if perguntas:
                django_messages.success(request, f"{len(perguntas)} perguntas importadas com sucesso!")
            else:
                django_messages.error(request, "Nenhuma pergunta encontrada no arquivo.")
        else:
            django_messages.error(request, "Nenhum arquivo foi enviado.")

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
            django_messages.success(request, f"Categoria '{nome_categoria}' criada!")
        else:
            django_messages.error(request, "O nome da categoria é obrigatório.")

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
    # BLINDADO
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
                    base_url="https://api.deepseek.com"
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
            
    return JsonResponse({"ok": False, "erro": "Método não permitido"}, status=405)
    
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
        redirect('setup_llm')

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

        if not nome:
            django_messages.error(request, 'O nome da métrica é obrigatório.')
            return redirect('setup_avaliacao')

        Metrica.objects.create(
            usuario          = request.user, # BLINDADO: Amarra a nova métrica ao usuário
            nome             = nome,
            descricao        = descricao,
            tipo             = tipo,
            pontuacao_maxima = int(pontuacao_maxima) if pontuacao_maxima else None,
            criterio_texto   = criterio_texto,
            ativa            = True,
        )
        return redirect('setup_avaliacao')

    return redirect('setup_avaliacao')


@login_required
def setup_configurar_metrica(request):
    if request.method == 'POST':
        metrica_id       = request.POST.get('metrica_id')
        nome             = request.POST.get('nome', '').strip()
        descricao        = request.POST.get('descricao', '').strip()
        tipo             = request.POST.get('tipo', 'quantitativa')
        pontuacao_maxima = request.POST.get('pontuacao_maxima')
        criterio_texto   = request.POST.get('criterio_texto', '').strip()

        if not metrica_id:
            django_messages.error(request, 'ID da métrica não informado.')
            return redirect('setup_avaliacao')

        # BLINDADO
        metrica = get_object_or_404(Metrica, id=metrica_id, usuario=request.user)
        metrica.nome             = nome
        metrica.descricao        = descricao
        metrica.tipo             = tipo
        metrica.pontuacao_maxima = int(pontuacao_maxima) if pontuacao_maxima else None
        metrica.criterio_texto   = criterio_texto
        metrica.save()

        return redirect('setup_avaliacao')

    return redirect('setup_avaliacao')

@login_required
@require_http_methods(["DELETE"])
def setup_deletar_metrica(request, id):
    try:
        # BLINDADO: Protegido com decorator login_required e filtrado pelo dono
        metrica = get_object_or_404(Metrica, id=id, usuario=request.user)
        metrica.delete()
        return JsonResponse({"status": "success"})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

@login_required
def deletar_llm(request, id):
    if request.method == "DELETE":
        LLM.objects.filter(id=id, usuario=request.user).delete()
        return JsonResponse({"status": "success", "id": id})
    return JsonResponse({"status": "error"})

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
        return JsonResponse({"status": "success"})
    return JsonResponse({"status": "error"})

@login_required
def menu_consulta(request):
    return render(request, 'consulta/menu-consulta.html')

@login_required
def executar_consulta(request):
    questoes = Questao.objects.prefetch_related('respostas').filter(usuario=request.user)
    return render(request, 'consulta/executar-consulta.html', {
        "questoes": questoes
    })

@login_required
def consulta_comparacao(request):
    return render(request, 'consulta/consulta-comparacao.html')

@login_required
def avaliacao(request):
    formularios = Formulario.objects.filter(usuario=request.user)
    questoes = Questao.objects.filter(usuario=request.user)
    return render(request, 'avaliacao/avaliacao_lista.html', {
        'formularios': formularios,
        'questoes': questoes
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
        formulario = Formulario.objects.create(
            nome=nome,
            usuario=request.user
        )
        formulario.questoes.set(questoes_ids)
        formulario.save()
        return redirect('avaliacao')

    questoes = Questao.objects.filter(usuario=request.user).order_by('-id')
    return render(request, 'avaliacao/avaliacao_adicionar_formulario.html', {'questoes': questoes})


@login_required
def avaliacao_editar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, usuario=request.user)

    if request.method == 'POST':
        nome = request.POST.get('nome')
        questoes_ids = request.POST.getlist('questoes')
        formulario.nome = nome
        formulario.questoes.set(questoes_ids)
        formulario.save()
        return redirect('avaliacao')

    questoes = Questao.objects.filter(usuario=request.user).order_by('-id')
    return render(request, 'avaliacao/avaliacao_editar_formulario.html', {
        'formulario': formulario,
        'questoes': questoes
    })

@login_required
def avaliacao_deletar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, usuario=request.user)
    if request.method == 'POST':
        formulario.delete()
    return redirect('avaliacao')

def responder_avaliacao_publica(request, formulario_id):
    # Aqui não vai request.user pois é uma rota pública para avaliadores externos (blind test)
    formulario = get_object_or_404(Formulario, id=formulario_id)
    metricas = Metrica.objects.filter(usuario=formulario.usuario, ativa=True)

    likert_options = [
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

        for chave, valor in request.POST.items():
            if chave.startswith('quanti_') and valor:
                partes = chave.split('_')
                resposta_id = partes[1]
                metrica_id = partes[2]
                texto_quali = request.POST.get(f'quali_{resposta_id}_{metrica_id}', '')

                AvaliacaoFormulario.objects.create(
                    usuario=formulario.usuario, # BLINDADO: Vincula a avaliação gerada ao dono do formulário
                    avaliador=avaliador,
                    resposta_id=resposta_id,
                    metrica_id=metrica_id,
                    avaliacao_quanti=valor,
                    avaliacao_quali=texto_quali
                )

        return render(request, 'avaliacao/avaliacao_sucesso.html')
    
    modo_cego = request.GET.get('blind') == 'true'

    contexto = {
        'formulario': formulario,
        'metricas': metricas,
        'likert_options': likert_options,
        'blind_mode': modo_cego,
    }
    return render(request, 'avaliacao/avaliacao_publica.html', contexto)


@login_required
def dashboard_avaliacoes(request):
    metricas = list(Metrica.objects.filter(usuario=request.user, ativa=True).values('id', 'nome', 'pontuacao_maxima'))
    
    llms = list(LLM.objects.filter(usuario=request.user).values('id', 'nome').order_by('-id'))
    
    dados = {}
    for metrica in metricas:
        dados_metrica = {}
        for llm in llms:
            media = AvaliacaoFormulario.objects.filter(
                metrica_id=metrica['id'],
                resposta__llm_id=llm['id'],
                avaliacao_quanti__isnull=False
            ).aggregate(media=Avg('avaliacao_quanti'))['media']
            
            dados_metrica[llm['nome']] = round(media, 2) if media is not None else None
        
        dados[metrica['nome']] = {
            'id': metrica['id'],
            'pontuacao_maxima': metrica['pontuacao_maxima'] or 5,
            'dados': dados_metrica
        }
    
    return render(request, 'avaliacao/dashboard_avaliacoes.html', {
        'metricas_json': json.dumps(dados),
        'llms_json': json.dumps([l['nome'] for l in llms]),
        'metricas_lista': metricas,
    })

@login_required
def menu_avaliacao(request):
    return render(request,"avaliacao/menu_avaliacao.html")