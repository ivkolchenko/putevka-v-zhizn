import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('surveys_builder', '0005_question_blocks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='surveyresponse',
            name='assignment',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='response',
                to='surveys_builder.surveyassignment',
            ),
        ),
    ]
