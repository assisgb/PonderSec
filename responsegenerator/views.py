from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from google import genai
import os
import re
from groq import Groq
from datetime import timedelta
import re
from django.http import JsonResponse, HttpResponse
import json
from responsegenerator.models import Historico, Categoria, LLM, Questao, Resposta, Avaliacao, Metrica, Formulario, Avaliador, AvaliacaoFormulario

def salvar_no_historico(user, pergunta, resposta):
    resp_obj = Resposta.objects.create(conteudo_resposta=resposta)
    q_obj = Questao.objects.create(conteudo=pergunta)  # removido respostas=resp_obj
    resp_obj.questao = q_obj  # associa pelo ForeignKey correto
    resp_obj.save()

    Historico.objects.create(
        usuario=user,
        questao=q_obj
    )

@login_required
def menu(request):
    return render(request, 'menu.html')

@login_required
def deletar_questao_historico(request, id):
    item = get_object_or_404(Questao, id=id)
    if request.method == 'POST':
        item.delete()
    return redirect('questoes')

@login_required
def ver_detalhes_questao(request, id):
    try:
        # Busca direto na tabela de Questões (não mais no Histórico)
        questao = get_object_or_404(Questao, id=id)

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
        Questao.objects.all().delete()
    return redirect('questoes')

@login_required
def consulta(request):
    pergunta_usuario = ""
    resultados_ias = []  # Lista genérica que vai para o template
    
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
                
                blocos_salvos = re.split(r'\[(.*?)\]', texto_salvo)
                
                # Ignora o primeiro item se for vazio (o split deixa o que vem antes do primeiro colchete)
                for i in range(1, len(blocos_salvos), 2):
                    nome_modelo = blocos_salvos[i]
                    texto_recuperado = blocos_salvos[i+1].strip()
                    
                    resultados_ias.append({
                        'modelo': nome_modelo,
                        'resposta_ia_limpa': texto_recuperado,
                        'status': 'Recuperado do Banco'
                    })
                
                return render(request, 'consulta.html', {
                    'resultados_ias': resultados_ias,
                    'pergunta': pergunta_usuario
                })

            prompt_final = contexto + pergunta_usuario

            llms_ativos = LLM.objects.filter(ativo=True)
            conteudo_unificado_banco = ""
            
            for llm in llms_ativos:
                texto_ia_limpa = ""
                provedor = llm.descricao.lower() if llm.descricao else ""

                try:
                    # Lógica para modelos do Google (Gemini)
                    if "gemini" in provedor or "google" in provedor:
                        client = genai.Client(api_key=llm.api_key)
                        resp = client.models.generate_content(model=llm.nome, contents=prompt_final)
                        texto_ia_limpa = resp.text

                    # Lógica para modelos da Groq (Llama, Mixtral, etc)
                    elif "groq" in provedor:
                        client = Groq(api_key=llm.api_key)
                        chat_completion = client.chat.completions.create(
                            messages=[{"role": "user", "content": prompt_final}],
                            model=llm.nome,
                        )
                        texto_ia_limpa = chat_completion.choices[0].message.content
                    
                    # Você pode adicionar OpenAI, Anthropic, etc., aqui depois seguindo a mesma estrutura
                    else:
                        texto_ia_limpa = f"Provedor '{llm.descricao}' não implementado no backend."

                except Exception as e:
                    texto_ia_limpa = f"Erro ao contatar API do {llm.nome}: {str(e)}"
                
                # Adiciona o resultado na lista genérica que vai para o Front
                resultados_ias.append({
                    'modelo': llm.nome,
                    'resposta_ia_limpa': texto_ia_limpa,
                    'status': 'Gerado Agora'
                })
                
                # Concatena para salvar no banco (Formato: [NomeDoModelo]\nResposta)
                conteudo_unificado_banco += f"[{llm.nome}]\n{texto_ia_limpa}\n\n"

            try:
                historico_qs = Historico.objects.filter(usuario=request.user).order_by('data')
                if historico_qs.count() >= 20:
                    historico_qs.first().delete()
                
                resp_obj = Resposta.objects.create(conteudo_resposta=conteudo_unificado_banco.strip())
                q_obj = Questao.objects.create(conteudo=pergunta_usuario, respostas=resp_obj)
                
                Historico.objects.create(
                    usuario=request.user,
                    questao=q_obj
                )
                print("✅ Nova pergunta salva com sucesso.")
            except Exception as e:
                print(f"❌ Erro crítico ao salvar no banco: {e}")

    return render(request, 'consulta.html', {
        'resultados_ias': resultados_ias,
        'pergunta': pergunta_usuario
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
    formulario = Formulario.objects.all()
    
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
    questao = get_object_or_404(
        Questao.objects.prefetch_related("respostas__llm"), 
        id=questao_id
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
    questao = get_object_or_404(Questao, id=questao_id)
    llms_ativos = LLM.objects.filter(ativo=True)
    
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
            
            else:
                texto_ia_limpa = f"Provedor '{llm.descricao}' não reconhecido para execução automática."

        except Exception as e:
            texto_ia_limpa = f"Erro na IA {llm.nome}: {str(e)}"

        Resposta.objects.create( # => Salva a resposta real atrelada àquela questão e àquele modelo específico
            questao_id=questao_id,
            llm=llm,
            conteudo_resposta=texto_ia_limpa.strip()
        )

    return JsonResponse({'status': 'ok'})

def limpar_respostas(request):
    if request.method == "POST":
        try:
            Resposta.objects.all().delete()
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
            nome = nome,
            descricao = provedor,
            api_key = api_key
        )
        redirect('setup_llm')

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
    questoes = Questao.objects.all()
    return render(request, 'avaliacao/avaliacao_lista.html', {
        'formularios': formularios,
        'questoes': questoes
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
        questoes_ids = request.POST.getlist('questoes')
        formulario = Formulario.objects.create(
            nome=nome,
            criado_por=request.user
        )
        formulario.questoes.set(questoes_ids)
        formulario.save()
        return redirect('avaliacao')

    questoes = Questao.objects.all()
    return render(request, 'avaliacao/avaliacao_adicionar_formulario.html', {'questoes': questoes})


@login_required
def avaliacao_editar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, criado_por=request.user)

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
        'questoes': questoes
    })

@login_required
def avaliacao_deletar_formulario(request, id):
    formulario = get_object_or_404(Formulario, id=id, criado_por=request.user)
    if request.method == 'POST':
        formulario.delete()
    return redirect('avaliacao')

def responder_avaliacao_publica(request, formulario_id):
    formulario = get_object_or_404(Formulario, id=formulario_id)
    metricas = Metrica.objects.filter(ativa=True)

    if request.method == 'POST':
        nome = request.POST.get('nome')
        email = request.POST.get('email')
        profissao = request.POST.get('profissao')

        avaliador = Avaliador.objects.create(
            nome=nome,
            email=email,
            profissao=profissao,
            formulario=formulario
        )

        for chave, valor in request.POST.items():
            if chave.startswith('quanti_') and valor:
                partes = chave.split('_')
                resposta_id = partes[1]
                metrica_id = partes[2]

                texto_quali = request.POST.get(f'quali_{resposta_id}_{metrica_id}', '')

                AvaliacaoFormulario.objects.create(
                    avaliador=avaliador,
                    resposta_id=resposta_id,
                    metrica_id=metrica_id,
                    avaliacao_quanti=valor,
                    avaliacao_quali=texto_quali
                )
            return render(request, 'avaliacao/avaliacao_sucesso.html')
    
    contexto = {
        'formulario': formulario,
        'metricas': metricas
    }
    return render(request, 'avaliacao/avaliacao_publica.html', contexto)