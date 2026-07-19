from django.db import migrations


def repair_evaluator_score_form_links(apps, schema_editor):
    """Move legacy scores to the evaluator row for their unambiguous form."""
    Avaliador = apps.get_model("responsegenerator", "Avaliador")
    AvaliacaoFormulario = apps.get_model(
        "responsegenerator",
        "AvaliacaoFormulario",
    )
    Formulario = apps.get_model("responsegenerator", "Formulario")
    alias = schema_editor.connection.alias

    evaluators = {
        evaluator.id: evaluator
        for evaluator in Avaliador.objects.using(alias).all().iterator()
    }
    target_evaluators = {}
    response_forms = {}

    scores = (
        AvaliacaoFormulario.objects.using(alias)
        .exclude(avaliador_id=None)
        .exclude(usuario_id=None)
        .order_by("id")
    )
    for score in scores.iterator():
        source_evaluator = evaluators.get(score.avaliador_id)
        if source_evaluator is None:
            continue

        response_key = (score.usuario_id, score.resposta_id)
        if response_key not in response_forms:
            candidate_ids = list(
                Formulario.objects.using(alias)
                .filter(
                    usuario_id=score.usuario_id,
                    questoes__respostas__id=score.resposta_id,
                )
                .values_list("id", flat=True)
                .distinct()[:2]
            )
            response_forms[response_key] = (
                candidate_ids[0] if len(candidate_ids) == 1 else None
            )

        target_form_id = response_forms[response_key]
        if (
            target_form_id is None
            or source_evaluator.formulario_id == target_form_id
        ):
            continue

        evaluator_key = (source_evaluator.email, target_form_id)
        target_evaluator = target_evaluators.get(evaluator_key)
        if target_evaluator is None:
            target_evaluator, created = (
                Avaliador.objects.using(alias).get_or_create(
                    email=source_evaluator.email,
                    formulario_id=target_form_id,
                    defaults={
                        "nome": source_evaluator.nome,
                        "profissao": source_evaluator.profissao,
                    },
                )
            )
            if created:
                Avaliador.objects.using(alias).filter(
                    pk=target_evaluator.pk,
                ).update(data_resposta=source_evaluator.data_resposta)
                target_evaluator.data_resposta = source_evaluator.data_resposta
                evaluators[target_evaluator.id] = target_evaluator
            target_evaluators[evaluator_key] = target_evaluator

        conflicting_score = None
        if score.metrica_id is not None:
            conflicting_score = (
                AvaliacaoFormulario.objects.using(alias)
                .filter(
                    avaliador_id=target_evaluator.id,
                    resposta_id=score.resposta_id,
                    metrica_id=score.metrica_id,
                )
                .exclude(pk=score.pk)
                .order_by("-id")
                .first()
            )

        if conflicting_score is not None:
            if conflicting_score.id > score.id:
                score.delete(using=alias)
                continue
            conflicting_score.delete(using=alias)

        AvaliacaoFormulario.objects.using(alias).filter(pk=score.pk).update(
            avaliador_id=target_evaluator.id,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0018_unique_form_evaluation"),
    ]

    operations = [
        migrations.RunPython(
            repair_evaluator_score_form_links,
            migrations.RunPython.noop,
        ),
    ]
