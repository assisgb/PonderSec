import hashlib
import logging
import time

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
    # Não há cache de cliente ou de credencial: cada chamada relê a chave persistida.
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


def _mapped_error(exc, provider):
    raw = str(exc or "")
    lowered = raw.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    status_text = str(status or "").lower()

    if (
        status == 429
        or "429" in status_text
        or "resource_exhausted" in lowered
        or "quota" in lowered
        or "rate limit" in lowered
        or "rate_limit" in lowered
    ):
        return LLMServiceError(
            _("A cota ou o limite de requisições do provedor foi atingido. Verifique o plano/chave e tente novamente mais tarde."),
            code="quota_exceeded",
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
    client = None

    logger.info(
        "Iniciando chamada LLM provider=%s model=%s key_fp=%s timeout_s=%s",
        provider, model, fingerprint, timeout,
    )
    try:
        if provider == "gemini":
            client = _gemini_client(api_key, timeout)
            response = client.models.generate_content(model=model, contents=prompt)
            result = _gemini_response_text(response)
        elif provider == "groq":
            if Groq is None:
                raise LLMServiceError(_("Biblioteca groq não instalada."), code="dependency_missing")
            client = Groq(api_key=api_key, timeout=timeout, max_retries=0)
            response = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}], model=model,
            )
            result = _chat_completion_text(response)
        elif provider in {"openai", "deepseek"}:
            if openai is None:
                raise LLMServiceError(_("Biblioteca openai não instalada."), code="dependency_missing")
            kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
            if provider == "deepseek":
                kwargs["base_url"] = "https://integrate.api.nvidia.com/v1"
            client = openai.OpenAI(**kwargs)
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
            )
            result = _chat_completion_text(response)
        else:
            raise LLMServiceError(
                _("Provedor '%(provedor)s' não reconhecido.") % {
                    "provedor": getattr(llm, "descricao", None) or model
                },
                code="unknown_provider",
            )
    except LLMServiceError:
        logger.warning(
            "Chamada LLM rejeitada provider=%s model=%s key_fp=%s elapsed_ms=%d",
            provider, model, fingerprint, int((time.monotonic() - started) * 1000),
        )
        raise
    except Exception as exc:
        mapped = _mapped_error(exc, provider)
        logger.error(
            "Falha na chamada LLM provider=%s model=%s key_fp=%s code=%s exception_type=%s elapsed_ms=%d",
            provider, model, fingerprint, mapped.code, type(exc).__name__,
            int((time.monotonic() - started) * 1000),
        )
        raise mapped from None
    finally:
        if client is not None:
            _close_client(client)

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


def stream_configured_llm(llm, prompt):
    api_key = _fresh_credentials(llm)
    provider = _provider_name(llm)
    model = getattr(llm, "nome", "")
    timeout = _timeout_seconds(stream=True)
    started = time.monotonic()
    fingerprint = _key_fingerprint(api_key)
    client = None
    chars = 0

    logger.info(
        "Iniciando stream LLM provider=%s model=%s key_fp=%s timeout_s=%s",
        provider, model, fingerprint, timeout,
    )
    try:
        if provider == "gemini":
            client = _gemini_client(api_key, timeout)
            stream = client.models.generate_content_stream(model=model, contents=prompt)
            for chunk in stream:
                try:
                    content = getattr(chunk, "text", None)
                except (AttributeError, ValueError):
                    content = None
                if content:
                    chars += len(str(content))
                    yield str(content)
        elif provider == "groq":
            if Groq is None:
                raise LLMServiceError(_("Biblioteca groq não instalada."), code="dependency_missing")
            client = Groq(api_key=api_key, timeout=timeout, max_retries=0)
            stream = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}], model=model, stream=True,
            )
            for content in _chat_completion_stream_text(stream):
                chars += len(content)
                yield content
        elif provider in {"openai", "deepseek"}:
            if openai is None:
                raise LLMServiceError(_("Biblioteca openai não instalada."), code="dependency_missing")
            kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
            if provider == "deepseek":
                kwargs["base_url"] = "https://integrate.api.nvidia.com/v1"
            client = openai.OpenAI(**kwargs)
            stream = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}], stream=True,
            )
            for content in _chat_completion_stream_text(stream):
                chars += len(content)
                yield content
        else:
            raise LLMServiceError(
                _("Provedor '%(provedor)s' não reconhecido.") % {
                    "provedor": getattr(llm, "descricao", None) or model
                },
                code="unknown_provider",
            )

        if chars == 0:
            raise LLMServiceError(
                _("O provedor encerrou a solicitação sem retornar conteúdo."),
                code="empty_response",
            )
    except LLMServiceError:
        logger.warning(
            "Stream LLM rejeitado provider=%s model=%s key_fp=%s elapsed_ms=%d chars=%d",
            provider, model, fingerprint, int((time.monotonic() - started) * 1000), chars,
        )
        raise
    except Exception as exc:
        mapped = _mapped_error(exc, provider)
        logger.error(
            "Falha no stream LLM provider=%s model=%s key_fp=%s code=%s exception_type=%s elapsed_ms=%d chars=%d",
            provider, model, fingerprint, mapped.code, type(exc).__name__,
            int((time.monotonic() - started) * 1000), chars,
        )
        raise mapped from None
    finally:
        if client is not None:
            _close_client(client)

    logger.info(
        "Stream LLM concluído provider=%s model=%s key_fp=%s elapsed_ms=%d chars=%d",
        provider, model, fingerprint, int((time.monotonic() - started) * 1000), chars,
    )
