from django.shortcuts import render
from openCHA.orchestrator import Orchestrator
from openCHA.tasks import BaseTask
from google import genai
import os
from groq import Groq
from django.contrib.auth.decorators import login_required
from responsegenerator.models import Historico

def salvar_no_historico(user, pergunta, resposta):
    # 1. Pega o histórico do usuário ordenado por data (antigo -> novo)
    logs = Historico.objects.filter(usuario=user).order_by('data')
    
    # 2. Se já tiver 20 ou mais, deleta o primeiro (o mais antigo)
    if logs.count() >= 20:
        logs.first().delete()
        
    # 3. Salva o novo registro
    Historico.objects.create(
        usuario=user,
        pergunta=pergunta,
        resposta_gemini=resposta,
        resposta_groq=resposta  
    )

@login_required
def pondersecoptions(request):
    return render(request, 'pondersecoptions.html')


@login_required
def perguntar(request):

    resposta_gemini = ""
    resposta_groq = ""
    pergunta_usuario = ""

    contexto = (
        "Irei lhe enviar uma série de perguntas no contexto de cibersegurança.\n"
        "Analise bem o questionamento e responda apenas nesse contexto.\n"
        "Qualquer pergunta fora desse contexto não deverá ser respondida.\n\n"
        "Obs: A saída vai ser formatada como texto normal, sem códigos ou marcações especiais.\n"
    )

    if request.method == 'POST':
        pergunta_usuario = request.POST.get('pergunta', '').strip()

        if pergunta_usuario:
            pergunta = contexto + pergunta_usuario

            # ---------- Gemini ----------
            try:
               
                client_gemini = genai.Client()
                response_gemini = client_gemini.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=pergunta,
                )

                resposta_gemini = (
                    f"Pergunta:\n{pergunta_usuario}\n\n"
                    f"Resposta Gemini:\n{response_gemini.text}"
                )

                salvar_no_historico(request.user, pergunta_usuario, resposta_gemini)

            except Exception as e:
                resposta_gemini = f"Erro: {str(e)}"

            # ---------- Groq ----------
            try:

                client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))

                chat_completion = client_groq.chat.completions.create(
                   messages=[{"role": "user", "content": pergunta}],
                    model="llama-3.3-70b-versatile",
                )

                resposta_groq = (
                    f"Pergunta:\n{pergunta_usuario}\n\n"
                    f"Resposta Groq:\n{chat_completion.choices[0].message.content}"
                )
                salvar_no_historico(request.user, pergunta_usuario, resposta_groq)
            
            except Exception as e:
                resposta_groq = f"Erro no OpenCHA: {str(e)}"
            
            registros_usuario = Historico.objects.filter(usuario=request.user, pergunta=pergunta_usuario)

            if registros_usuario.count() >= 20:
                registros_usuario.first().delete()

            Historico.objects.create(
                usuario=request.user,
                pergunta=pergunta_usuario,
                resposta_gemini=resposta_gemini,
                resposta_groq=resposta_groq
            )

    return render(request, 'perguntar.html', {
        'resposta_gemini': resposta_gemini,
        'resposta_groq': resposta_groq
    })


@login_required(login_url='/login/') # => Garante que só usuários logados acessem o histórico
def historico(request):
    # Busque as perguntas do banco de dados do usuário logado
    historico = Historico.objects.filter(usuario=request.user).order_by('-data')

    return render(request, 'historico.html', {
        'historico': historico
    })
