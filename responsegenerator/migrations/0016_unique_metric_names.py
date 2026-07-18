from django.db import migrations, models


def _keep_latest_unique_evaluations(
    Evaluation,
    metric_ids,
    primary_id,
    identity_fields,
    database_alias,
):
    evaluations = list(
        Evaluation.objects.using(database_alias)
        .filter(metrica_id__in=metric_ids)
        .order_by("-atualizado_em", "-id")
    )
    seen = set()
    loser_ids = []
    winner_ids = []
    nullable_ids = []

    for evaluation in evaluations:
        identity = tuple(getattr(evaluation, field) for field in identity_fields)
        if any(value is None for value in identity):
            nullable_ids.append(evaluation.id)
        elif identity in seen:
            loser_ids.append(evaluation.id)
        else:
            seen.add(identity)
            winner_ids.append(evaluation.id)

    if loser_ids:
        Evaluation.objects.using(database_alias).filter(id__in=loser_ids).delete()
    Evaluation.objects.using(database_alias).filter(
        id__in=winner_ids + nullable_ids,
    ).update(metrica_id=primary_id)


def deduplicate_metric_names(apps, schema_editor):
    Metrica = apps.get_model("responsegenerator", "Metrica")
    Avaliacao = apps.get_model("responsegenerator", "Avaliacao")
    AvaliacaoFormulario = apps.get_model("responsegenerator", "AvaliacaoFormulario")
    AvaliacaoJuiz = apps.get_model("responsegenerator", "AvaliacaoJuiz")
    AvaliacaoPublicaLLM = apps.get_model("responsegenerator", "AvaliacaoPublicaLLM")
    alias = schema_editor.connection.alias

    duplicate_groups = list(
        Metrica.objects.using(alias)
        .values("usuario_id", "nome")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
    )

    for group in duplicate_groups:
        metric_ids = list(
            Metrica.objects.using(alias)
            .filter(usuario_id=group["usuario_id"], nome=group["nome"])
            .order_by("id")
            .values_list("id", flat=True)
        )
        primary_id = metric_ids[0]
        duplicate_ids = metric_ids[1:]

        Avaliacao.objects.using(alias).filter(
            metrica_id__in=duplicate_ids,
        ).update(metrica_id=primary_id)
        AvaliacaoFormulario.objects.using(alias).filter(
            metrica_id__in=duplicate_ids,
        ).update(metrica_id=primary_id)
        _keep_latest_unique_evaluations(
            AvaliacaoJuiz,
            metric_ids,
            primary_id,
            ("usuario_id", "juiz_id", "resposta_id"),
            alias,
        )
        _keep_latest_unique_evaluations(
            AvaliacaoPublicaLLM,
            metric_ids,
            primary_id,
            ("juiz_id", "resposta_id"),
            alias,
        )
        Metrica.objects.using(alias).filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0015_public_evaluation_claim"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_metric_names,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="metrica",
            constraint=models.UniqueConstraint(
                condition=models.Q(usuario__isnull=False),
                fields=("usuario", "nome"),
                name="unique_metric_user_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="metrica",
            constraint=models.UniqueConstraint(
                condition=models.Q(usuario__isnull=True),
                fields=("nome",),
                name="unique_global_metric_name",
            ),
        ),
    ]
