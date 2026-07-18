from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0013_unique_ai_response_per_question_llm"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="perguntapublica",
            index=models.Index(
                fields=["-criado_em"],
                name="perg_pub_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="questao",
            index=models.Index(
                fields=["usuario", "-id"],
                name="quest_user_id_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="historico",
            index=models.Index(
                fields=["usuario", "-data"],
                name="histor_user_data_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="avaliacaoformulario",
            index=models.Index(
                condition=models.Q(avaliacao_quanti__isnull=False),
                fields=["usuario", "metrica"],
                name="aval_form_user_metric_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="avaliacaojuiz",
            index=models.Index(
                condition=models.Q(
                    avaliacao_quanti__isnull=False,
                    erro=False,
                ),
                fields=["usuario", "metrica"],
                name="aval_judge_user_metric_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="avaliacaopublicallm",
            index=models.Index(
                condition=models.Q(
                    avaliacao_quanti__isnull=False,
                    erro=False,
                ),
                fields=["-atualizado_em"],
                name="aval_pub_updated_idx",
            ),
        ),
    ]
