from django.db import migrations, models


def restore_evaluator_form_links(apps, schema_editor):
    """Recover form counters hidden by the former globally unique e-mail."""
    Avaliador = apps.get_model("responsegenerator", "Avaliador")
    AvaliacaoFormulario = apps.get_model("responsegenerator", "AvaliacaoFormulario")
    alias = schema_editor.connection.alias

    evaluators = list(Avaliador.objects.using(alias).all())
    for evaluator in evaluators:
        form_ids = set(
            AvaliacaoFormulario.objects.using(alias)
            .filter(avaliador_id=evaluator.id)
            .values_list("resposta__questao__formularios__id", flat=True)
        )
        form_ids.discard(None)

        for form_id in form_ids:
            clone, created = Avaliador.objects.using(alias).get_or_create(
                email=evaluator.email,
                formulario_id=form_id,
                defaults={
                    "nome": evaluator.nome,
                    "profissao": evaluator.profissao,
                },
            )
            if created:
                Avaliador.objects.using(alias).filter(pk=clone.pk).update(
                    data_resposta=evaluator.data_resposta,
                )


def merge_evaluators_by_email(apps, schema_editor):
    Avaliador = apps.get_model("responsegenerator", "Avaliador")
    AvaliacaoFormulario = apps.get_model("responsegenerator", "AvaliacaoFormulario")
    alias = schema_editor.connection.alias

    duplicate_emails = (
        Avaliador.objects.using(alias)
        .values("email")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
    )
    for group in duplicate_emails:
        evaluators = list(
            Avaliador.objects.using(alias)
            .filter(email=group["email"])
            .order_by("-data_resposta", "-id")
        )
        keeper = evaluators[0]
        duplicate_ids = [evaluator.id for evaluator in evaluators[1:]]
        AvaliacaoFormulario.objects.using(alias).filter(
            avaliador_id__in=duplicate_ids,
        ).update(avaliador_id=keeper.id)
        Avaliador.objects.using(alias).filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0016_unique_metric_names"),
    ]

    operations = [
        migrations.AlterField(
            model_name="avaliador",
            name="email",
            field=models.EmailField(max_length=254),
        ),
        migrations.RunPython(
            restore_evaluator_form_links,
            merge_evaluators_by_email,
        ),
        migrations.AddConstraint(
            model_name="avaliador",
            constraint=models.UniqueConstraint(
                fields=("email", "formulario"),
                name="unique_evaluator_email_per_form",
            ),
        ),
    ]
