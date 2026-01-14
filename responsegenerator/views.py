from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib.auth.decorators import login_required
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
        pergunta_usuario = request.POST.get('pergunta', '').strip()

        if pergunta_usuario:
            ultima_interacao = Historico.objects.filter(usuario=request.user).order_by('-data').first()

            if ultima_interacao and ultima_interacao.pergunta == pergunta_usuario: # => Blindagem contra duplicação
                print("🚫 Duplicação detectada! Recuperando resposta do banco sem chamar IAs.")
                
                resposta_gemini_formatada = f"Pergunta: {pergunta_usuario}\n\nResposta (Recuperada): {ultima_interacao.resposta_gemini}"
                resposta_groq_formatada = f"Pergunta: {pergunta_usuario}\n\nResposta (Recuperada): {ultima_interacao.resposta_groq}"
                
                return render(request, 'perguntar.html', {
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

    #print(f"DEBUG -> Gemini: {len(resposta_gemini_formatada)} chars | Groq: {len(resposta_groq_formatada)} chars")
    return render(request, 'perguntar.html', {
        'resposta_gemini': resposta_gemini_formatada,
        'resposta_groq': resposta_groq_formatada
    })


@login_required(login_url='/login/') # => Garante que só usuários logados acessem o histórico
def historico(request):
    # Busque as perguntas do banco de dados do usuário logado
    historico = Historico.objects.filter(usuario=request.user).order_by('-data')

    return render(request, 'historico.html', {
        'historico': historico
    })
