import hashlib
import logging
import threading
import time
from collections import OrderedDict

from django.conf import settings
from django.utils.translation import gettext as _

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

try:
    import openai
except ImportError:
    openai = None

try:
    from groq import Groq
except ImportError:
    Groq = None


logger = logging.getLogger("responsegenerator.llm")


# Os SDKs mantêm pools HTTP internamente. Reaproveitá-los evita repetir DNS/TLS
# em toda pergunta. O cache é local à thread porque os SDKs não documentam uma
# garantia comum de thread-safety e os workers do projeto usam gthread.
_CLIENT_CACHE_TTL_SECONDS = 300.0
_CLIENT_CACHE_MAX_SIZE = 8
_TRANSIENT_MAX_ATTEMPTS = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 0.2
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
    if mapped_code in {"quota_exceeded", "authentication_error", "model_not_found"}:
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
        "Iniciando chamada LLM provider=%s model=%s key_fp=%s timeout_s=%s",
        provider, model, fingerprint, timeout,
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
                response = client.models.generate_content(model=model, contents=prompt)
                result = _gemini_response_text(response)
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
        "Iniciando stream LLM provider=%s model=%s key_fp=%s timeout_s=%s",
        provider, model, fingerprint, timeout,
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
