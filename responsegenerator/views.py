from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
#from openCHA.orchestrator import Orchestrator
#from openCHA.tasks import BaseTask
from google import genai
import os
from groq import Groq
from datetime import timedelta
from responsegenerator.models import Historico

def salvar_no_historico(user, pergunta, resposta):
    logs = Historico.objects.filter(usuario=user).order_by('data')
    
    if logs.count() >= 20:
        logs.first().delete()
        
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
def deletar_item_historico(request, id):
    # Busca o item pelo ID, mas SÓ se ele pertencer ao usuário logado (Segurança máxima)
    item = get_object_or_404(Historico, id=id, usuario=request.user)
    
    # Só deleta se for uma requisição POST (padrão de segurança para não deletar via link direto)
    if request.method == 'POST':
        item.delete()
        
    return redirect('historico')

@login_required
def ver_detalhes(request, id):
    item = get_object_or_404(Historico, id=id, usuario=request.user)
    
    return render(request, 'detalhes_historico.html', {
        'item': item
    })

@login_required
def limpar_historico(request):
    if request.method == 'POST':
        Historico.objects.filter(usuario=request.user).delete()
    return redirect('historico')

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
            ultima_interacao = Historico.objects.filter(usuario=request.user).order_by('-data').first()

            # 2. Se a última pergunta for IGUAL à nova, é uma duplicação (F5 ou clique duplo)
            if ultima_interacao and ultima_interacao.pergunta == pergunta_usuario:
                print("🚫 Duplicação detectada! Recuperando resposta do banco sem chamar IAs.")
                
                # Para o usuário não achar que falhou, mostramos a resposta que já estava salva
                resposta_gemini_formatada = f"Pergunta:\n{pergunta_usuario}\n\nResposta (Recuperada):\n{ultima_interacao.resposta_gemini}"
                resposta_groq_formatada = f"Pergunta:\n{pergunta_usuario}\n\nResposta (Recuperada):\n{ultima_interacao.resposta_groq}"
                
                # RETORNAMOS AQUI. Não chama IA, não salva nada novo.
                return render(request, 'perguntar.html', {
                    'resposta_gemini': resposta_gemini_formatada,
                    'resposta_groq': resposta_groq_formatada
                })
                texto_gemini_limpo = ""
                texto_groq_limpo = ""
            prompt_final = contexto + pergunta_usuario

            # ---------- Gemini ----------
            try:
                #orchestrator = Orchestrator(
                #    planner_model="gemini-2.5-flash",
                #    planner_api_key=os.environ.get("GOOGLE_API_KEY"),
                #)
                #resposta_gemini = orchestrator.run(query=pergunta)
                #txt_limpo = str(resposta_gemini)

                #salvar_no_historico(request.user, pergunta_usuario, txt_limpo)

                #resposta_gemini = txt_limpo

                client_gemini = genai.Client()
                resp_gem = client_gemini.models.generate_content(
                    model="gemini-2.5-flash", contents=prompt_final
                )
                texto_gemini_limpo = resp_gem.text
                resposta_gemini_formatada = f"Pergunta:\n{pergunta_usuario}\n\nResposta Gemini:\n{texto_gemini_limpo}"
            except Exception as e:
                texto_gemini_limpo = f"Erro no Gemini: {str(e)}"
                resposta_gemini_formatada = texto_gemini_limpo
            #except Exception as e:
            #    resposta_gemini = f"Erro no OpenCHA: {str(e)}"

            # ---------- Groq ----------
            try:
                #orchestrator = Orchestrator(
                #    planner_model="llama-3.3-70b-versatile",
                #    planner_api_key=os.environ.get("GROQ_API_KEY"),
                #)
                #resposta_groq = orchestrator.run(query=pergunta)
                #txt_limpo = str(resposta_groq)

                #salvar_no_historico(request.user, pergunta_usuario, txt_limpo)
                #resposta_groq = txt_limpo

                client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))
                chat_completion = client_groq.chat.completions.create(
                    messages=[{"role": "user", "content": prompt_final}],
                    model="llama-3.3-70b-versatile",
                )
                texto_groq_limpo = chat_completion.choices[0].message.content
                resposta_groq_formatada = f"Pergunta:\n{pergunta_usuario}\n\nResposta Groq:\n{texto_groq_limpo}"
            except Exception as e:
                texto_groq_limpo = f"Erro no Groq: {str(e)}"
                resposta_groq_formatada = texto_groq_limpo

            #except Exception as e:
            #    resposta_groq = f"Erro no OpenCHA: {str(e)}"

            try:
                # Remove o mais antigo se tiver 20
                historico_qs = Historico.objects.filter(usuario=request.user).order_by('data')
                if historico_qs.count() >= 20:
                    historico_qs.first().delete()

                Historico.objects.create(
                    usuario=request.user,
                    pergunta=pergunta_usuario,
                    resposta_gemini=texto_gemini_limpo,
                    resposta_groq=texto_groq_limpo
                )
                print("✅ Nova pergunta salva com sucesso.")
                
            except Exception as e:
                print(f"❌ Erro crítico ao salvar no banco: {e}")

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
