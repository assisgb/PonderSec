from django.shortcuts import render
from google import genai
import os
from groq import Groq
from django.contrib.auth.decorators import login_required


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

            except Exception as e:
                resposta_gemini = "Erro ao obter resposta do Gemini."

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

            except Exception as e:
                resposta_groq = "Erro ao obter resposta do Groq."

    return render(request, 'perguntar.html', {
        'resposta_gemini': resposta_gemini,
        'resposta_groq': resposta_groq
    })


@login_required
def historico(request):
    # Busque as perguntas do banco de dados do usuário logado
    historico = None

    return render(request, 'historico.html', {
        'historico': historico
    })
