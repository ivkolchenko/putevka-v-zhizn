import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys_builder', '0009_questionsbuilder_visibility_filters_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='questionsbuilder',
            name='show_if_option',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='dependent_questions', to='surveys_builder.questionoptions', verbose_name='Показывать при выборе варианта'),
        ),
    ]
