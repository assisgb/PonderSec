import unicodedata

from django.db import migrations, models


DEFINITIONS = (
    (
        "completude",
        "Completude",
        "Verifica se a resposta cobre todos os pontos essenciais da pergunta, sem omissões que prejudiquem sua utilidade.",
    ),
    (
        "acuracia",
        "Acurácia",
        "Verifica se as afirmações, conceitos e orientações técnicas estão corretos e não contêm erros factuais.",
    ),
    (
        "diretividade",
        "Diretividade",
        "Verifica se a resposta atende diretamente ao que foi perguntado, mantendo foco e evitando conteúdo desnecessário.",
    ),
    (
        "clareza",
        "Clareza",
        "Verifica se a resposta é organizada, compreensível e não ambígua para o público a que se destina.",
    ),
)


def _normalize(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    return "".join(
        char
        for char in normalized
        if not unicodedata.combining(char) and char.isalnum()
    )


def _move_evaluations(apps, duplicate, primary):
    Avaliacao = apps.get_model("responsegenerator", "Avaliacao")
    AvaliacaoFormulario = apps.get_model("responsegenerator", "AvaliacaoFormulario")
    AvaliacaoJuiz = apps.get_model("responsegenerator", "AvaliacaoJuiz")
    AvaliacaoPublicaLLM = apps.get_model("responsegenerator", "AvaliacaoPublicaLLM")

    Avaliacao.objects.filter(metrica_id=duplicate.id).update(metrica_id=primary.id)
    AvaliacaoFormulario.objects.filter(metrica_id=duplicate.id).update(metrica_id=primary.id)

    for item in AvaliacaoJuiz.objects.filter(metrica_id=duplicate.id).iterator():
        AvaliacaoJuiz.objects.update_or_create(
            usuario_id=item.usuario_id,
            juiz_id=item.juiz_id,
            resposta_id=item.resposta_id,
            metrica_id=primary.id,
            defaults={
                "avaliacao_quali": item.avaliacao_quali,
                "avaliacao_quanti": item.avaliacao_quanti,
                "justificativa_geral": item.justificativa_geral,
                "erro": item.erro,
            },
        )
    AvaliacaoJuiz.objects.filter(metrica_id=duplicate.id).delete()

    for item in AvaliacaoPublicaLLM.objects.filter(metrica_id=duplicate.id).iterator():
        AvaliacaoPublicaLLM.objects.update_or_create(
            juiz_id=item.juiz_id,
            resposta_id=item.resposta_id,
            metrica_id=primary.id,
            defaults={
                "avaliacao_quali": item.avaliacao_quali,
                "avaliacao_quanti": item.avaliacao_quanti,
                "justificativa_geral": item.justificativa_geral,
                "erro": item.erro,
            },
        )
    AvaliacaoPublicaLLM.objects.filter(metrica_id=duplicate.id).delete()


def forwards(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Metrica = apps.get_model("responsegenerator", "Metrica")
    Avaliacao = apps.get_model("responsegenerator", "Avaliacao")
    AvaliacaoFormulario = apps.get_model("responsegenerator", "AvaliacaoFormulario")
    AvaliacaoJuiz = apps.get_model("responsegenerator", "AvaliacaoJuiz")
    AvaliacaoPublicaLLM = apps.get_model("responsegenerator", "AvaliacaoPublicaLLM")

    canonical_keys = {item[0] for item in DEFINITIONS}
    legacy_keys = {"fidelidade", "relevancia"}
    owners = [None, *User.objects.values_list("id", flat=True)]

    for owner_id in owners:
        metrics = list(Metrica.objects.filter(usuario_id=owner_id).order_by("id"))
        by_key = {}
        for metric in metrics:
            key = _normalize(metric.nome)
            if key in canonical_keys:
                by_key.setdefault(key, []).append(metric)

        for key, name, description in DEFINITIONS:
            candidates = by_key.get(key, [])
            if candidates:
                primary = candidates[0]
            else:
                primary = Metrica.objects.create(
                    usuario_id=owner_id,
                    nome=name,
                    descricao=description,
                    tipo="quantitativa",
                    pontuacao_maxima=5,
                    criterio_texto=description,
                    ativa=True,
                )

            primary.nome = name
            primary.descricao = description
            primary.tipo = "quantitativa"
            primary.pontuacao_maxima = 5
            primary.criterio_texto = description
            primary.label_opcao_1 = None
            primary.label_opcao_2 = None
            primary.ativa = True
            primary.save()

            for duplicate in candidates[1:]:
                _move_evaluations(apps, duplicate, primary)
                duplicate.delete()

        for metric in Metrica.objects.filter(usuario_id=owner_id):
            key = _normalize(metric.nome)
            if key in legacy_keys:
                AvaliacaoJuiz.objects.filter(metrica_id=metric.id).delete()
                AvaliacaoPublicaLLM.objects.filter(metrica_id=metric.id).delete()
                metric.delete()
            elif key not in canonical_keys and metric.ativa:
                metric.ativa = False
                metric.save(update_fields=["ativa"])

    # Notas inválidas antigas não são convertidas em notas válidas artificialmente.
    AvaliacaoJuiz.objects.filter(
        models.Q(avaliacao_quanti__lt=1) | models.Q(avaliacao_quanti__gt=5)
    ).delete()
    AvaliacaoPublicaLLM.objects.filter(
        models.Q(avaliacao_quanti__lt=1) | models.Q(avaliacao_quanti__gt=5)
    ).delete()
    Avaliacao.objects.filter(
        models.Q(avaliacao_quanti__lt=1) | models.Q(avaliacao_quanti__gt=5)
    ).delete()
    AvaliacaoFormulario.objects.filter(
        models.Q(avaliacao_quanti__lt=1) | models.Q(avaliacao_quanti__gt=5)
    ).delete()

    canonical_metric_ids = [
        metric.id
        for metric in Metrica.objects.all()
        if _normalize(metric.nome) in canonical_keys
    ]
    AvaliacaoJuiz.objects.exclude(metrica_id__in=canonical_metric_ids).delete()
    AvaliacaoPublicaLLM.objects.exclude(metrica_id__in=canonical_metric_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0011_add_tipo_respostas_to_formulario"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="avaliacao",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="research_score_between_1_and_5",
            ),
        ),
        migrations.AddConstraint(
            model_name="avaliacaoformulario",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="form_score_between_1_and_5",
            ),
        ),
        migrations.AddConstraint(
            model_name="avaliacaojuiz",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="judge_score_between_1_and_5",
            ),
        ),
        migrations.AddConstraint(
            model_name="avaliacaopublicallm",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(avaliacao_quanti__isnull=True)
                    | models.Q(avaliacao_quanti__gte=1, avaliacao_quanti__lte=5)
                ),
                name="public_judge_score_between_1_and_5",
            ),
        ),
    ]
