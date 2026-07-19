import hashlib
import importlib
import logging
import threading
import time
from collections import OrderedDict

from django.conf import settings
from django.utils.translation import gettext as _


# Os SDKs dos provedores importam milhares de classes (principalmente o
# google-genai/Pydantic). Carregá-los junto com o URLconf fazia o primeiro acesso
# de cada worker pagar vários segundos mesmo sem executar uma chamada de IA.
_DEPENDENCY_UNLOADED = object()
_dependency_lock = threading.Lock()
genai = _DEPENDENCY_UNLOADED
genai_types = _DEPENDENCY_UNLOADED
openai = _DEPENDENCY_UNLOADED
Groq = _DEPENDENCY_UNLOADED


def _load_provider_dependency(provider):
    global genai, genai_types, openai, Groq

    if provider == "gemini" and genai is _DEPENDENCY_UNLOADED:
        with _dependency_lock:
            if genai is _DEPENDENCY_UNLOADED:
                try:
                    loaded_genai = importlib.import_module("google.genai")
                    loaded_types = importlib.import_module("google.genai.types")
                except ImportError:
                    loaded_genai = None
                    loaded_types = None
                genai = loaded_genai
                genai_types = loaded_types
    elif provider in {"openai", "deepseek"} and openai is _DEPENDENCY_UNLOADED:
        with _dependency_lock:
            if openai is _DEPENDENCY_UNLOADED:
                try:
                    openai = importlib.import_module("openai")
                except ImportError:
                    openai = None
    elif provider == "groq" and Groq is _DEPENDENCY_UNLOADED:
        with _dependency_lock:
            if Groq is _DEPENDENCY_UNLOADED:
                try:
                    Groq = importlib.import_module("groq").Groq
                except ImportError:
                    Groq = None


def preload_provider_dependencies():
    """Carrega SDKs antes do fork do Gunicorn, sem criar clientes ou conexões."""
    for provider in ("gemini", "openai", "groq"):
        _load_provider_dependency(provider)


logger = logging.getLogger("responsegenerator.llm")


# Os SDKs mantêm pools HTTP internamente. Reaproveitá-los evita repetir DNS/TLS
# em toda pergunta. O cache é local à thread porque os SDKs não documentam uma
# garantia comum de thread-safety e os workers do projeto usam gthread.
_CLIENT_CACHE_TTL_SECONDS = 300.0
_CLIENT_CACHE_MAX_SIZE = 8
_TRANSIENT_MAX_ATTEMPTS = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 0.2
_GEMINI_AUTH_KEY_PREFIX = "AQ."
_client_cache_state = threading.local()


class LLMServiceError(RuntimeError):
    def __init__(self, message, *, code="provider_error"):
        super().__init__(message)
        self.code = code


def _provider_name(llm):
    value = f"{getattr(llm, 'descricao', '') or ''} {getattr(llm, 'nome', '') or ''}".lower()
    if "gemini" in value or "google" in value:
        return "gemini"
    if "groq" in value or "llama" in value or "mixtral" in value:
        return "groq"
    if "deepseek" in value:
        return "deepseek"
    if "openai" in value or "gpt" in value or "chatgpt" in value:
        return "openai"
    return "unknown"


def _fresh_credentials(llm):
    # A credencial nunca é cacheada separadamente: cada chamada relê o banco. O
    # digest da chave atual seleciona (ou invalida naturalmente) o cliente HTTP.
    if getattr(llm, "pk", None) and hasattr(llm, "refresh_from_db"):
        llm.refresh_from_db(fields=["nome", "descricao", "api_key"])
    api_key = (getattr(llm, "api_key", "") or "").strip()
    if not api_key:
        raise LLMServiceError(_("A chave da API não está configurada para este modelo."), code="missing_api_key")
    return api_key


def _key_fingerprint(api_key):
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]


def _gemini_key_kind(api_key):
    """Classifica a credencial sem restringir formatos futuros do Google."""
    if api_key.startswith(_GEMINI_AUTH_KEY_PREFIX):
        return "authorization"
    if api_key.startswith("AIza"):
        return "standard"
    return "unknown"


def _timeout_seconds(stream=False):
    setting_name = "LLM_STREAM_TIMEOUT_SECONDS" if stream else "LLM_REQUEST_TIMEOUT_SECONDS"
    return max(1.0, float(getattr(settings, setting_name, 45)))


def _close_client(client):
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("Falha não crítica ao fechar cliente de LLM", exc_info=True)


def _bounded_setting(name, default, minimum, maximum, cast):
    try:
        value = cast(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _client_cache_policy():
    ttl = _bounded_setting(
        "LLM_CLIENT_CACHE_TTL_SECONDS", _CLIENT_CACHE_TTL_SECONDS, 1.0, 3600.0, float,
    )
    max_size = _bounded_setting(
        "LLM_CLIENT_CACHE_MAX_SIZE", _CLIENT_CACHE_MAX_SIZE, 1, 32, int,
    )
    return ttl, max_size


def _client_cache():
    cache = getattr(_client_cache_state, "clients", None)
    if cache is None:
        cache = OrderedDict()
        _client_cache_state.clients = cache
    return cache


def _client_factory_identity(provider):
    _load_provider_dependency(provider)
    if provider == "gemini":
        factory = getattr(genai, "Client", None) if genai is not None else None
    elif provider == "groq":
        factory = Groq
    else:
        factory = getattr(openai, "OpenAI", None) if openai is not None else None
    # Incluir a identidade também impede que mocks de testes reutilizem um
    # cliente construído por um patch anterior na mesma thread.
    return factory


def _client_cache_key(provider, api_key, timeout):
    key_digest = hashlib.sha256(api_key.encode("utf-8")).digest()
    return provider, key_digest, float(timeout), _client_factory_identity(provider)


def _prune_client_cache(cache, now, ttl, max_size):
    expired = [
        key for key, (last_used, _client) in cache.items()
        if now - last_used >= ttl
    ]
    for key in expired:
        _last_used, client = cache.pop(key)
        _close_client(client)

    while len(cache) > max_size:
        _key, (_last_used, client) = cache.popitem(last=False)
        _close_client(client)


def _new_provider_client(provider, api_key, timeout):
    if provider == "gemini":
        return _gemini_client(api_key, timeout)
    if provider == "groq":
        if Groq is None:
            raise LLMServiceError(_("Biblioteca groq não instalada."), code="dependency_missing")
        return Groq(api_key=api_key, timeout=timeout, max_retries=0)
    if provider in {"openai", "deepseek"}:
        if openai is None:
            raise LLMServiceError(_("Biblioteca openai não instalada."), code="dependency_missing")
        kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if provider == "deepseek":
            kwargs["base_url"] = "https://api.deepseek.com"
        return openai.OpenAI(**kwargs)
    raise LLMServiceError(
        _("Provedor de IA não reconhecido."),
        code="unknown_provider",
    )


def _cached_provider_client(provider, api_key, timeout):
    cache = _client_cache()
    ttl, max_size = _client_cache_policy()
    now = time.monotonic()
    _prune_client_cache(cache, now, ttl, max_size)
    cache_key = _client_cache_key(provider, api_key, timeout)
    entry = cache.pop(cache_key, None)
    if entry is not None:
        _last_used, client = entry
        cache[cache_key] = (now, client)
        return cache_key, client

    client = _new_provider_client(provider, api_key, timeout)
    cache[cache_key] = (now, client)
    _prune_client_cache(cache, now, ttl, max_size)
    return cache_key, client


def _discard_cached_client(cache_key, client):
    if cache_key is None or client is None:
        return
    cache = _client_cache()
    entry = cache.get(cache_key)
    if entry is not None and entry[1] is client:
        cache.pop(cache_key, None)
        _close_client(client)


def _exception_status(exc):
    candidates = [getattr(exc, "status_code", None), getattr(exc, "code", None)]
    response = getattr(exc, "response", None)
    if response is not None:
        candidates.append(getattr(response, "status_code", None))

    for value in candidates:
        if isinstance(value, int):
            return value
        try:
            text = str(value or "").strip()
            if text.isdigit():
                return int(text)
        except Exception:
            continue
    return None


def _is_transient_error(exc, provider):
    mapped_code = _mapped_error(exc, provider).code
    if mapped_code in {
        "quota_exceeded",
        "authentication_error",
        "gemini_auth_key_rejected",
        "model_not_found",
    }:
        return False
    if mapped_code in {"timeout", "rate_limited"}:
        return True

    status = _exception_status(exc)
    if status in {408, 409, 425, 500, 502, 503, 504}:
        return True

    lowered = f"{type(exc).__name__} {exc}".lower()
    transient_markers = (
        "apiconnectionerror",
        "connecterror",
        "connection error",
        "connection reset",
        "connection refused",
        "networkerror",
        "remoteprotocolerror",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "server disconnected",
        "econnreset",
    )
    return isinstance(exc, ConnectionError) or any(marker in lowered for marker in transient_markers)


def _transient_max_attempts():
    return _bounded_setting(
        "LLM_TRANSIENT_MAX_ATTEMPTS", _TRANSIENT_MAX_ATTEMPTS, 1, 3, int,
    )


def _retry_after_seconds(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None) or {}
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
        return max(0.0, float(value))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _retry_delay(attempt, exc=None):
    base = _bounded_setting(
        "LLM_TRANSIENT_RETRY_DELAY_SECONDS",
        _TRANSIENT_RETRY_DELAY_SECONDS,
        0.0,
        5.0,
        float,
    )
    exponential = base * (2 ** max(0, attempt - 1))
    return min(5.0, max(exponential, _retry_after_seconds(exc)))


def _mapped_error(exc, provider):
    raw = str(exc or "")
    lowered = raw.lower()
    raw_status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    status = _exception_status(exc)
    status_text = str(status if status is not None else raw_status or "").lower()

    if "resource_exhausted" in lowered or "quota" in lowered:
        return LLMServiceError(
            _("A cota ou o limite de requisições do provedor foi atingido. Verifique o plano/chave e tente novamente mais tarde."),
            code="quota_exceeded",
        )
    if status == 429 or "429" in status_text or "rate limit" in lowered or "rate_limit" in lowered:
        return LLMServiceError(
            _("O limite momentâneo de requisições do provedor foi atingido. Aguarde e tente novamente."),
            code="rate_limited",
        )
    if provider == "gemini" and (
        "access_token_type_unsupported" in lowered
        or "api_key_service_blocked" in lowered
    ):
        return LLMServiceError(
            _(
                "A chave de autorização do Gemini (AQ.) foi aceita pelo PonderSec, "
                "mas o Google recusou o vínculo dela com a conta de serviço. "
                "Verifique a chave/projeto no Google AI Studio ou use uma chave "
                "padrão restrita à Generative Language API."
            ),
            code="gemini_auth_key_rejected",
        )
    if (
        status in (401, 403)
        or status_text in {"401", "403", "unauthenticated", "permission_denied"}
        or "api_key_invalid" in lowered
        or "invalid api key" in lowered
        or "invalid_api_key" in lowered
        or "permission denied" in lowered
    ):
        return LLMServiceError(
            _("A chave da API é inválida, expirou ou não tem permissão para usar este modelo. Atualize a chave e tente novamente."),
            code="authentication_error",
        )
    if (
        isinstance(exc, TimeoutError)
        or "timeout" in exc.__class__.__name__.lower()
        or "timed out" in lowered
        or "deadline exceeded" in lowered
        or "deadline_exceeded" in lowered
    ):
        return LLMServiceError(
            _("O provedor demorou além do tempo limite para responder. Tente novamente."),
            code="timeout",
        )
    if status == 404 or status_text == "404" or "model not found" in lowered or "not_found" in lowered:
        return LLMServiceError(
            _("O modelo configurado não foi encontrado ou não está disponível para esta chave."),
            code="model_not_found",
        )
    return LLMServiceError(
        _("O provedor de IA não conseguiu concluir a solicitação. Tente novamente ou revise a configuração do modelo."),
        code=f"{provider}_provider_error",
    )


def _gemini_client(api_key, timeout):
    _load_provider_dependency("gemini")
    if genai is None:
        raise LLMServiceError(_("Biblioteca google-genai não instalada."), code="dependency_missing")
    kwargs = {"api_key": api_key}
    if genai_types is not None:
        kwargs["http_options"] = genai_types.HttpOptions(timeout=int(timeout * 1000))
    return genai.Client(**kwargs)


def _gemini_response_text(response):
    try:
        text = getattr(response, "text", None)
    except (AttributeError, ValueError):
        text = None
    if text and str(text).strip():
        return str(text).strip()

    candidates = getattr(response, "candidates", None) or []
    parts_text = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            part_text = getattr(part, "text", None)
            if part_text:
                parts_text.append(str(part_text))
    if parts_text:
        return "".join(parts_text).strip()

    raise LLMServiceError(
        _("O Gemini encerrou a solicitação sem retornar conteúdo. Verifique filtros de segurança e o modelo configurado."),
        code="empty_response",
    )


def _gemini_interaction_response_text(response):
    try:
        text = getattr(response, "output_text", None)
    except (AttributeError, ValueError):
        text = None
    if text and str(text).strip():
        return str(text).strip()

    raise LLMServiceError(
        _(
            "O Gemini encerrou a interação sem retornar conteúdo. "
            "Verifique filtros de segurança e o modelo configurado."
        ),
        code="empty_response",
    )


def _gemini_generate_text(client, api_key, model, prompt):
    # As novas chaves AQ. são vinculadas a contas de serviço. A Interactions
    # API é o fluxo atual recomendado pelo Google para essas credenciais; as
    # chaves padrão/legadas permanecem no generateContent para não alterar o
    # comportamento das integrações já existentes.
    if _gemini_key_kind(api_key) == "authorization":
        interactions = getattr(client, "interactions", None)
        create = getattr(interactions, "create", None)
        if not callable(create):
            raise LLMServiceError(
                _(
                    "A versão instalada do google-genai não oferece suporte às "
                    "novas chaves de autorização do Gemini."
                ),
                code="dependency_missing",
            )
        response = create(model=model, input=prompt)
        return _gemini_interaction_response_text(response)

    response = client.models.generate_content(model=model, contents=prompt)
    return _gemini_response_text(response)


def _gemini_interaction_stream_text(interaction_stream):
    for event in interaction_stream:
        event_type = (
            event.get("event_type") if isinstance(event, dict)
            else getattr(event, "event_type", None)
        )
        if event_type == "error":
            error = event.get("error") if isinstance(event, dict) else getattr(event, "error", None)
            raise RuntimeError(str(error or "Gemini interaction stream error"))
        if event_type != "step.delta":
            continue

        delta = event.get("delta") if isinstance(event, dict) else getattr(event, "delta", None)
        delta_type = delta.get("type") if isinstance(delta, dict) else getattr(delta, "type", None)
        content = delta.get("text") if isinstance(delta, dict) else getattr(delta, "text", None)
        if delta_type == "text" and content:
            yield str(content)


def _chat_completion_text(response):
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMServiceError(_("O provedor encerrou a solicitação sem retornar conteúdo."), code="empty_response")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not content or not str(content).strip():
        raise LLMServiceError(_("O provedor encerrou a solicitação sem retornar conteúdo."), code="empty_response")
    return str(content).strip()


def call_configured_llm(llm, prompt):
    api_key = _fresh_credentials(llm)
    provider = _provider_name(llm)
    model = getattr(llm, "nome", "")
    timeout = _timeout_seconds()
    started = time.monotonic()
    fingerprint = _key_fingerprint(api_key)
    max_attempts = _transient_max_attempts()

    logger.info(
        "Iniciando chamada LLM provider=%s model=%s key_kind=%s key_fp=%s timeout_s=%s",
        provider,
        model,
        _gemini_key_kind(api_key) if provider == "gemini" else "api_key",
        fingerprint,
        timeout,
    )
    for attempt in range(1, max_attempts + 1):
        cache_key = None
        client = None
        try:
            if provider not in {"gemini", "groq", "openai", "deepseek"}:
                raise LLMServiceError(
                    _("Provedor '%(provedor)s' não reconhecido.") % {
                        "provedor": getattr(llm, "descricao", None) or model
                    },
                    code="unknown_provider",
                )

            cache_key, client = _cached_provider_client(provider, api_key, timeout)
            if provider == "gemini":
                result = _gemini_generate_text(client, api_key, model, prompt)
            else:
                response = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                )
                result = _chat_completion_text(response)
            break
        except LLMServiceError:
            logger.warning(
                "Chamada LLM rejeitada provider=%s model=%s key_fp=%s attempt=%d elapsed_ms=%d",
                provider, model, fingerprint, attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise
        except Exception as exc:
            transient = _is_transient_error(exc, provider)
            if transient:
                # Um pool cuja conexão falhou não deve ser entregue à tentativa
                # seguinte; ela abre uma conexão limpa e a recacheia.
                _discard_cached_client(cache_key, client)
            if transient and attempt < max_attempts:
                delay = _retry_delay(attempt, exc)
                logger.warning(
                    "Falha transitória LLM; nova tentativa provider=%s model=%s "
                    "key_fp=%s attempt=%d/%d delay_s=%.2f exception_type=%s",
                    provider, model, fingerprint, attempt, max_attempts, delay,
                    type(exc).__name__,
                )
                if delay:
                    time.sleep(delay)
                continue

            mapped = _mapped_error(exc, provider)
            logger.error(
                "Falha na chamada LLM provider=%s model=%s key_fp=%s code=%s "
                "exception_type=%s attempt=%d elapsed_ms=%d",
                provider, model, fingerprint, mapped.code, type(exc).__name__, attempt,
                int((time.monotonic() - started) * 1000),
            )
            raise mapped from None

    logger.info(
        "Chamada LLM concluída provider=%s model=%s key_fp=%s elapsed_ms=%d chars=%d",
        provider, model, fingerprint, int((time.monotonic() - started) * 1000), len(result),
    )
    return result


def _chat_completion_stream_text(completion_stream):
    for chunk in completion_stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None
        if content:
            yield str(content)


def _close_stream(stream):
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("Falha não crítica ao fechar stream de LLM", exc_info=True)


def stream_configured_llm(llm, prompt):
    api_key = _fresh_credentials(llm)
    provider = _provider_name(llm)
    model = getattr(llm, "nome", "")
    timeout = _timeout_seconds(stream=True)
    started = time.monotonic()
    fingerprint = _key_fingerprint(api_key)
    chars = 0
    max_attempts = _transient_max_attempts()

    logger.info(
        "Iniciando stream LLM provider=%s model=%s key_kind=%s key_fp=%s timeout_s=%s",
        provider,
        model,
        _gemini_key_kind(api_key) if provider == "gemini" else "api_key",
        fingerprint,
        timeout,
    )
    for attempt in range(1, max_attempts + 1):
        cache_key = None
        client = None
        stream = None
        try:
            if provider not in {"gemini", "groq", "openai", "deepseek"}:
                raise LLMServiceError(
                    _("Provedor '%(provedor)s' não reconhecido.") % {
                        "provedor": getattr(llm, "descricao", None) or model
                    },
                    code="unknown_provider",
                )

            cache_key, client = _cached_provider_client(provider, api_key, timeout)
            if provider == "gemini":
                if _gemini_key_kind(api_key) == "authorization":
                    interactions = getattr(client, "interactions", None)
                    create = getattr(interactions, "create", None)
                    if not callable(create):
                        raise LLMServiceError(
                            _(
                                "A versão instalada do google-genai não oferece suporte às "
                                "novas chaves de autorização do Gemini."
                            ),
                            code="dependency_missing",
                        )
                    stream = create(model=model, input=prompt, stream=True)
                    for content in _gemini_interaction_stream_text(stream):
                        chars += len(content)
                        yield content
                else:
                    stream = client.models.generate_content_stream(model=model, contents=prompt)
                    for chunk in stream:
                        try:
                            content = getattr(chunk, "text", None)
                        except (AttributeError, ValueError):
                            content = None
                        if content:
                            content = str(content)
                            chars += len(content)
                            yield content
            else:
                stream = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )
                for content in _chat_completion_stream_text(stream):
                    chars += len(content)
                    yield content

            if chars == 0:
                raise LLMServiceError(
                    _("O provedor encerrou a solicitação sem retornar conteúdo."),
                    code="empty_response",
                )
            break
        except LLMServiceError:
            logger.warning(
                "Stream LLM rejeitado provider=%s model=%s key_fp=%s attempt=%d elapsed_ms=%d chars=%d",
                provider, model, fingerprint, attempt,
                int((time.monotonic() - started) * 1000), chars,
            )
            raise
        except Exception as exc:
            transient = _is_transient_error(exc, provider)
            if transient:
                _discard_cached_client(cache_key, client)
            # Depois do primeiro trecho não é seguro reiniciar: isso duplicaria
            # conteúdo que o consumidor já recebeu.
            if transient and chars == 0 and attempt < max_attempts:
                delay = _retry_delay(attempt, exc)
                logger.warning(
                    "Falha transitória antes do primeiro trecho; nova tentativa "
                    "provider=%s model=%s key_fp=%s attempt=%d/%d delay_s=%.2f "
                    "exception_type=%s",
                    provider, model, fingerprint, attempt, max_attempts, delay,
                    type(exc).__name__,
                )
                if delay:
                    time.sleep(delay)
                continue

            mapped = _mapped_error(exc, provider)
            logger.error(
                "Falha no stream LLM provider=%s model=%s key_fp=%s code=%s "
                "exception_type=%s attempt=%d elapsed_ms=%d chars=%d",
                provider, model, fingerprint, mapped.code, type(exc).__name__, attempt,
                int((time.monotonic() - started) * 1000), chars,
            )
            raise mapped from None
        finally:
            if stream is not None:
                _close_stream(stream)

    logger.info(
        "Stream LLM concluído provider=%s model=%s key_fp=%s elapsed_ms=%d chars=%d",
        provider, model, fingerprint, int((time.monotonic() - started) * 1000), chars,
    )
