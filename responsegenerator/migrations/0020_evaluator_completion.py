from django.db import migrations, models


def mark_existing_evaluators_as_completed(apps, schema_editor):
    Avaliador = apps.get_model("responsegenerator", "Avaliador")
    AvaliacaoFormulario = apps.get_model(
        "responsegenerator",
        "AvaliacaoFormulario",
    )
    alias = schema_editor.connection.alias

    completed_ids = (
        AvaliacaoFormulario.objects.using(alias)
        .exclude(avaliador_id=None)
        .filter(avaliacao_quanti__isnull=False)
        .values_list("avaliador_id", flat=True)
        .distinct()
    )
    Avaliador.objects.using(alias).filter(id__in=completed_ids).update(
        finalizado_em=models.F("data_resposta"),
    )


class Migration(migrations.Migration):
    dependencies = [
        (
            "responsegenerator",
            "0019_repair_evaluator_score_form_links",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="avaliador",
            name="finalizado_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            mark_existing_evaluators_as_completed,
            migrations.RunPython.noop,
        ),
    ]
