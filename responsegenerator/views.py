from django.shortcuts import render
from google import genai
import os
from groq import Groq
from django.contrib.auth.decorators import login_required
# Create your views here.
@login_required
def pondersecoptions(request):


    return render(request, 'pondersecoptions.html')

@login_required
def perguntar(request):

    resposta = None

    contexto = """Irei lhe enviar uma série de perguntas no contexto de cibersegurança.
Analise bem o questionamento e responda apenas nesse contexto.
Qualquer pergunta fora desse contexto não deverá ser respondida.

Obs: A saída vai ser formatada como texto normal, sem códigos ou marcações especiais.
"""

    client_gemini = genai.Client()
    client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))

    if request.method == 'POST':
        pergunta_usuario = request.POST.get('pergunta', '').strip()

        if pergunta_usuario:
            pergunta = contexto + pergunta_usuario

            # Gemini
            response_gemini = client_gemini.models.generate_content(
                model="gemini-2.5-flash",
                contents=pergunta,
            )

            # Groq
            chat_completion = client_groq.chat.completions.create(
                messages=[
                    {"role": "user", "content": pergunta}
                ],
                model="llama-3.3-70b-versatile",
            )

            resposta = f"""
Pergunta:
{pergunta_usuario}

Resposta Gemini:
{response_gemini.text}

Resposta Groq:
{chat_completion.choices[0].message.content}
"""

    return render(request, 'perguntar.html', {
        'resposta': resposta
    })




@login_required
def historico(request):
    if not request.user.is_authenticated:
            return render(request, 'login.html')
    return render(request, 'historico.html')
