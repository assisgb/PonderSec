from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("responsegenerator", "0014_add_hot_query_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="respostapublica",
            name="avaliacao_estado",
            field=models.CharField(
                choices=[
                    ("pendente", "Pendente"),
                    ("processando", "Processando"),
                    ("concluida", "Concluída"),
                ],
                default="pendente",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="respostapublica",
            name="avaliacao_iniciada_em",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="respostapublica",
            name="avaliacao_claim_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
    ]
