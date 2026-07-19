from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
from django.db import close_old_connections, transaction
from datetime import timedelta
import hashlib
import logging
import re
import uuid
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse
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
from django.db.models import Avg, Count, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Lower
from concurrent.futures import as_completed
from responsegenerator.executors import get_llm_executor
from responsegenerator.judgeai_metrics import (
    JUDGE_METRIC_KEYS,
    JUDGE_METRIC_NAMES,
    ensure_judge_metrics,
    judge_metric_key,
    normalize_metric_name,
)
from responsegenerator.llm_client import (
    LLMServiceError,
    call_configured_llm,
    stream_configured_llm,
)


logger = logging.getLogger(__name__)
PUBLIC_EVALUATION_TOKEN_SALT = "responsegenerator.public-evaluation"
PUBLIC_EVALUATION_CLAIM_TTL = timedelta(minutes=4)


def _public_api_rate_limit(request, scope, setting_name="PUBLIC_CHAT_RATE_LIMIT"):
    """Limite curto por origem para impedir que rajadas saturem as APIs pagas."""
    limit = max(1, int(getattr(settings, setting_name, 30)))
    window = max(1, int(getattr(settings, "PUBLIC_CHAT_RATE_WINDOW_SECONDS", 60)))
    origin = request.META.get("REMOTE_ADDR") or "unknown"
    if getattr(settings, "PUBLIC_RATE_TRUST_X_REAL_IP", False):
        # Use somente quando um proxy confiável sobrescreve este cabeçalho.
        origin = request.META.get("HTTP_X_REAL_IP") or origin
    digest = hashlib.sha256(f"{scope}:{origin}".encode("utf-8")).hexdigest()
    key = f"pondersec:public-api:{digest}"
    if cache.add(key, 1, timeout=window):
        return None
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)
        return None
    if count <= limit:
        return None

    response = JsonResponse({
        "status": "erro",
        "mensagem": "Muitas solicitações em pouco tempo. Aguarde antes de tentar novamente.",
    }, status=429)
    response["Retry-After"] = str(window)
    return response


def _api_key_hint(api_key):
    """Identifica uma chave persistida sem expor o segredo completo."""
    clean_key = (api_key or "").strip()
    if not clean_key:
        return ""
    return f"••••{clean_key[-4:]}"


def _api_key_fingerprint(api_key):
    clean_key = (api_key or "").strip()
    if not clean_key:
        return "missing"
    return hashlib.sha256(clean_key.encode("utf-8")).hexdigest()[:10]


def _call_llm_in_worker(llm, prompt):
    """Isola conexões Django criadas pelas tarefas paralelas de provedores externos."""
    close_old_connections()
    try:
        return _judgeai_call_configured_llm(llm, prompt)
    finally:
        close_old_connections()


def salvar_no_historico(user, pergunta, resposta):
    q_obj = Questao.objects.create(conteudo=pergunta, usuario=user)  
    resp_obj = Resposta.objects.create(conteudo_resposta=resposta, questao=q_obj)

    Historico.objects.create(
        usuario=user,
        questao=q_obj
    )


# ===== VIEWS PÚBLICAS (SEM LOGIN) =====

def _public_chat_prompt(pergunta):
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
    return f"{contexto}\n\n{pergunta}"

@ensure_csrf_cookie
def usuario_final_chat(request):
    """Renderiza a página pública de chat para usuários finais."""
    llms_publicas = LLMPublica.objects.filter(ativo=True).only("id", "nome").order_by("nome")
    return render(request, 'chat/chatpublico.html', {
        "llms_publicas": llms_publicas,
    })


@require_http_methods(["POST"])
def usuario_final_chat_api(request):
    """
    API pública para processar perguntas de usuários finais.
    Recebe: {"pergunta": "...", "modelo_id": 1}
    Retorna: {"status": "ok/erro", "respostas": [...], "mensagem": "..."}
    """
    limited_response = _public_api_rate_limit(request, "generate")
    if limited_response is not None:
        return limited_response

    try:
        dados = json.loads(request.body)
        pergunta = dados.get('pergunta', '').strip()
        modelo_id = dados.get('modelo_id')
        avaliar_agora = dados.get('avaliar', True) is not False
        
        if not pergunta:
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'Pergunta não pode estar vazia.'
            }, status=400)

        try:
            modelo_id = int(modelo_id)
        except (TypeError, ValueError):
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'Selecione um modelo antes de enviar a pergunta.'
            }, status=400)
        
        # Apenas LLMs cadastradas pelo admin no painel /admin-pondersec/ atendem o chat público.
        # As LLMs dos pesquisadores (model LLM) NUNCA são usadas aqui.
        llms_ativos = list(LLMPublica.objects.filter(ativo=True).order_by("nome"))

        if not llms_ativos:
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'Nenhuma LLM foi configurada pelo administrador para o chat público.'
            }, status=400)

        llm_selecionada = next(
            (llm for llm in llms_ativos if llm.id == modelo_id),
            None,
        )
        if llm_selecionada is None:
            return JsonResponse({
                'status': 'erro',
                'mensagem': 'O modelo selecionado não está disponível.'
            }, status=400)
        
        prompt_final = _public_chat_prompt(pergunta)

        pergunta_publica = PerguntaPublica.objects.create(conteudo=pergunta)

        try:
            texto_resposta = _judgeai_call_configured_llm(llm_selecionada, prompt_final)
            resultado = {
                'llm_id': llm_selecionada.id,
                'modelo': llm_selecionada.nome,
                'resposta': texto_resposta.strip(),
                'ok': True,
            }
        except Exception as exc:
            logger.exception(
                "Falha no chat público pergunta_id=%s llm_id=%s",
                pergunta_publica.id,
                llm_selecionada.id,
            )
            resultado = {
                'llm_id': llm_selecionada.id,
                'modelo': llm_selecionada.nome,
                'resposta': str(exc),
                'ok': False,
            }

        resposta_publica = RespostaPublica.objects.create(
            pergunta=pergunta_publica,
            llm=llm_selecionada,
            conteudo_resposta=resultado["resposta"],
            ok=resultado["ok"],
        )
        resultado["resposta_id"] = resposta_publica.id
        respostas = [resultado]
        respostas_publicas = [resposta_publica]

        if not resultado["ok"]:
            return JsonResponse({
                "status": "erro",
                "respostas": respostas,
                "mensagem": resultado["resposta"],
            }, status=502)

        avaliacao_token = signing.dumps(
            {"resposta_id": resposta_publica.id},
            salt=PUBLIC_EVALUATION_TOKEN_SALT,
            compress=True,
        )
        if avaliar_agora:
            try:
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
            except Exception:
                logger.exception(
                    "Resposta pública salva, mas avaliação falhou resposta_id=%s",
                    resposta_publica.id,
                )
                avaliacao_status = {
                    "status": "erro",
                    "total": 0,
                    "notas_total": 0,
                    "mensagem": "A resposta foi gerada, mas a avaliação automática falhou.",
                }
                avaliacoes_por_resposta = {}
                tabela_avaliacao_cruzada = []
        else:
            avaliacao_status = {
                "status": "pendente",
                "total": 0,
                "notas_total": 0,
                "mensagem": "Use o token retornado para solicitar a avaliação separadamente.",
            }
            avaliacoes_por_resposta = {}
            tabela_avaliacao_cruzada = []

        avaliacao_concluida = (
            not avaliacao_status.get("limitado")
            and avaliacao_status.get("status") not in {
                "erro", "parcial", "pendente", "sem_notas",
            }
        )
        RespostaPublica.objects.filter(pk=resposta_publica.pk).update(
            avaliacao_estado=(
                RespostaPublica.AVALIACAO_CONCLUIDA
                if avaliacao_concluida
                else RespostaPublica.AVALIACAO_PENDENTE
            ),
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
            'avaliacao_token': avaliacao_token,
            'mensagem': 'Respostas geradas com sucesso.'
        })
    
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({
            'status': 'erro',
            'mensagem': 'Erro ao processar JSON.'
        }, status=400)
    except Exception:
        logger.exception("Erro não tratado em usuario_final_chat_api")
        return JsonResponse({
            'status': 'erro',
            'mensagem': 'Não foi possível processar a solicitação. Tente novamente.'
        }, status=500)


def _public_chat_stream_event(tipo, **dados):
    return json.dumps({"tipo": tipo, **dados}, ensure_ascii=False) + "\n"


@require_http_methods(["POST"])
def usuario_final_chat_stream_api(request):
    """Transmite incrementalmente a resposta de uma única LLM pública selecionada."""
    limited_response = _public_api_rate_limit(request, "generate")
    if limited_response is not None:
        return limited_response

    try:
        dados = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({
            "status": "erro",
            "mensagem": "Erro ao processar JSON.",
        }, status=400)

    pergunta = dados.get("pergunta", "").strip()
    if not pergunta:
        return JsonResponse({
            "status": "erro",
            "mensagem": "Pergunta não pode estar vazia.",
        }, status=400)

    try:
        modelo_id = int(dados.get("modelo_id"))
    except (TypeError, ValueError):
        return JsonResponse({
            "status": "erro",
            "mensagem": "Selecione um modelo antes de enviar a pergunta.",
        }, status=400)

    llms_ativos = list(LLMPublica.objects.filter(ativo=True).order_by("nome"))
    if not llms_ativos:
        return JsonResponse({
            "status": "erro",
            "mensagem": "Nenhuma LLM foi configurada pelo administrador para o chat público.",
        }, status=400)

    llm_selecionada = next((llm for llm in llms_ativos if llm.id == modelo_id), None)
    if llm_selecionada is None:
        return JsonResponse({
            "status": "erro",
            "mensagem": "O modelo selecionado não está disponível.",
        }, status=400)

    try:
        pergunta_publica = PerguntaPublica.objects.create(conteudo=pergunta)
    except Exception:
        logger.exception("Falha ao salvar pergunta do chat público antes do streaming")
        return JsonResponse({
            "status": "erro",
            "mensagem": "Não foi possível salvar a pergunta. Tente novamente.",
        }, status=500)
    prompt_final = _public_chat_prompt(pergunta)

    def gerar_eventos():
        partes = []
        yield _public_chat_stream_event(
            "inicio",
            modelo={"id": llm_selecionada.id, "nome": llm_selecionada.nome},
        )

        try:
            for trecho in _judgeai_stream_configured_llm(llm_selecionada, prompt_final):
                if not trecho:
                    continue
                partes.append(trecho)
                yield _public_chat_stream_event("trecho", conteudo=trecho)

            texto_resposta = "".join(partes).strip()
            if not texto_resposta:
                raise RuntimeError("O modelo não retornou conteúdo.")
        except Exception as exc:
            texto_parcial = "".join(partes).strip()
            mensagem = str(exc)
            logger.exception(
                "Falha no stream do chat público pergunta_id=%s llm_id=%s parcial=%s",
                pergunta_publica.id,
                llm_selecionada.id,
                bool(texto_parcial),
            )
            try:
                RespostaPublica.objects.create(
                    pergunta=pergunta_publica,
                    llm=llm_selecionada,
                    conteudo_resposta=texto_parcial or mensagem,
                    ok=False,
                )
            except Exception:
                logger.exception(
                    "Falha ao salvar resposta pública com erro pergunta_id=%s llm_id=%s",
                    pergunta_publica.id,
                    llm_selecionada.id,
                )
            yield _public_chat_stream_event(
                "erro",
                mensagem=mensagem,
                possui_conteudo_parcial=bool(texto_parcial),
            )
            return

        try:
            resposta_publica = RespostaPublica.objects.create(
                pergunta=pergunta_publica,
                llm=llm_selecionada,
                conteudo_resposta=texto_resposta,
                ok=True,
            )
        except Exception:
            logger.exception(
                "Falha ao salvar resposta pública pergunta_id=%s llm_id=%s",
                pergunta_publica.id,
                llm_selecionada.id,
            )
            yield _public_chat_stream_event(
                "erro",
                mensagem="A resposta foi gerada, mas não pôde ser salva. Tente novamente.",
                possui_conteudo_parcial=True,
            )
            return
        yield _public_chat_stream_event(
            "resposta_concluida",
            resposta_id=resposta_publica.id,
        )
        avaliacao_token = signing.dumps(
            {"resposta_id": resposta_publica.id},
            salt=PUBLIC_EVALUATION_TOKEN_SALT,
            compress=True,
        )
        yield _public_chat_stream_event(
            "concluido",
            resposta_id=resposta_publica.id,
            avaliacao={"status": "pendente", "media_geral": None, "notas_total": 0, "metricas": []},
            avaliacao_cruzada={
                "status": "pendente",
                "total": 0,
                "notas_total": 0,
                "mensagem": "A avaliação automática está sendo processada.",
            },
            tabela_avaliacao_cruzada=[],
            avaliacao_token=avaliacao_token,
        )

    response = StreamingHttpResponse(
        gerar_eventos(),
        content_type="application/x-ndjson; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-store"
    response["X-Accel-Buffering"] = "no"
    return response


@require_http_methods(["POST"])
def usuario_final_chat_avaliacao_api(request):
    """Executa o JudgeAI fora do caminho crítico da geração da resposta."""
    limited_response = _public_api_rate_limit(
        request,
        "evaluation",
        setting_name="PUBLIC_EVALUATION_RATE_LIMIT",
    )
    if limited_response is not None:
        return limited_response

    try:
        dados = json.loads(request.body or "{}")
        resposta_id = int(dados.get("resposta_id"))
        token = dados.get("avaliacao_token") or ""
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return JsonResponse({"status": "erro", "mensagem": "Solicitação inválida."}, status=400)

    try:
        token_data = signing.loads(
            token,
            salt=PUBLIC_EVALUATION_TOKEN_SALT,
            max_age=15 * 60,
        )
    except SignatureExpired:
        return JsonResponse({"status": "erro", "mensagem": "A autorização da avaliação expirou."}, status=403)
    except BadSignature:
        return JsonResponse({"status": "erro", "mensagem": "Autorização de avaliação inválida."}, status=403)

    if not isinstance(token_data, dict) or token_data.get("resposta_id") != resposta_id:
        return JsonResponse({"status": "erro", "mensagem": "Autorização incompatível."}, status=403)

    metricas = _metricas_publicas_ativas()
    juizes = list(LLMPublica.objects.filter(ativo=True).order_by("nome"))
    agora = timezone.now()
    claim_id = None

    # O estado é reivindicado em uma transação curta. Nenhum lock de banco fica
    # aberto durante chamadas externas, mas outro worker enxerga imediatamente
    # que a mesma resposta já está sendo avaliada.
    with transaction.atomic():
        try:
            resposta_publica = (
                RespostaPublica.objects
                # As relações são opcionais e viram LEFT JOIN. No PostgreSQL,
                # tentar bloqueá-las junto com a resposta causa
                # "FOR UPDATE cannot be applied to the nullable side".
                .select_for_update(of=("self",))
                .select_related("pergunta", "llm")
                .get(id=resposta_id, ok=True)
            )
        except RespostaPublica.DoesNotExist:
            return JsonResponse(
                {"status": "erro", "mensagem": "Resposta não encontrada."},
                status=404,
            )

        juizes_validos = [
            juiz
            for juiz in juizes
            if not _same_llm_identity(resposta_publica.llm, juiz)
        ]
        concluidos = set()
        if metricas and juizes_validos:
            concluidos = set(
                AvaliacaoPublicaLLM.objects
                .filter(
                    resposta=resposta_publica,
                    juiz__in=juizes_validos,
                    metrica__in=metricas,
                    erro=False,
                    avaliacao_quanti__isnull=False,
                )
                .values("juiz_id")
                .annotate(metricas_total=Count("metrica_id", distinct=True))
                .filter(metricas_total__gte=len(metricas))
                .values_list("juiz_id", flat=True)
            )
        juizes_faltantes = [juiz for juiz in juizes_validos if juiz.id not in concluidos]

        claim_recente = (
            resposta_publica.avaliacao_estado == RespostaPublica.AVALIACAO_PROCESSANDO
            and resposta_publica.avaliacao_iniciada_em
            and resposta_publica.avaliacao_iniciada_em > agora - PUBLIC_EVALUATION_CLAIM_TTL
        )
        if juizes_faltantes and claim_recente:
            return JsonResponse({
                "status": "processando",
                "resposta_id": resposta_publica.id,
                "retry_after_ms": 1200,
            }, status=202)

        if juizes_faltantes:
            claim_id = uuid.uuid4()
            resposta_publica.avaliacao_estado = RespostaPublica.AVALIACAO_PROCESSANDO
            resposta_publica.avaliacao_iniciada_em = agora
            resposta_publica.avaliacao_claim_id = claim_id
        else:
            resposta_publica.avaliacao_estado = RespostaPublica.AVALIACAO_CONCLUIDA
            resposta_publica.avaliacao_iniciada_em = None
            resposta_publica.avaliacao_claim_id = None
        resposta_publica.save(update_fields=[
            "avaliacao_estado",
            "avaliacao_iniciada_em",
            "avaliacao_claim_id",
        ])

    if juizes_faltantes:
        limite_lote = max(1, int(getattr(settings, "LLM_EVALUATION_MAX_WORKERS", 4)))
        juizes_lote = juizes_faltantes[:limite_lote]
        avaliacao_status = _executar_avaliacao_cruzada_publica(
            resposta_publica.pergunta,
            [resposta_publica],
            juizes_lote,
            metricas,
        )
        if len(juizes_lote) < len(juizes_faltantes):
            avaliacao_status["limitado"] = True
            avaliacao_status["mensagem"] = "Avaliação parcial concluída; processando os demais juízes."
    elif juizes_validos and metricas:
        avaliacao_status = {
            "status": "ok",
            "total": len(concluidos),
            "notas_total": len(concluidos) * len(metricas),
            "erros": 0,
            "limitado": False,
            "mensagem": "Avaliação cruzada já concluída.",
        }
    else:
        avaliacao_status = {
            "status": "sem_pares" if not juizes_validos else "sem_metricas",
            "total": 0,
            "notas_total": 0,
            "erros": 0,
            "limitado": False,
            "mensagem": "Não há pares ou métricas disponíveis para avaliação.",
        }

    deve_continuar = bool(avaliacao_status.get("limitado"))
    houve_falha = avaliacao_status.get("status") in {"erro", "parcial", "sem_notas"}
    estado_final = (
        RespostaPublica.AVALIACAO_PENDENTE
        if deve_continuar or houve_falha
        else RespostaPublica.AVALIACAO_CONCLUIDA
    )
    finalizacao = RespostaPublica.objects.filter(pk=resposta_publica.pk)
    if claim_id is not None:
        # Uma execução antiga não pode apagar o claim de uma retomada mais nova.
        finalizacao = finalizacao.filter(avaliacao_claim_id=claim_id)
    finalizacao.update(
        avaliacao_estado=estado_final,
        avaliacao_iniciada_em=None,
        avaliacao_claim_id=None,
    )

    avaliacoes = _resumo_avaliacoes_publicas_por_resposta([resposta_publica.id])
    payload = {
        "status": "processando" if deve_continuar else (
            "ok" if avaliacao_status.get("status") not in {"erro"} else "parcial"
        ),
        "resposta_id": resposta_publica.id,
        "avaliacao": avaliacoes.get(
            resposta_publica.id,
            {"status": "sem_dados", "media_geral": None, "notas_total": 0, "metricas": []},
        ),
        "avaliacao_cruzada": avaliacao_status,
        "tabela_avaliacao_cruzada": _tabela_avaliacoes_publicas([resposta_publica.id]),
    }
    if deve_continuar:
        payload["retry_after_ms"] = 300
    return JsonResponse(payload, status=202 if deve_continuar else 200)


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

        llms_ativas_ids = set(
            LLM.objects.filter(usuario=request.user, ativo=True).values_list("id", flat=True)
        )
        total_llms = len(llms_ativas_ids)

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
        llms_respondidas_ids = {
            resposta.llm_id
            for resposta in respostas_qs
            if resposta.llm_id in llms_ativas_ids
        }
        ias_faltantes_total = max(0, total_llms - len(llms_respondidas_ids))

        return JsonResponse({
            'pergunta': questao.conteudo,
            'questao_id': questao.id,
            'data': '',
            'respostas': respostas_encontradas,
            'respostas_humanas': humanas_encontradas,
            'resposta_humana': humanas_encontradas[0]['texto'] if humanas_encontradas else None,
            'total_llms': total_llms,
            'llms_respondidas_total': len(llms_respondidas_ids),
            'ias_faltantes_total': ias_faltantes_total,
            'tem_resposta_humana': tem_resposta_humana,
            'tem_respostas_ia': len(respostas_encontradas) > 0,
        })
    except Exception:
        logger.exception("Falha ao carregar detalhes da questão id=%s usuario_id=%s", id, request.user.id)
        return JsonResponse({'erro': 'Não foi possível carregar os detalhes da questão.'}, status=500)


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
@require_http_methods(["POST"])
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

    try:
        texto_ia_limpa = _judgeai_call_configured_llm(llm, prompt_final)
    except Exception as exc:
        logger.exception("Falha ao gerar resposta única questao_id=%s llm_id=%s", questao.id, llm.id)
        return JsonResponse({'status': 'erro', 'mensagem': str(exc)}, status=502)

    try:
        Resposta.objects.update_or_create(
            questao_id=questao_id,
            llm=llm,
            defaults={"conteudo_resposta": texto_ia_limpa.strip()},
        )
    except Exception:
        logger.exception("Resposta gerada, mas não salva questao_id=%s llm_id=%s", questao.id, llm.id)
        return JsonResponse({
            'status': 'erro',
            'mensagem': 'A resposta foi gerada, mas não pôde ser salva. Tente novamente.',
        }, status=500)

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
        Resposta.objects.filter(
            questao=questao,
            llm_id__isnull=False,
        ).values_list('llm_id', flat=True)
    )

    ias_faltantes = [llm for llm in llms_ativos if llm.id not in respostas_existentes]

    if not ias_faltantes:
        return JsonResponse({
            'status': 'ja_completo',
            'geradas': 0,
            'respondidas': len(respostas_existentes & {llm.id for llm in llms_ativos}),
            'total_llms': len(llms_ativos),
        })

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
    geradas = 0
    concluidas = 0
    # Cada resultado é persistido assim que termina. Uma LLM lenta ou um worker
    # interrompido não mantém em memória (nem faz sumir) a resposta que já chegou.
    executor = get_llm_executor("generation")
    futures = {
        executor.submit(_call_llm_in_worker, llm, prompt_final): llm
        for llm in ias_faltantes
    }
    for future in as_completed(futures):
        llm = futures[future]
        try:
            texto = future.result().strip()
        except LLMServiceError as exc:
            logger.warning(
                "Provedor não concluiu resposta faltante questao_id=%s llm_id=%s code=%s",
                questao.id,
                llm.id,
                exc.code,
            )
            erros.append({
                "llm_id": llm.id,
                "modelo": llm.nome,
                "codigo": exc.code,
                "mensagem": str(exc),
            })
            continue
        except Exception as exc:
            logger.exception(
                "Falha inesperada ao gerar resposta faltante questao_id=%s llm_id=%s",
                questao.id,
                llm.id,
            )
            erros.append({
                "llm_id": llm.id,
                "modelo": llm.nome,
                "mensagem": str(exc),
            })
            continue

        try:
            _resposta, criada = Resposta.objects.update_or_create(
                questao_id=questao_id,
                llm=llm,
                defaults={"conteudo_resposta": texto},
            )
            concluidas += 1
            geradas += int(criada)
        except Exception as exc:
            logger.exception(
                "Falha ao salvar resposta faltante questao_id=%s llm_id=%s",
                questao.id,
                llm.id,
            )
            erros.append({
                "llm_id": llm.id,
                "modelo": llm.nome,
                "mensagem": str(exc),
            })

    respondidas = (
        Resposta.objects
        .filter(questao=questao, llm_id__in=[llm.id for llm in llms_ativos])
        .values("llm_id")
        .distinct()
        .count()
    )

    if erros:
        return JsonResponse({
            'status': 'parcial' if concluidas else 'erro',
            'geradas': geradas,
            'concluidas': concluidas,
            'respondidas': respondidas,
            'total_llms': len(llms_ativos),
            'erros': erros,
            'mensagem': 'Alguns modelos não responderam. Consulte os detalhes retornados.',
        }, status=207 if concluidas else 502)

    return JsonResponse({
        'status': 'ok',
        'geradas': geradas,
        'concluidas': concluidas,
        'respondidas': respondidas,
        'total_llms': len(llms_ativos),
    })


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
    primeira_resposta = (
        Resposta.objects
        .filter(questao_id=OuterRef("pk"))
        .order_by("id")
        .values("conteudo_resposta")[:1]
    )
    respostas_prefetch = Prefetch(
        "respostas",
        queryset=Resposta.objects.select_related("llm").only(
            "id",
            "questao_id",
            "llm_id",
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
        .annotate(primeira_resposta_preview=Subquery(primeira_resposta))
        .select_related("categoria")
        .prefetch_related(respostas_prefetch, formularios_prefetch)
        .only(
            "id",
            "conteudo",
            "resposta_humana",
            "categoria_id",
            "categoria__id",
            "categoria__nome_categoria",
        )
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
def download_template_perguntas(request, formato):
    if formato == 'json':
        conteudo = json.dumps([
            {
                "pergunta": "What is a SQL injection attack?",
                "categoria": "Web Security",
                "resposta": "A SQL injection inserts malicious SQL code into a query... (optional field)"
            },
            {
                "pergunta": "What controls reduce phishing risk in an organization?",
                "categoria": "Web Security",
                "resposta": ""
            },
            {
                "pergunta": "What is the difference between IDS and IPS?",
                "categoria": "Network Security",
                "resposta": ""
            }
        ], ensure_ascii=False, indent=2)
        response = HttpResponse(conteudo, content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="template_perguntas.json"'
        return response

    if formato == 'txt':
        conteudo = (
            "Eixo 1 - Web Security\n"
            "1. What is a SQL injection attack?\n"
            "RESPOSTA: A SQL injection inserts malicious SQL code into a query... (optional)\n"
            "\n"
            "2. What controls reduce phishing risk in an organization?\n"
            "\n"
            "Eixo 2 - Network Security\n"
            "1. What is the difference between IDS and IPS?\n"
            "\n"
            "2. What is a VPN and how does it protect network traffic?\n"
        )
        response = HttpResponse(conteudo, content_type='text/plain; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="template_perguntas.txt"'
        return response

    return HttpResponse(status=404)


def _upload_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _parse_uploaded_questions(conteudo_texto, nome_arquivo):
    """Valida todo o arquivo antes de qualquer escrita no banco."""
    itens = []
    if nome_arquivo.endswith(".json"):
        dados = json.loads(conteudo_texto)
        if not isinstance(dados, list):
            dados = [dados]

        for indice, item in enumerate(dados, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"O item {indice} do JSON não é um objeto.")
            pergunta = _upload_text(item.get("pergunta"))
            if not pergunta:
                continue
            itens.append({
                "pergunta": pergunta,
                "resposta": _upload_text(item.get("resposta") or item.get("RESPOSTA")),
                "categoria": _upload_text(item.get("categoria") or item.get("Categoria")),
            })
        return itens

    categoria_atual = ""
    linhas = conteudo_texto.splitlines()
    indice = 0
    while indice < len(linhas):
        linha = linhas[indice].strip()
        if not linha:
            indice += 1
            continue

        if linha.lower().startswith("eixo"):
            categoria_atual = re.sub(
                r'^eixo\s*\d+\s*[-–—:]\s*',
                '',
                linha,
                flags=re.IGNORECASE,
            ).strip()
            indice += 1
            continue

        pergunta = re.sub(r'^\d+[\.\)]\s*', '', linha).strip()
        resposta = ""
        if pergunta and indice + 1 < len(linhas):
            proxima_linha = linhas[indice + 1].strip()
            if proxima_linha.upper().startswith("RESPOSTA:"):
                resposta = proxima_linha[len("RESPOSTA:"):].strip()
                indice += 1
        if pergunta:
            itens.append({
                "pergunta": pergunta,
                "resposta": resposta,
                "categoria": categoria_atual,
            })
        indice += 1
    return itens


@login_required
@require_http_methods(["POST"])
def upload_perguntas(request):
    arquivo = request.FILES.get("arquivo_upload")
    if not arquivo:
        django_messages.error(request, _("Nenhum arquivo foi enviado."))
        return redirect('questoes')

    max_bytes = max(1, int(getattr(settings, "QUESTION_UPLOAD_MAX_BYTES", 10 * 1024 * 1024)))
    if arquivo.size > max_bytes:
        django_messages.error(request, _("O arquivo excede o limite permitido para importação."))
        return redirect('questoes')

    nome_arquivo = arquivo.name.lower()
    if not nome_arquivo.endswith((".json", ".txt")):
        django_messages.error(request, _("Envie um arquivo JSON ou TXT válido."))
        return redirect('questoes')

    try:
        conteudo_texto = arquivo.read().decode("utf-8-sig")
        itens = _parse_uploaded_questions(conteudo_texto, nome_arquivo)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Upload de perguntas inválido usuario_id=%s erro=%s", request.user.id, exc)
        django_messages.error(request, _("Arquivo inválido ou mal formatado."))
        return redirect('questoes')

    if not itens:
        django_messages.error(request, _("Nenhuma pergunta encontrada no arquivo."))
        return redirect('questoes')

    max_items = max(1, int(getattr(settings, "QUESTION_UPLOAD_MAX_ITEMS", 20000)))
    category_max_length = Categoria._meta.get_field("nome_categoria").max_length
    if len(itens) > max_items or any(
        len(item["categoria"]) > category_max_length
        for item in itens
    ):
        django_messages.error(request, _("O arquivo excede os limites permitidos para importação."))
        return redirect('questoes')

    categoria_id = request.POST.get("categoria_id")
    with transaction.atomic():
        categoria_padrao = None
        if categoria_id:
            categoria_padrao = Categoria.objects.filter(
                id=categoria_id,
                usuario=request.user,
            ).first()
        if not categoria_padrao:
            categoria_padrao, _created = Categoria.objects.get_or_create(
                nome_categoria="Geral",
                usuario=request.user,
                defaults={'descricao_categoria': 'Categoria padrão para importação'},
            )

        nomes_categorias = {
            item["categoria"]
            for item in itens
            if item["categoria"] and item["categoria"] != categoria_padrao.nome_categoria
        }
        existentes = {
            categoria.nome_categoria: categoria
            for categoria in Categoria.objects.filter(
                usuario=request.user,
                nome_categoria__in=nomes_categorias,
            )
        }
        faltantes = nomes_categorias - set(existentes)
        if faltantes:
            Categoria.objects.bulk_create(
                [
                    Categoria(
                        usuario=request.user,
                        nome_categoria=nome,
                        descricao_categoria="",
                    )
                    for nome in sorted(faltantes)
                ],
                ignore_conflicts=True,
            )
            existentes.update({
                categoria.nome_categoria: categoria
                for categoria in Categoria.objects.filter(
                    usuario=request.user,
                    nome_categoria__in=faltantes,
                )
            })

        Questao.objects.bulk_create(
            [
                Questao(
                    conteudo=item["pergunta"],
                    usuario=request.user,
                    categoria=existentes.get(item["categoria"], categoria_padrao),
                    resposta_humana=item["resposta"] or None,
                )
                for item in itens
            ],
            batch_size=500,
        )

    django_messages.success(request, _("%(count)s perguntas importadas com sucesso!") % {
        "count": len(itens),
    })

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
@require_http_methods(["POST"])
def gerar_respostas(request, questao_id):
    # Compatibilidade com a rota antiga, agora usando o mesmo fluxo paralelo,
    # incremental e idempotente da tela de consultas.
    return gerar_respostas_ia_faltantes(request, questao_id)

@login_required
def limpar_respostas(request):
    if request.method == "POST":
        try:
            # BLINDADO: Apaga apenas as respostas das questões que pertencem ao usuário logado
            Resposta.objects.filter(questao__usuario=request.user).delete()
            return JsonResponse({"ok": True})
        except Exception:
            logger.exception("Falha ao limpar respostas usuario_id=%s", request.user.id)
            return JsonResponse({"ok": False, "erro": _("Não foi possível limpar as respostas.")}, status=500)
            
    return JsonResponse({"ok": False, "erro": _("Método não permitido")}, status=405)
    
@login_required
def setup_llm(request):
    if request.method == "POST":
        nome = (request.POST.get("model") or "").strip()
        provedor = (request.POST.get("provider") or "").strip()
        api_key = (request.POST.get("apiKey") or "").strip()

        if not nome or not provedor or not api_key:
            django_messages.error(request, _("Provedor, modelo e chave da API são obrigatórios."))
            return redirect('setup_llm')

        if len(api_key) > LLM._meta.get_field("api_key").max_length:
            django_messages.error(request, _("A chave da API excede o tamanho permitido."))
            return redirect('setup_llm')

        try:
            llm = LLM.objects.create(
                usuario=request.user,
                nome=nome,
                descricao=provedor,
                api_key=api_key,
            )
        except Exception:
            logger.exception("Falha ao salvar nova configuração de LLM usuario_id=%s", request.user.id)
            django_messages.error(request, _("Não foi possível salvar a configuração da LLM."))
            return redirect('setup_llm')

        logger.info(
            "Configuração LLM criada usuario_id=%s llm_id=%s model=%s provider=%s key_fp=%s",
            request.user.id,
            llm.id,
            llm.nome,
            llm.descricao,
            _api_key_fingerprint(llm.api_key),
        )
        django_messages.success(request, _("IA '%(nome)s' configurada com sucesso!") % {
            "nome": nome,
        })
        return redirect('setup_llm')

    llms_cadastradas = list(LLM.objects.filter(usuario=request.user, ativo=True))
    for llm in llms_cadastradas:
        llm.api_key_hint = _api_key_hint(llm.api_key)
    return render(request, 'setup/setup-llm.html',{"llms_cadastradas": llms_cadastradas})

@login_required
def setup_configurar_llm(request):
    return render(request, 'setup/setup-configurar-llm.html')

@login_required
def setup_avaliacao(request):
    metricas = ensure_judge_metrics(request.user)
    return render(request, 'setup/setup-avaliacao.html', {'metricas': metricas})

@login_required
def setup_adicionar_metrica(request):
    if request.method == 'POST':
        ensure_judge_metrics(request.user)
        django_messages.error(
            request,
            _("As métricas do JudgeAI são fixas: Completude, Acurácia, Diretividade e Clareza (1 a 5)."),
        )

    return redirect('setup_avaliacao')


@login_required
def setup_configurar_metrica(request):
    if request.method == 'POST':
        ensure_judge_metrics(request.user)
        django_messages.error(request, _("As métricas e a escala do JudgeAI são fixas."))
    return redirect('setup_avaliacao')

@login_required
@require_http_methods(["DELETE"])
def setup_deletar_metrica(request, id):
    ensure_judge_metrics(request.user)
    return JsonResponse({
        "status": "error",
        "message": _("As quatro métricas oficiais do JudgeAI não podem ser removidas."),
    }, status=409)

@login_required
@require_http_methods(["DELETE"])
def deletar_llm(request, id):
    llm = get_object_or_404(LLM, id=id, usuario=request.user)
    # Remoção lógica: respostas e avaliações históricas não podem desaparecer ao
    # retirar uma credencial da lista de modelos disponíveis.
    llm.ativo = False
    llm.save(update_fields=["ativo"])
    return JsonResponse({"status": "success", "message": _("LLM removida com sucesso.")})

@login_required
@require_http_methods(["PUT"])
def edit_llm_api(request, id):
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": _("JSON inválido.")}, status=400)

    nome = (data.get("nome") or "").strip()
    provedor = (data.get("descricao") or "").strip()
    api_key = (data.get("api_key") or "").strip()

    if not nome or not provedor:
        return JsonResponse({
            "status": "error",
            "message": _("Provedor e modelo são obrigatórios."),
        }, status=400)

    if len(api_key) > LLM._meta.get_field("api_key").max_length:
        return JsonResponse({
            "status": "error",
            "message": _("A chave da API excede o tamanho permitido."),
        }, status=400)

    llm = LLM.objects.filter(id=id, usuario=request.user).first()
    if not llm:
        return JsonResponse({
            "status": "error",
            "message": _("Configuração de LLM não encontrada."),
        }, status=404)

    previous_fingerprint = _api_key_fingerprint(llm.api_key)
    try:
        with transaction.atomic():
            llm.nome = nome
            llm.descricao = provedor
            fields = ["nome", "descricao"]
            if api_key:
                llm.api_key = api_key
                fields.append("api_key")
            llm.save(update_fields=fields)
            llm.refresh_from_db(fields=["nome", "descricao", "api_key"])

            if api_key and llm.api_key != api_key:
                raise RuntimeError("A chave da API não foi persistida após o salvamento.")
    except Exception:
        logger.exception(
            "Falha ao atualizar configuração LLM usuario_id=%s llm_id=%s",
            request.user.id,
            llm.id,
        )
        return JsonResponse({
            "status": "error",
            "message": _("Não foi possível salvar a configuração da LLM."),
        }, status=500)

    current_fingerprint = _api_key_fingerprint(llm.api_key)
    logger.info(
        "Configuração LLM atualizada usuario_id=%s llm_id=%s model=%s provider=%s "
        "key_updated=%s key_fp_before=%s key_fp_after=%s",
        request.user.id,
        llm.id,
        llm.nome,
        llm.descricao,
        bool(api_key),
        previous_fingerprint,
        current_fingerprint,
    )
    return JsonResponse({
        "status": "success",
        "message": _("LLM atualizada com sucesso."),
        "nome": llm.nome,
        "descricao": llm.descricao,
        "key_updated": bool(api_key),
        "api_key_hint": _api_key_hint(llm.api_key),
    })

@login_required
def menu_consulta(request):
    return redirect('executar_consulta')

@login_required
def executar_consulta(request):
    total_llms_ativas = LLM.objects.filter(usuario=request.user, ativo=True).count()
    questoes = list(
        Questao.objects
        .filter(usuario=request.user)
        .annotate(
            respostas_ativas_total=Count(
                "respostas__llm_id",
                filter=Q(
                    respostas__llm__usuario=request.user,
                    respostas__llm__ativo=True,
                ),
                distinct=True,
            )
        )
        .only("id", "conteudo")
        .order_by("id")
    )

    for questao in questoes:
        questao.total_llms_ativas = total_llms_ativas
        if total_llms_ativas == 0:
            questao.status_consulta = "sem_modelos"
        elif questao.respostas_ativas_total == 0:
            questao.status_consulta = "pendente"
        elif questao.respostas_ativas_total < total_llms_ativas:
            questao.status_consulta = "parcial"
        else:
            questao.status_consulta = "completa"
        questao.pode_executar = questao.status_consulta in {"pendente", "parcial"}
        questao.tem_respostas_ia = questao.respostas_ativas_total > 0

    return render(request, 'consulta/executar-consulta.html', {
        "questoes": questoes,
        "total_llms_ativas": total_llms_ativas,
    })

@login_required
def consulta_comparacao(request):
    return render(request, 'consulta/consulta-comparacao.html')

def _text_preview(text, limit=320):
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."

def _metric_max(metrica):
    return 5


def _judgeai_stream_configured_llm(llm, prompt):
    yield from stream_configured_llm(llm, prompt)


def _judgeai_call_configured_llm(llm, prompt):
    return call_configured_llm(llm, prompt)

def _judgeai_prompt(questao, resposta, juiz, metricas):
    metricas_txt = "\n".join(
        f"- {m.nome} (1 a 5): {m.descricao or m.criterio_texto}"
        for m in metricas
    )
    return (
        "Você é uma LLM atuando como juiz técnico especializado em cibersegurança no PonderSec.\n"
        "Sua tarefa é avaliar, de forma objetiva e criteriosa, a resposta de outro modelo (o 'respondente') "
        "a uma pergunta técnica de cibersegurança.\n\n"

        "REGRAS DE AVALIAÇÃO:\n"
        "1. Avalie EXATAMENTE estas quatro métricas, na escala inteira de 1 a 5: Completude, Acurácia, Diretividade e Clareza.\n"
        "2. Não avalie nenhuma métrica além das quatro listadas.\n"
        "3. Antes de decidir a nota, releia a descrição da métrica e depois releia a resposta em busca de evidências concretas "
        "(trechos, afirmações, exemplos, comandos, códigos) que sustentem ou contradigam o critério descrito.\n"
        "4. Evite viés de verbosidade: uma resposta mais longa não é automaticamente melhor. Avalie se a resposta "
        "atende ao critério da métrica, não o tamanho do texto.\n"
        "5. Evite viés de complacência: não dê notas altas por padrão. Se a resposta não atender ao critério descrito "
        "na métrica, a nota deve refletir isso.\n"
        "6. Se a resposta avaliada for vazia, sem sentido, uma recusa sem justificativa técnica ou completamente fora "
        "do escopo da pergunta, atribua nota 1 nas quatro métricas e explique isso na justificativa.\n"
        "7. Você está avaliando a resposta de OUTRO modelo. Nunca avalie uma resposta produzida por você mesmo.\n\n"

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
        '    {"metrica": "Nome exato da métrica", "nota": 1, "justificativa": "Frase completa explicando o motivo da nota com evidência da resposta."}\n'
        "  ],\n"
        '  "justificativa": "síntese geral em uma frase completa, mencionando o principal ponto forte e a principal fraqueza da resposta avaliada"\n'
        "}\n"
        "- O campo 'nota' deve ser um número inteiro entre 1 e 5.\n"
        "- A ordem das métricas no array 'notas' deve seguir a ordem em que foram apresentadas.\n"
        "- Retorne as quatro métricas uma única vez; não omita, duplique ou inclua outra métrica.\n"
        "- Não adicione campos extras ao JSON.\n\n"

        f"Juiz: {juiz.nome}\n\n"
        f"Métricas (nome, escala e DESCRIÇÃO — avalie conforme o descrito):\n{metricas_txt}\n\n"
        f"Pergunta de cibersegurança avaliada:\n{questao.conteudo}\n\n"
        f"Modelo respondente: {resposta.llm.nome if resposta.llm else 'IA desconhecida'}\n"
        f"Resposta avaliada:\n{resposta.conteudo_resposta}"
    )

def _parse_judgeai_result(raw_text, metricas):
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise ValueError("A LLM avaliadora retornou uma resposta vazia.")

    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text, re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else raw_text
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("A LLM avaliadora retornou JSON inválido.") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("notas"), list):
        raise ValueError("A avaliação não contém o array 'notas' esperado.")

    expected = {judge_metric_key(metrica.nome): metrica for metrica in metricas}
    if None in expected or set(expected) != set(JUDGE_METRIC_KEYS) or len(metricas) != 4:
        raise ValueError("A avaliação deve usar exclusivamente as quatro métricas oficiais do JudgeAI.")

    parsed_by_key = {}
    for item in parsed["notas"]:
        if not isinstance(item, dict):
            raise ValueError("Cada nota do JudgeAI deve ser um objeto JSON.")
        key = judge_metric_key(item.get("metrica"))
        if key is None:
            raise ValueError(f"Métrica inesperada na resposta do JudgeAI: {item.get('metrica') or '(vazia)' }.")
        if key in parsed_by_key:
            raise ValueError(f"A métrica {expected[key].nome} foi retornada mais de uma vez.")

        nota = item.get("nota")
        if isinstance(nota, bool) or not isinstance(nota, (int, float)) or int(nota) != nota:
            raise ValueError(f"A nota de {expected[key].nome} deve ser um número inteiro entre 1 e 5.")
        nota = int(nota)
        if nota < 1 or nota > 5:
            raise ValueError(f"A nota de {expected[key].nome} está fora da escala de 1 a 5.")

        justificativa_metrica = item.get("justificativa")
        if not isinstance(justificativa_metrica, str) or not justificativa_metrica.strip():
            raise ValueError(f"A justificativa de {expected[key].nome} não foi informada.")
        parsed_by_key[key] = {
            "nota": nota,
            "justificativa": justificativa_metrica.strip(),
        }

    missing = [
        expected[key].nome
        for key in JUDGE_METRIC_KEYS
        if key not in parsed_by_key
    ]
    if missing:
        raise ValueError(f"A avaliação não retornou todas as métricas: {', '.join(missing)}.")
    if len(parsed_by_key) != 4:
        raise ValueError("A avaliação deve retornar exatamente quatro notas.")

    resultado = [
        {
            "metrica": expected[key].nome,
            "nota": parsed_by_key[key]["nota"],
            "max": 5,
            "justificativa": parsed_by_key[key]["justificativa"],
        }
        for key in JUDGE_METRIC_KEYS
    ]
    justificativa = parsed.get("justificativa")
    if not isinstance(justificativa, str) or not justificativa.strip():
        justificativa = "Avaliação concluída com justificativas específicas para as quatro métricas."
    return resultado, justificativa.strip()


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
    metricas_por_chave = {judge_metric_key(metrica.nome): metrica for metrica in metricas}
    notas_por_chave = {judge_metric_key(item.get("metrica")): item for item in notas}
    if set(metricas_por_chave) != set(JUDGE_METRIC_KEYS) or set(notas_por_chave) != set(JUDGE_METRIC_KEYS):
        raise ValueError("O JudgeAI só pode salvar as quatro métricas oficiais.")

    with transaction.atomic():
        AvaliacaoJuiz.objects.filter(
            usuario=usuario,
            juiz=juiz,
            resposta=resposta,
            metrica__in=metricas,
        ).delete()

        novas_avaliacoes = []
        for key in JUDGE_METRIC_KEYS:
            metrica = metricas_por_chave[key]
            item = notas_por_chave[key]
            nota = item["nota"]
            novas_avaliacoes.append(AvaliacaoJuiz(
                usuario=usuario,
                juiz=juiz,
                resposta=resposta,
                metrica=metrica,
                avaliacao_quanti=nota,
                avaliacao_quali=_formatar_justificativa_avaliacao(
                    item["justificativa"], nota, 5,
                ),
                justificativa_geral=justificativa,
                erro=False,
            ))
        AvaliacaoJuiz.objects.bulk_create(novas_avaliacoes)

PUBLIC_CROSS_EVAL_MAX_TASKS = 40


def _metricas_publicas_ativas():
    return ensure_judge_metrics(None)


def _public_judge_prompt(pergunta_publica, resposta_publica, juiz, metricas):
    metricas_txt = "\n".join(
        f"- {m.nome} (1 a 5): {m.descricao or m.criterio_texto}"
        for m in metricas
    )
    respondente = resposta_publica.llm.nome if resposta_publica.llm else "LLM removida"
    return (
        "Você é uma LLM atuando como juiz no chat público do PonderSec.\n"
        "O usuário final é leigo em cibersegurança. Avalie se a resposta de outro modelo é correta, clara, útil e segura para esse público.\n\n"

        "REGRAS DE AVALIAÇÃO:\n"
        "1. Avalie EXATAMENTE estas quatro métricas, na escala inteira de 1 a 5: Completude, Acurácia, Diretividade e Clareza.\n"
        "2. Não avalie nenhuma métrica além das quatro listadas.\n"
        "3. Use a descrição de cada métrica para não misturar critérios nem justificativas.\n"
        "4. Antes de atribuir cada nota, releia a descrição da métrica e verifique se a resposta atende ou não ao que ela descreve.\n"
        "5. Evite viés de complacência: não dê notas altas por padrão. Se a resposta não atender ao critério descrito na métrica, a nota deve ser baixa.\n"
        "6. Se a resposta for vazia, uma recusa ou completamente fora do escopo, atribua nota 1 nas quatro métricas.\n"
        "7. Você está avaliando a resposta de OUTRO modelo. Nunca avalie uma resposta produzida por você mesmo.\n\n"

        "FORMATO DAS JUSTIFICATIVAS:\n"
        "- Escreva em português, entre 20 e 45 palavras por métrica.\n"
        "- Comece citando evidência concreta da resposta (trecho, afirmação, exemplo).\n"
        "- Conecte essa evidência ao critério descrito na métrica, explicando por que a nota foi aquela.\n"
        "- Não use frases genéricas como 'é clara' ou 'está completa'; detalhe o quê especificamente foi bom ou ruim.\n\n"

        "FORMATO DE SAÍDA:\n"
        "Retorne SOMENTE um JSON válido, sem markdown, sem texto antes ou depois, sem blocos de código (```).\n"
        "{\n"
        '  "notas": [\n'
        '    {"metrica": "Nome exato da métrica", "nota": 1, "justificativa": "Frase completa explicando o motivo da nota com evidência da resposta."}\n'
        "  ],\n"
        '  "justificativa": "síntese geral em uma frase completa"\n'
        "}\n"
        "- O campo 'nota' deve ser um número inteiro entre 1 e 5.\n"
        "- Retorne as quatro métricas, uma única vez cada, na ordem apresentada.\n\n"

        f"Juiz: {juiz.nome}\n\n"
        f"Métricas (nome, escala e DESCRIÇÃO — avalie conforme o descrito):\n{metricas_txt}\n\n"
        f"Pergunta do usuário público:\n{pergunta_publica.conteudo}\n\n"
        f"Modelo respondente: {respondente}\n"
        f"Resposta avaliada:\n{resposta_publica.conteudo_resposta}"
    )


def _salvar_avaliacoes_publicas(resposta_publica, juiz, metricas, notas, justificativa):
    metricas_por_chave = {judge_metric_key(metrica.nome): metrica for metrica in metricas}
    notas_por_chave = {judge_metric_key(item.get("metrica")): item for item in notas}
    if set(metricas_por_chave) != set(JUDGE_METRIC_KEYS) or set(notas_por_chave) != set(JUDGE_METRIC_KEYS):
        raise ValueError("O JudgeAI só pode salvar as quatro métricas oficiais.")

    with transaction.atomic():
        AvaliacaoPublicaLLM.objects.filter(
            juiz=juiz,
            resposta=resposta_publica,
            metrica__in=metricas,
        ).delete()

        novas_avaliacoes = []
        for key in JUDGE_METRIC_KEYS:
            metrica = metricas_por_chave[key]
            item = notas_por_chave[key]
            nota = item["nota"]
            novas_avaliacoes.append(AvaliacaoPublicaLLM(
                juiz=juiz,
                resposta=resposta_publica,
                metrica=metrica,
                avaliacao_quanti=nota,
                avaliacao_quali=_formatar_justificativa_avaliacao(
                    item["justificativa"], nota, 5,
                ),
                justificativa_geral=justificativa,
                erro=False,
            ))
        AvaliacaoPublicaLLM.objects.bulk_create(novas_avaliacoes)


def _same_llm_identity(respondente, juiz):
    if respondente is None or juiz is None:
        return False
    if getattr(respondente, "pk", None) == getattr(juiz, "pk", None) and type(respondente) is type(juiz):
        return True
    # Duas configurações do mesmo modelo continuam sendo a mesma LLM, mesmo com chaves distintas.
    return normalize_metric_name(getattr(respondente, "nome", "")) == normalize_metric_name(
        getattr(juiz, "nome", "")
    )


def _executar_avaliacao_cruzada_publica(pergunta_publica, respostas_publicas, juizes, metricas):
    if not metricas:
        return {"status": "sem_metricas", "total": 0, "notas_total": 0, "mensagem": "Nenhuma métrica pública ativa."}

    if not juizes:
        return {"status": "sem_pares", "total": 0, "notas_total": 0, "mensagem": "Nenhuma LLM avaliadora está disponível."}

    respostas_validas = [resposta for resposta in respostas_publicas if resposta.ok and resposta.llm_id]
    tarefas = []
    for resposta_publica in respostas_validas:
        for juiz in juizes:
            if _same_llm_identity(resposta_publica.llm, juiz):
                continue
            tarefas.append((resposta_publica, juiz))

    if not tarefas:
        return {"status": "sem_pares", "total": 0, "notas_total": 0, "mensagem": "Não há pares válidos para avaliação cruzada."}

    tarefas_limitadas = tarefas[:PUBLIC_CROSS_EVAL_MAX_TASKS]
    limitou = len(tarefas_limitadas) < len(tarefas)

    resultados = []
    executor = get_llm_executor("evaluation")
    futures = {}
    for resposta_publica, juiz in tarefas_limitadas:
        prompt = _public_judge_prompt(pergunta_publica, resposta_publica, juiz, metricas)
        futures[executor.submit(_call_llm_in_worker, juiz, prompt)] = (resposta_publica, juiz)

    for future in as_completed(futures):
        resposta_publica, juiz = futures[future]
        try:
            raw = future.result()
            notas, justificativa = _parse_judgeai_result(raw, metricas)
            _salvar_avaliacoes_publicas(resposta_publica, juiz, metricas, notas, justificativa)
            resultados.append({"erro": False, "notas": notas})
        except Exception as exc:
            logger.exception(
                "Falha no JudgeAI público resposta_id=%s juiz_id=%s",
                resposta_publica.id,
                juiz.id,
            )
            resultados.append({"erro": True, "mensagem": str(exc), "notas": []})

    notas_total = sum(
        len([nota for nota in item.get("notas", []) if nota.get("nota") is not None])
        for item in resultados
        if not item.get("erro")
    )
    erros = sum(1 for item in resultados if item.get("erro"))

    status = "ok"
    if erros and notas_total:
        status = "parcial"
    elif erros:
        status = "erro"
    elif not notas_total:
        status = "sem_notas"

    return {
        "status": status,
        "total": len(resultados),
        "notas_total": notas_total,
        "erros": erros,
        "limitado": limitou,
        "mensagem": (
            "A resposta foi gerada, mas uma ou mais avaliações automáticas falharam."
            if erros else "Avaliação cruzada concluída."
        ),
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
        .order_by("resposta_id", "juiz__nome")
    )
    metric_order = {name: index for index, name in enumerate(JUDGE_METRIC_NAMES)}
    avaliacoes = sorted(
        qs,
        key=lambda item: (
            item.resposta_id,
            item.juiz.nome if item.juiz else "",
            metric_order.get(item.metrica.nome if item.metrica else "", 99),
        ),
    )
    if limite:
        avaliacoes = avaliacoes[:limite]

    linhas = []
    for avaliacao in avaliacoes:
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
        "juiz_id": juiz.id,
        "resposta_preview": _text_preview(resposta.conteudo_resposta),
        "resposta_texto": resposta.conteudo_resposta,
        "modelo_respondente": resposta.llm.nome if resposta.llm else "IA desconhecida",
        "modelo_juiz": juiz.nome,
        "notas": [],
        "justificativa": motivo,
        "erro": True,
    }


def _resultados_juizes_persistidos(usuario, metricas):
    """Reconstrói o formato do comparador a partir das avaliações salvas."""
    avaliacoes = (
        AvaliacaoJuiz.objects
        .filter(
            usuario=usuario,
            resposta__questao__usuario=usuario,
            metrica__in=metricas,
            avaliacao_quanti__isnull=False,
            erro=False,
        )
        .select_related("juiz", "resposta__questao", "resposta__llm", "metrica")
        .order_by("resposta__questao_id", "resposta_id", "juiz_id", "metrica_id")
    )
    metric_order = {
        judge_metric_key(nome): index
        for index, nome in enumerate(JUDGE_METRIC_NAMES)
    }
    resultados_por_par = {}

    for avaliacao in avaliacoes:
        chave = (avaliacao.resposta_id, avaliacao.juiz_id)
        resultado = resultados_por_par.setdefault(chave, {
            "questao_id": avaliacao.resposta.questao_id,
            "pergunta": avaliacao.resposta.questao.conteudo,
            "resposta_id": avaliacao.resposta_id,
            "juiz_id": avaliacao.juiz_id,
            "resposta_preview": _text_preview(avaliacao.resposta.conteudo_resposta),
            "resposta_texto": avaliacao.resposta.conteudo_resposta,
            "modelo_respondente": (
                avaliacao.resposta.llm.nome
                if avaliacao.resposta.llm else "IA desconhecida"
            ),
            "modelo_juiz": (
                avaliacao.juiz.nome
                if avaliacao.juiz else "LLM avaliadora removida"
            ),
            "notas": [],
            "justificativa": avaliacao.justificativa_geral or "",
            "erro": False,
        })
        if not resultado["justificativa"] and avaliacao.justificativa_geral:
            resultado["justificativa"] = avaliacao.justificativa_geral

        resultado["notas"].append({
            "metrica": avaliacao.metrica.nome,
            "nota": avaliacao.avaliacao_quanti,
            "max": _metric_max(avaliacao.metrica),
            "justificativa": (
                avaliacao.avaliacao_quali
                or avaliacao.justificativa_geral
                or ""
            ),
        })

    resultados = list(resultados_por_par.values())
    for resultado in resultados:
        resultado["notas"].sort(
            key=lambda item: metric_order.get(judge_metric_key(item["metrica"]), 99)
        )
    resultados.sort(key=lambda item: (
        item["questao_id"],
        item["modelo_respondente"],
        item["modelo_juiz"],
    ))
    return resultados


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

    metricas = ensure_judge_metrics(request.user)

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
            if _same_llm_identity(resposta.llm, juiz):
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
    executor = get_llm_executor("evaluation")
    futures = {}
    for questao, resposta, juiz in tarefas:
        prompt = _judgeai_prompt(questao, resposta, juiz, metricas)
        futures[executor.submit(_call_llm_in_worker, juiz, prompt)] = (questao, resposta, juiz)

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
                "juiz_id": juiz.id,
                "resposta_preview": _text_preview(resposta.conteudo_resposta),
                "resposta_texto": resposta.conteudo_resposta,
                "modelo_respondente": resposta.llm.nome if resposta.llm else "IA desconhecida",
                "modelo_juiz": juiz.nome,
                "notas": notas,
                "justificativa": justificativa,
                "erro": False,
            })
        except Exception as exc:
            logger.exception(
                "Falha no JudgeAI de pesquisador usuario_id=%s resposta_id=%s juiz_id=%s",
                request.user.id,
                resposta.id,
                juiz.id,
            )
            resultados.append(_judgeai_error_result(questao, resposta, juiz, str(exc)))

    resultados.sort(key=lambda item: (item["questao_id"], item["modelo_respondente"], item["modelo_juiz"]))

    erros_total = sum(1 for item in resultados if item.get("erro"))
    return JsonResponse({
        "status": "parcial" if erros_total else "ok",
        "total": len(resultados),
        "notas_total": sum(len(item.get("notas", [])) for item in resultados if not item.get("erro")),
        "erros_total": erros_total,
        "mensagem": (
            _("Uma ou mais avaliações falharam. Consulte o resultado de cada par.")
            if erros_total else _("Avaliação concluída.")
        ),
        "resultados": resultados,
    })

@login_required
def avaliacao(request):
    formularios = (
        Formulario.objects
        .filter(usuario=request.user)
        .annotate(
            questoes_total=Count("questoes", distinct=True),
            avaliadores_total=Count(
                Lower("avaliadores__email"),
                filter=Q(
                    avaliadores__finalizado_em__isnull=False,
                    avaliadores__avaliacoes__avaliacao_quanti__isnull=False,
                    avaliadores__avaliacoes__resposta__questao__formularios=F("pk"),
                ),
                distinct=True,
            ),
        )
        .prefetch_related(
            Prefetch(
                "questoes",
                queryset=Questao.objects.only("id"),
                to_attr="questoes_cache",
            ),
            Prefetch(
                "avaliadores",
                queryset=(
                    Avaliador.objects
                    .only(
                        "id",
                        "formulario_id",
                        "nome",
                        "email",
                        "profissao",
                        "data_resposta",
                        "finalizado_em",
                    )
                    .order_by("nome", "email")
                ),
                to_attr="avaliadores_cache",
            ),
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
    metricas = ensure_judge_metrics(request.user)

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'status': 'erro', 'mensagem': _('JSON inválido.')}, status=400)
        metrica_ids = {metrica.id for metrica in metricas}
        resposta_ids = set(respostas.values_list("id", flat=True))
        for item in data:
            if item.get('metrica_id') not in metrica_ids or item.get('resposta_id') not in resposta_ids:
                return JsonResponse({'status': 'erro', 'mensagem': _('Avaliação contém uma resposta ou métrica inválida.')}, status=400)
            try:
                nota = int(item.get('quanti'))
            except (TypeError, ValueError):
                return JsonResponse({'status': 'erro', 'mensagem': _('A nota deve ser um inteiro entre 1 e 5.')}, status=400)
            if nota < 1 or nota > 5:
                return JsonResponse({'status': 'erro', 'mensagem': _('A nota deve estar entre 1 e 5.')}, status=400)
            Avaliacao.objects.create(
                usuario=request.user,
                resposta_id=item['resposta_id'],
                metrica_id=item['metrica_id'],
                avaliacao_quanti=nota,
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


@login_required
@require_http_methods(["POST"])
def avaliacao_reabrir_avaliador(request, formulario_id, avaliador_id):
    with transaction.atomic():
        formulario = get_object_or_404(
            Formulario.objects.select_for_update(),
            id=formulario_id,
            usuario=request.user,
        )
        avaliador = get_object_or_404(
            Avaliador.objects.select_for_update(),
            id=avaliador_id,
            formulario=formulario,
        )
        was_already_open = avaliador.finalizado_em is None
        if not was_already_open:
            avaliador.finalizado_em = None
            avaliador.save(update_fields=["finalizado_em"])

    if was_already_open:
        django_messages.info(
            request,
            _("Esta participação já estava aberta para um novo envio."),
        )
    else:
        django_messages.success(
            request,
            _("A avaliação de %(nome)s foi reaberta.") % {
                "nome": avaliador.nome,
            },
        )

    return redirect("avaliacao")

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
    
    metricas = ensure_judge_metrics(formulario.usuario)

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
        email = request.POST.get('email', '').strip().lower()
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

        already_completed = False
        with transaction.atomic():
            # Serializa envios do mesmo formulário para impedir duas
            # finalizações simultâneas com o mesmo e-mail.
            Formulario.objects.select_for_update().only("id").get(
                id=formulario.id,
            )
            avaliador = (
                Avaliador.objects
                .select_for_update()
                .filter(formulario=formulario, email__iexact=email)
                .first()
            )

            if avaliador is not None and avaliador.finalizado_em is not None:
                already_completed = True
            else:
                if avaliador is None:
                    avaliador = Avaliador.objects.create(
                        email=email,
                        formulario=formulario,
                        nome=nome,
                        profissao=profissao,
                    )
                else:
                    avaliador.nome = nome
                    avaliador.profissao = profissao
                    avaliador.save(update_fields=["nome", "profissao"])

                # Uma participação reaberta preserva as notas anteriores até
                # o novo envio ser validado. Dentro desta transação, elas são
                # substituídas de forma atômica pelas notas completas.
                AvaliacaoFormulario.objects.filter(
                    avaliador=avaliador,
                    resposta_id__in=respostas_ids,
                    metrica_id__in=[
                        metrica.id for metrica in metricas_quantitativas
                    ],
                ).delete()

                for avaliacao_formulario in avaliacoes:
                    avaliacao_formulario.avaliador = avaliador
                AvaliacaoFormulario.objects.bulk_create(avaliacoes)
                avaliador.finalizado_em = timezone.now()
                avaliador.save(update_fields=["finalizado_em"])

        if already_completed:
            return render(
                request,
                "avaliacao/avaliacao_sucesso.html",
                {"ja_finalizada": True},
                status=409,
            )

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
    metricas_obj = ensure_judge_metrics(request.user)
    metricas = [
        {"id": metrica.id, "nome": metrica.nome, "pontuacao_maxima": 5}
        for metrica in metricas_obj
    ]
    metrica_ids = [metrica["id"] for metrica in metricas]
    llms = list(
        LLM.objects
        .filter(usuario=request.user)
        .order_by("-id")
        .values("id", "nome")
    )

    # Uma avaliação continua armazenada quando o pesquisador edita um
    # formulário e remove uma pergunta. Preserve o histórico no banco, mas
    # mostre no dashboard apenas notas das perguntas atualmente vinculadas ao
    # mesmo formulário do avaliador.
    avaliacoes_especialistas = AvaliacaoFormulario.objects.filter(
        usuario=request.user,
        avaliador__formulario__usuario=request.user,
        avaliador__finalizado_em__isnull=False,
        resposta__questao__formularios__id=F("avaliador__formulario_id"),
        metrica_id__in=metrica_ids,
        avaliacao_quanti__isnull=False,
    )

    por_metrica = {}
    divergencias = []
    especialistas_agregados = {
        (item["metrica_id"], item["resposta__llm_id"]): item
        for item in (
            avaliacoes_especialistas
            .values("metrica_id", "resposta__llm_id")
            .annotate(media=Avg("avaliacao_quanti"), total=Count("id"))
        )
    }
    juizes_agregados = {
        (item["metrica_id"], item["resposta__llm_id"]): item
        for item in (
            AvaliacaoJuiz.objects
            .filter(usuario=request.user, metrica_id__in=metrica_ids, avaliacao_quanti__isnull=False, erro=False)
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

    total_especialistas = avaliacoes_especialistas.count()
    total_juizes = AvaliacaoJuiz.objects.filter(
        usuario=request.user,
        erro=False,
        avaliacao_quanti__isnull=False,
    ).count()
    avaliadores_humanos = (
        avaliacoes_especialistas
        .annotate(email_normalizado=Lower("avaliador__email"))
        .values("email_normalizado")
        .distinct()
        .count()
    )
    avaliacoes_modelos = (
        avaliacoes_especialistas
        .exclude(resposta__llm_id=None)
        .annotate(email_normalizado=Lower("avaliador__email"))
        .values("email_normalizado", "resposta__llm_id")
        .distinct()
        .count()
    )
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
            "avaliacoes_modelos": avaliacoes_modelos,
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
    metricas = ensure_judge_metrics(request.user)
    resultados_data = _resultados_juizes_persistidos(request.user, metricas)
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
        "resultados_data": resultados_data,
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
    metricas_publicas = ensure_judge_metrics(None)
    total_llms = LLMPublica.objects.count()
    llms_ativas = LLMPublica.objects.filter(ativo=True).count()
    total_metricas_publicas = len(metricas_publicas)
    metricas_publicas_ativas = len(metricas_publicas)
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
        ensure_judge_metrics(None)
        django_messages.error(
            request,
            "As métricas públicas são fixas: Completude, Acurácia, Diretividade e Clareza (1 a 5).",
        )
        return redirect("admin_pondersec_metricas_publicas")

    metricas = ensure_judge_metrics(None)
    return render(request, "admin_pondersec/metricas_publicas.html", {
        "admin": request.admin_pondersec,
        "metricas": metricas,
    })


@admin_required
@require_http_methods(["PUT"])
def admin_pondersec_metrica_publica_editar(request, id):
    ensure_judge_metrics(None)
    return JsonResponse({
        "status": "erro",
        "mensagem": "As quatro métricas oficiais e a escala de 1 a 5 são fixas.",
    }, status=409)


@admin_required
@require_http_methods(["DELETE"])
def admin_pondersec_metrica_publica_deletar(request, id):
    ensure_judge_metrics(None)
    return JsonResponse({
        "status": "erro",
        "mensagem": "As quatro métricas oficiais não podem ser removidas.",
    }, status=409)


@admin_required
@require_http_methods(["POST"])
def admin_pondersec_metrica_publica_toggle(request, id):
    ensure_judge_metrics(None)
    return JsonResponse({
        "status": "erro",
        "mensagem": "As quatro métricas oficiais devem permanecer ativas.",
    }, status=409)


@admin_required
def admin_pondersec_avaliacoes_publicas(request):
    metricas = [
        {"id": metrica.id, "nome": metrica.nome, "pontuacao_maxima": 5}
        for metrica in ensure_judge_metrics(None)
    ]
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

    if not nome:
        return JsonResponse({"status": "erro", "mensagem": "Nome do modelo é obrigatório."}, status=400)

    llm.nome = nome
    fields = ["nome"]
    if api_key:
        llm.api_key = api_key
        fields.append("api_key")
    if descricao is not None:
        llm.descricao = descricao.strip()
        fields.append("descricao")
    if isinstance(ativo, bool):
        llm.ativo = ativo
        fields.append("ativo")
    llm.save(update_fields=fields)
    logger.info(
        "Configuração LLM pública atualizada admin_id=%s llm_id=%s model=%s key_updated=%s",
        request.admin_pondersec.id,
        llm.id,
        llm.nome,
        bool(api_key),
    )
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
