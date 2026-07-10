# Generated manually for resposta_humana field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("responsegenerator", "0009_chat_publico_avaliacao_cruzada"),
    ]

    operations = [
        migrations.AddField(
            model_name="questao",
            name="resposta_humana",
            field=models.TextField(blank=True, null=True, verbose_name="Resposta Humana"),
        ),
    ]
