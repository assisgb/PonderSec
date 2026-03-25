from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from google import genai
import os
from groq import Groq
from datetime import timedelta
import re
from django.http import HttpResponseServerError, JsonResponse, HttpResponse
import json
from responsegenerator.models import Historico, Categoria, LLM, Questao, Resposta,  Formulario
from django.views.decorators.http import require_POST
from openai import OpenAI
import threading

def salvar_no_historico(user, pergunta, resposta):
    resp_obj = Resposta.objects.create(conteudo_resposta=resposta)
    q_obj = Questao.objects.create(conteudo=pergunta, respostas=resp_obj)
    
    Historico.objects.create(
        usuario=user,
        questao=q_obj
    )

@login_required
def menu(request):
    return render(request, 'menu.html')

@login_required
def deletar_item_historico(request, id):
    item = get_object_or_404(Historico, id=id, usuario=request.user)
    if request.method == 'POST':
        item.delete()
    return redirect('historico')

@login_required
def ver_detalhes(request, id):
    item = get_object_or_404(Historico, id=id, usuario=request.user)
    data_formatada = item.data.strftime('%d/%m/%Y %H:%M') if getattr(item, 'data', None) else ''

    respostas_encontradas = []
    pergunta_texto = "Conteúdo da questão não encontrado."

    if item.questao:
        pergunta_texto = item.questao.conteudo

        if getattr(item.questao, 'respostas', None) and item.questao.respostas.conteudo_resposta:
            conteudo_resp = item.questao.respostas.conteudo_resposta
            
            if item.questao.llm:
                nome_ia = item.questao.llm.nome
                nome_ia_lower = nome_ia.lower()
                
                if 'gemini' in nome_ia_lower: cor = '#4285F4'
                elif 'groq' in nome_ia_lower or 'llama' in nome_ia_lower: cor = '#f55036'
                elif 'chatgpt' in nome_ia_lower or 'gpt' in nome_ia_lower: cor = '#10a37f'
                else: cor = '#00ff9f'

                respostas_encontradas.append({
                    'ia': nome_ia, 
                    'texto': conteudo_resp, 
                    'cor': cor
                })
            
            else:
                if "[Gemini]" in conteudo_resp and "[Groq]" in conteudo_resp:
                    partes = conteudo_resp.split("[Groq]")
                    texto_gemini = partes[0].replace("[Gemini]", "").strip()
                    texto_groq = partes[1].strip()
                    
                    respostas_encontradas.append({'ia': 'Gemini', 'texto': texto_gemini, 'cor': '#4285F4'})
                    respostas_encontradas.append({'ia': 'Groq', 'texto': texto_groq, 'cor': '#f55036'})
                else:
                    respostas_encontradas.append({'ia': 'Geral', 'texto': conteudo_resp, 'cor': '#8ba1b0'})

    return JsonResponse({
        'pergunta': pergunta_texto,
        'data': data_formatada,
        'respostas': respostas_encontradas
    })

@login_required
def limpar_historico(request):
    if request.method == 'POST':
        Historico.objects.filter(usuario=request.user).delete()
    return redirect('historico')

@login_required
def consulta(request):
    resposta_gemini_formatada = ""
    resposta_groq_formatada = ""
    pergunta_usuario = ""
    texto_gemini_limpo = ""
    texto_groq_limpo = ""
    contexto = (
        "Irei lhe enviar uma série de perguntas no contexto de cibersegurança.\n"
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

    if request.method == 'POST':
        pergunta_usuario = request.POST.get('consulta', '').strip()
        if pergunta_usuario:
            
            ultima_interacao = Historico.objects.filter(usuario=request.user).select_related('questao').order_by('-data').first()
            
            if ultima_interacao and ultima_interacao.questao and ultima_interacao.questao.conteudo == pergunta_usuario:
                print("🚫 Duplicação detectada! Recuperando resposta do banco sem chamar IAs.")
                texto_salvo = ultima_interacao.questao.respostas.conteudo_resposta if ultima_interacao.questao.respostas else ""
                
                if "[Gemini]" in texto_salvo and "[Groq]" in texto_salvo:
                    partes = texto_salvo.split("[Groq]")
                    gemini_salvo = partes[0].replace("[Gemini]", "").strip()
                    groq_salvo = partes[1].strip()
                else:
                    gemini_salvo = texto_salvo
                    groq_salvo = texto_salvo

                resposta_gemini_formatada = f"Pergunta: {pergunta_usuario}\n\nResposta (Recuperada): {gemini_salvo}"
                resposta_groq_formatada = f"Pergunta: {pergunta_usuario}\n\nResposta (Recuperada): {groq_salvo}"
                
                return render(request, 'consulta.html', {
                    'resposta_gemini': resposta_gemini_formatada,
                    'resposta_groq': resposta_groq_formatada
                })

            prompt_final = contexto + pergunta_usuario

            # ---------- Gemini ----------
            try:
                client_gemini = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
                resp_gem = client_gemini.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt_final
                )
                texto_gemini_limpo = resp_gem.text
                resposta_gemini_formatada = f"Resposta Gemini: {texto_gemini_limpo}"
            except Exception as e:
                texto_gemini_limpo = f"Erro no Gemini: {str(e)}"
                resposta_gemini_formatada = texto_gemini_limpo

            # ---------- Groq ----------
            try:
                client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))
                chat_completion = client_groq.chat.completions.create(
                    messages=[{"role": "user", "content": prompt_final}],
                    model="llama-3.3-70b-versatile",
                )
                texto_groq_limpo = chat_completion.choices[0].message.content
                resposta_groq_formatada = f"Resposta Groq: {texto_groq_limpo}"
            except Exception as e:
                texto_groq_limpo = f"Erro no Groq: {str(e)}"
                resposta_groq_formatada = texto_groq_limpo

            # Salva no histórico empacotando as duas respostas
            try:
                historico_qs = Historico.objects.filter(usuario=request.user).order_by('data')
                if historico_qs.count() >= 20:
                    historico_qs.first().delete()
                
                # Empacota em uma string estruturada
                conteudo_unificado = f"[Gemini]\n{texto_gemini_limpo}\n\n[Groq]\n{texto_groq_limpo}"
                resp_obj = Resposta.objects.create(conteudo_resposta=conteudo_unificado)
                q_obj = Questao.objects.create(conteudo=pergunta_usuario, respostas=resp_obj)
                
                Historico.objects.create(
                    usuario=request.user,
                    questao=q_obj
                )
                print("✅ Nova pergunta salva com sucesso.")
            except Exception as e:
                print(f"❌ Erro crítico ao salvar no banco: {e}")

    return render(request, 'consulta.html', {
        'resposta_gemini': resposta_gemini_formatada,
        'resposta_groq': resposta_groq_formatada
    })

@login_required(login_url='/login/')
def historico(request):
    historico = Historico.objects.filter(usuario=request.user).order_by('-data')
    return render(request, 'historico.html', {'historico': historico})

@login_required
def questoes(request):
    lista_questoes = Questao.objects.all().order_by('-id')
    lista_categorias = Categoria.objects.all()
    llms = LLM.objects.all()
    
    return render(request, 'questoes/questoes.html', {
        "historico": lista_questoes,
        "categorias": lista_categorias,
        "llms": llms
    })

@login_required
def add_questoes(request):
    if request.method == "POST":
        pergunta_texto = request.POST.get('pergunta')
        categoria_id = request.POST.get('categoria')
        
        if pergunta_texto:
            nova_questao = Questao(conteudo=pergunta_texto)
            
            if categoria_id and str(categoria_id).strip(): 
                try:
                    categoria = Categoria.objects.get(id=int(categoria_id))
                    nova_questao.categoria = categoria
                except (Categoria.DoesNotExist, ValueError):
                    pass
                    
            nova_questao.save()
            django_messages.success(request, "Questão adicionada com sucesso!")
        
    return redirect('questoes')
    
@login_required
def upload_perguntas(request):
    if request.method == "POST":
        arquivo = request.FILES.get("arquivo_upload")

        if arquivo:
            perguntas = []
            conteudo_texto = arquivo.read().decode("utf-8")
            
            for linha in conteudo_texto.split("\n"):
                match = re.search(r'PERGUNTA\s*:\s*"?(.+?)"?$', linha, re.IGNORECASE)
                
                if match:
                    texto_pergunta = match.group(1).strip()
                    perguntas.append(texto_pergunta)
                    Questao.objects.create(conteudo=texto_pergunta)
            
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

    questao = Questao.objects.prefetch_related("respostas__llm").get(id=questao_id)
    respostas = Resposta.objects.filter(questao_id=questao_id)
    lista_respostas = []

    for r in respostas.all():
        lista_respostas.append({
            "llm": r.llm.nome,
            "conteudo": r.conteudo_resposta
        })

    return JsonResponse({
        "questao": questao.conteudo,
        "respostas": lista_respostas
    })


@login_required
def verificar_respostas(request, questao_id):
    total = Resposta.objects.filter(questao_id=questao_id).count()

    # ajuste conforme quantas respostas você espera (3 nesse caso)
    pronto = total >= 3

    return JsonResponse({"pronto": pronto})




@login_required
def gerar_respostas(request, questao_id):
    questao = get_object_or_404(Questao, id=questao_id)

    context = (
        "Irei lhe enviar uma série de perguntas no contexto de cibersegurança.\n"
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

    prompt = context + questao.conteudo

    try:
        llm_groq   = LLM.objects.get(nome__iexact="Groq")
        llm_gemini = LLM.objects.get(nome__iexact="Gemini")
        llm_gpt    = LLM.objects.get(nome__iexact="ChatGPT")
    except LLM.DoesNotExist:
        return JsonResponse({"ok": False, "erro": "LLM não encontrada"})

    # (opcional) limpa respostas antigas
    Resposta.objects.filter(questao_id=questao_id).delete()

    # --- GROQ ---
    try:
        client_groq = Groq(api_key=llm_groq.api_key)
        chat_completion = client_groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        resposta_groq = chat_completion.choices[0].message.content
    except Exception as e:
        resposta_groq = f"Erro no Groq: {str(e)}"

    Resposta.objects.create(
        questao_id=questao_id,
        llm=llm_groq,
        conteudo_resposta=resposta_groq,
    )

    # --- GEMINI ---
    try:
        client = genai.Client(api_key=llm_gemini.api_key)
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt,
        )
        resposta_gemini = response.text
    except Exception as e:
        resposta_gemini = f"Erro no Gemini: {str(e)}"

    Resposta.objects.create(
        questao_id=questao_id,
        llm=llm_gemini,
        conteudo_resposta=resposta_gemini,
    )

    # --- GPT ---
    if(llm_gpt.api_key):
        client = OpenAI(api_key = llm_gpt.api_key)
        response = client.responses.create(
            model="gpt-5.4",
            input=prompt
        )

        
        Resposta.objects.create(
            questao_id=questao_id,
            llm=llm_gpt,
            conteudo_resposta= response.output_text,
        )
    else:
        Resposta.objects.create(
            questao_id=questao_id,
            llm=llm_gpt,
            conteudo_resposta="Resposta GPT ainda não implementada",
        )

    

   
    # só retorna quando tudo terminar
    return JsonResponse({"ok": True})


def verificar_respostas(request, questao_id):
    total = Resposta.objects.filter(questao_id=questao_id).count()
    pronto = total >= 3
    return JsonResponse({"pronto": pronto})

    
@require_POST
@login_required
def limpar_respostas(request):
    Resposta.objects.all().delete()
    return JsonResponse({"ok": True})


@login_required
def setup_llm(request):
    if request.method == "POST":
        nome = request.POST.get("model")
        provedor = request.POST.get("provider")
        api_key = request.POST.get("apiKey")

        LLM.objects.create(
            nome = nome,
            descricao = provedor,
            api_key = api_key
        )

    llms_cadastradas = LLM.objects.all()
    return render(request, 'setup/setup-llm.html',{"llms_cadastradas": llms_cadastradas})

@login_required
def setup_avaliacao(request):
    return render(request, 'setup/setup-avaliacao.html')

@login_required
def setup_configurar_llm(request):
    return render(request, 'setup/setup-configurar-llm.html')

@login_required
def setup_adicionar_metrica(request):
    return render(request, 'setup/setup-adicionar-metrica.html')

@login_required
def setup_configurar_metrica(request):
    return render(request, 'setup/setup-configurar-metrica.html')

@login_required
def deletar_llm(request, id):
    if request.method == "DELETE":
        LLM.objects.filter(id=id).delete()
        return JsonResponse({"status": "success", "id": id})
    return JsonResponse({"status": "error"})

def edit_llm_api(request, id):
    if request.method == "PUT":
        data = json.loads(request.body)
        nome = data.get("nome")
        api_key = data.get("api_key")

        llm = LLM.objects.get(id=id)
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
    questoes = Questao.objects.all()
    return render(request, 'consulta/executar-consulta.html', {"questoes":  questoes})

@login_required
def consulta_comparacao(request):
    return render(request, 'consulta/consulta-comparacao.html')

@login_required
def avaliacao(request):
    formularios = Formulario.objects.filter(criado_por=request.user)
    questoes_respondidas = Questao.objects.filter(respostas__isnull=False).distinct()
    return render(request, 'avaliacao/avaliacao_lista.html', {
        'formularios': formularios,
        'questoes_respondidas': questoes_respondidas
    })

@login_required
def avaliacao_respostas(request, formulario_id, questao_id):
    formulario = get_object_or_404(Formulario, id=formulario_id)
    questao = get_object_or_404(Questao, id=questao_id)
    respostas = Resposta.objects.filter(questao=questao)
    metricas = Metrica.objects.filter(ativa=True)
    

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
        questoes_ids = list(
            Resposta.objects.values_list('questao_id', flat=True).distinct()
        )



        formulario = Formulario.objects.create(
            nome=nome,
            criado_por=request.user
        )
        formulario.questoes.set(questoes_ids)
        formulario.save()
        return redirect('avaliacao')

    questoes_respondidas = Questao.objects.filter(respostas__isnull=False).distinct()
    return render(request, 'avaliacao/avaliacao_adicionar_formulario.html', {'questoes_respondidas': questoes_respondidas})


@login_required
def avaliacao_editar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, criado_por=request.user)
    questoes_respondidas = Questao.objects.filter(respostas__isnull=False).distinct()
    if request.method == 'POST':
        nome = request.POST.get('nome')
        questoes_ids = request.POST.getlist('questoes')
        formulario.nome = nome
        formulario.questoes.set(questoes_ids)
        formulario.save()
        return redirect('avaliacao')

    questoes = Questao.objects.all()
    return render(request, 'avaliacao/avaliacao_editar_formulario.html', {
        'formulario': formulario,
        'questoes_respondidas': questoes_respondidas
    })

@login_required
def avaliacao_deletar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, criado_por=request.user)
    if request.method == 'POST':
        formulario.delete()
    return redirect('avaliacao')