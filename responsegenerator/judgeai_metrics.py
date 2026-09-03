import unicodedata


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

JUDGE_METRIC_NAMES = tuple(item["name"] for item in JUDGE_METRICS)


def normalize_metric_name(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return "".join(char for char in without_accents if char.isalnum())


def _metric_defaults(definition):
    return {
        "nome": definition["name"],
        "descricao": definition["description"],
        "tipo": "quantitativa",
        "pontuacao_maxima": 5,
        "criterio_texto": definition["description"],
        "label_opcao_1": None,
        "label_opcao_2": None,
        "ativa": True,
    }


def ensure_judge_metrics(usuario, *, include_inactive=False):
    """Cria os padrões no primeiro uso sem sobrescrever métricas configuradas."""
    from responsegenerator.models import Metrica

    queryset = Metrica.objects.filter(usuario=usuario)
    if not queryset.exists():
        # Um único INSERT em lote evita deixar só parte dos padrões caso dois
        # workers inicializem o mesmo conjunto simultaneamente. As constraints
        # de unicidade resolvem a eventual corrida sem alterar registros atuais.
        Metrica.objects.bulk_create(
            [Metrica(usuario=usuario, **_metric_defaults(item)) for item in JUDGE_METRICS],
            ignore_conflicts=True,
        )

    queryset = Metrica.objects.filter(usuario=usuario).order_by("id")
    if not include_inactive:
        queryset = queryset.filter(ativa=True)
    return list(queryset)
