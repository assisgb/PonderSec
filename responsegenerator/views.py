from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from google import genai
import os
from groq import Groq
from datetime import timedelta
from responsegenerator.models import HistoricoAntigo
from responsegenerator.models import Categoria
from responsegenerator.models import LLM
from responsegenerator.models import Questao
import re
from django.http import JsonResponse
from django.http import HttpResponse
import json

def salvar_no_historico(user, pergunta, resposta):
    logs = HistoricoAntigo.objects.filter(usuario=user).order_by('data')
    
    if logs.count() >= 20:
        logs.first().delete()
        
    HistoricoAntigo.objects.create(
        usuario=user,
        pergunta=pergunta,
        resposta_gemini=resposta,
        resposta_groq=resposta  
    )

@login_required
def menu(request):
    return render(request, 'menu.html')

@login_required
def deletar_item_historico(request, id):
    item = get_object_or_404(HistoricoAntigo, id=id, usuario=request.user)
    if request.method == 'POST':
        item.delete()
    return redirect('historico')

@login_required
def ver_detalhes(request, id):
    item = get_object_or_404(HistoricoAntigo, id=id, usuario=request.user)
    return render(request, 'detalhes_historico.html', {'item': item})

@login_required
def limpar_historico(request):
    if request.method == 'POST':
        HistoricoAntigo.objects.filter(usuario=request.user).delete()
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
            ultima_interacao = HistoricoAntigo.objects.filter(usuario=request.user).order_by('-data').first()
            if ultima_interacao and ultima_interacao.pergunta == pergunta_usuario:
                print("🚫 Duplicação detectada! Recuperando resposta do banco sem chamar IAs.")
                resposta_gemini_formatada = f"Pergunta: {pergunta_usuario}\n\nResposta (Recuperada): {ultima_interacao.resposta_gemini}"
                resposta_groq_formatada = f"Pergunta: {pergunta_usuario}\n\nResposta (Recuperada): {ultima_interacao.resposta_groq}"
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

            # Salva no histórico
            try:
                historico_qs = HistoricoAntigo.objects.filter(usuario=request.user).order_by('data')
                if historico_qs.count() >= 20:
                    historico_qs.first().delete()
                HistoricoAntigo.objects.create(
                    usuario=request.user,
                    pergunta=pergunta_usuario,
                    resposta_gemini=texto_gemini_limpo,
                    resposta_groq=texto_groq_limpo
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
    historico = HistoricoAntigo.objects.filter(usuario=request.user).order_by('-data')
    return render(request, 'historico.html', {'historico': historico})

# QUESTOES
@login_required
def questoes(request):
    questoes = Questao.objects.all()
    return render(request, 'questoes/questoes.html',{
        "questoes": questoes
    })

@login_required
def add_questoes(request):

    categorias = Categoria.objects.all()
    return render(request, 'questoes/add-questoes.html',{
     "categorias": categorias
    })

@login_required
def questoes_upload(request):
    return render(request, 'questoes/questoes-upload.html')

def upload_perguntas(request):
    print(request.method)
    if request.method == "POST":
        
        arquivo = request.FILES["file"]

        perguntas = []

        for linha in arquivo.read().decode("utf-8").split("\n"):
            match = re.search(r'PERGUNTA\s*:\s*"?(.+?)"?$', linha, re.IGNORECASE)
            
            if match:
                perguntas.append(match.group(1))
                Questao.objects.create(
                    conteudo = match.group(1),
                )
    return HttpResponse("Perguntas Importadas com sucesso")

                 

@login_required
def questoes_cadastro_categoria(request):
    if (request.method == "POST"):
        nome_categoria = request.POST.get("nome")
        descricao_categoria = request.POST.get("descricao")


        Categoria.objects.create(
            nome_categoria = nome_categoria,
            descricao_categoria = descricao_categoria


        )
        


    return render(request, 'questoes/questoes_cadastro_categoria.html')

# SETUP
@login_required
def setup(request):
    return render(request, 'setup/setup.html')

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

        return JsonResponse({
            "status": "success",
            "id": id
        })

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

        return JsonResponse({
            "status": "success"
        })

    return JsonResponse({"status": "error"})



# CONSULTA
@login_required
def menu_consulta(request):
    return render(request, 'consulta/menu-consulta.html')

@login_required
def executar_consulta(request):
    return render(request, 'consulta/executar-consulta.html')

@login_required
def consulta_comparacao(request):
    return render(request, 'consulta/consulta-comparacao.html')

# AVALIACAO
@login_required
def avaliacao(request):
    return render(request, 'avaliacao/avaliacao_lista.html')

@login_required
def avaliacao_respostas(request):
    return render(request, 'avaliacao/avaliacao_respostas.html')

@login_required
def avaliacao_adicionar_formulario(request):
    return render(request, 'avaliacao/avaliacao_adicionar_formulario.html')

@login_required
def avaliacao_editar_formulario(request, id):
    return render(request, 'avaliacao/avaliacao_editar_formulario.html')

@login_required
def avaliacao_deletar_formulario(request, id):
    return redirect('avaliacao')