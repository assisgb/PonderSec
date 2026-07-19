from django.db import migrations, models


def deduplicate_form_scores(apps, schema_editor):
    AvaliacaoFormulario = apps.get_model(
        "responsegenerator",
        "AvaliacaoFormulario",
    )
    alias = schema_editor.connection.alias

    duplicate_groups = (
        AvaliacaoFormulario.objects.using(alias)
        .exclude(avaliador_id=None)
        .exclude(metrica_id=None)
        .values("avaliador_id", "resposta_id", "metrica_id")
        .annotate(latest_id=models.Max("id"), total=models.Count("id"))
        .filter(total__gt=1)
    )

    for group in duplicate_groups.iterator():
        (
            AvaliacaoFormulario.objects.using(alias)
            .filter(
                avaliador_id=group["avaliador_id"],
                resposta_id=group["resposta_id"],
                metrica_id=group["metrica_id"],
            )
            .exclude(id=group["latest_id"])
            .delete()
        )


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0017_evaluator_per_form"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_form_scores,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="avaliacaoformulario",
            constraint=models.UniqueConstraint(
                fields=("avaliador", "resposta", "metrica"),
                condition=(
                    models.Q(avaliador__isnull=False)
                    & models.Q(metrica__isnull=False)
                ),
                name="unique_form_score_per_evaluator",
            ),
        ),
    ]
