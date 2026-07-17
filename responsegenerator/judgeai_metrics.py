import unicodedata

from django.db import transaction


JUDGE_METRICS = (
    {
        "key": "completude",
        "name": "Completude",
        "description": (
            "Verifica se a resposta cobre todos os pontos essenciais da pergunta, "
            "sem omissões que prejudiquem sua utilidade."
        ),
    },
    {
        "key": "acuracia",
        "name": "Acurácia",
        "description": (
            "Verifica se as afirmações, conceitos e orientações técnicas estão corretos "
            "e não contêm erros factuais."
        ),
    },
    {
        "key": "diretividade",
        "name": "Diretividade",
        "description": (
            "Verifica se a resposta atende diretamente ao que foi perguntado, mantendo "
            "foco e evitando conteúdo desnecessário."
        ),
    },
    {
        "key": "clareza",
        "name": "Clareza",
        "description": (
            "Verifica se a resposta é organizada, compreensível e não ambígua para o "
            "público a que se destina."
        ),
    },
)

JUDGE_METRIC_KEYS = tuple(item["key"] for item in JUDGE_METRICS)
JUDGE_METRIC_NAMES = tuple(item["name"] for item in JUDGE_METRICS)
LEGACY_JUDGE_METRIC_KEYS = {"fidelidade", "relevancia"}


def normalize_metric_name(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return "".join(char for char in without_accents if char.isalnum())


def judge_metric_key(value):
    normalized = normalize_metric_name(value)
    return normalized if normalized in JUDGE_METRIC_KEYS else None


@transaction.atomic
def ensure_judge_metrics(usuario):
    """Garante as quatro métricas canônicas e devolve-as na ordem oficial."""
    from responsegenerator.models import Metrica

    existing = list(Metrica.objects.select_for_update().filter(usuario=usuario).order_by("id"))
    by_key = {}
    for metric in existing:
        key = judge_metric_key(metric.nome)
        if key and key not in by_key:
            by_key[key] = metric

    result = []
    for definition in JUDGE_METRICS:
        metric = by_key.get(definition["key"])
        defaults = {
            "nome": definition["name"],
            "descricao": definition["description"],
            "tipo": "quantitativa",
            "pontuacao_maxima": 5,
            "criterio_texto": definition["description"],
            "label_opcao_1": None,
            "label_opcao_2": None,
            "ativa": True,
        }
        if metric is None:
            metric = Metrica.objects.create(usuario=usuario, **defaults)
        else:
            changed = []
            for field, value in defaults.items():
                if getattr(metric, field) != value:
                    setattr(metric, field, value)
                    changed.append(field)
            if changed:
                metric.save(update_fields=changed)
        result.append(metric)

    # Métricas antigas não podem voltar a participar de nenhuma avaliação.
    legacy_ids = [
        metric.id
        for metric in existing
        if normalize_metric_name(metric.nome) in LEGACY_JUDGE_METRIC_KEYS
    ]
    if legacy_ids:
        Metrica.objects.filter(id__in=legacy_ids).delete()

    noncanonical_ids = [
        metric.id
        for metric in existing
        if judge_metric_key(metric.nome) is None
        and normalize_metric_name(metric.nome) not in LEGACY_JUDGE_METRIC_KEYS
        and metric.ativa
    ]
    if noncanonical_ids:
        Metrica.objects.filter(id__in=noncanonical_ids).update(ativa=False)

    return result
