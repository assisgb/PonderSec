from django.db import migrations, models


def _move_judge_evaluations(
    AvaliacaoJuiz,
    response_ids,
    primary_id,
    database_alias,
):
    evaluations = list(
        AvaliacaoJuiz.objects.using(database_alias)
        .filter(resposta_id__in=response_ids)
        .order_by("-atualizado_em", "-id")
    )
    seen = set()
    winner_ids = []
    nullable_ids = []
    loser_ids = []
    for evaluation in evaluations:
        identity = (
            evaluation.usuario_id,
            evaluation.juiz_id,
            evaluation.metrica_id,
        )
        if evaluation.juiz_id is None or evaluation.metrica_id is None:
            nullable_ids.append(evaluation.id)
        elif identity in seen:
            loser_ids.append(evaluation.id)
        else:
            seen.add(identity)
            winner_ids.append(evaluation.id)

    # Remove primeiro os conflitos mais antigos; assim a avaliação realmente
    # mais recente pode ser movida para a resposta preservada sem violar a chave.
    if loser_ids:
        AvaliacaoJuiz.objects.using(database_alias).filter(id__in=loser_ids).delete()
    AvaliacaoJuiz.objects.using(database_alias).filter(
        id__in=winner_ids + nullable_ids,
    ).update(resposta_id=primary_id)


def deduplicate_ai_responses(apps, schema_editor):
    Resposta = apps.get_model("responsegenerator", "Resposta")
    Avaliacao = apps.get_model("responsegenerator", "Avaliacao")
    AvaliacaoFormulario = apps.get_model(
        "responsegenerator",
        "AvaliacaoFormulario",
    )
    AvaliacaoJuiz = apps.get_model("responsegenerator", "AvaliacaoJuiz")
    database_alias = schema_editor.connection.alias

    duplicate_pairs = list(
        Resposta.objects.using(database_alias)
        .filter(questao__isnull=False, llm__isnull=False)
        .values("questao_id", "llm_id")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
    )

    for pair in duplicate_pairs:
        response_ids = list(
            Resposta.objects.using(database_alias)
            .filter(
                questao_id=pair["questao_id"],
                llm_id=pair["llm_id"],
            )
            .order_by("-id")
            .values_list("id", flat=True)
        )
        primary_id = response_ids[0]
        duplicate_ids = response_ids[1:]

        Avaliacao.objects.using(database_alias).filter(
            resposta_id__in=duplicate_ids
        ).update(resposta_id=primary_id)
        AvaliacaoFormulario.objects.using(database_alias).filter(
            resposta_id__in=duplicate_ids
        ).update(resposta_id=primary_id)
        _move_judge_evaluations(
            AvaliacaoJuiz,
            response_ids,
            primary_id,
            database_alias,
        )

        Resposta.objects.using(database_alias).filter(
            id__in=duplicate_ids
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0012_fix_judgeai_metrics"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_ai_responses,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="resposta",
            constraint=models.UniqueConstraint(
                fields=("questao", "llm"),
                condition=models.Q(llm__isnull=False),
                name="unique_resposta_questao_llm",
            ),
        ),
    ]
