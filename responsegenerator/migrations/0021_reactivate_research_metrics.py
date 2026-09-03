from django.db import migrations


def reactivate_research_metrics(apps, schema_editor):
    Metrica = apps.get_model("responsegenerator", "Metrica")
    Metrica.objects.using(schema_editor.connection.alias).filter(
        usuario_id__isnull=False,
        ativa=False,
    ).update(ativa=True)


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0020_evaluator_completion"),
    ]

    operations = [
        migrations.RunPython(
            reactivate_research_metrics,
            migrations.RunPython.noop,
        ),
    ]
