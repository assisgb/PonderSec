from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from django.conf import settings


_EXECUTORS = {}
_EXECUTORS_LOCK = Lock()


def get_llm_executor(kind):
    """Retorna pools persistentes e limitados por processo para chamadas externas."""
    settings_by_kind = {
        "generation": ("LLM_MODELS_MAX_WORKERS", 4),
        "evaluation": ("LLM_EVALUATION_MAX_WORKERS", 4),
    }
    try:
        setting_name, default = settings_by_kind[kind]
    except KeyError as exc:
        raise ValueError(f"Pool de LLM desconhecido: {kind}") from exc

    workers = max(1, min(16, int(getattr(settings, setting_name, default))))
    cache_key = (kind, workers)
    executor = _EXECUTORS.get(cache_key)
    if executor is not None:
        return executor

    with _EXECUTORS_LOCK:
        executor = _EXECUTORS.get(cache_key)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"pondersec-{kind}",
            )
            _EXECUTORS[cache_key] = executor
    return executor
