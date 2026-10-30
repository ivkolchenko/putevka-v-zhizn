import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys_builder', '0010_questionsbuilder_show_if_option'),
    ]

    operations = [
        migrations.AddField(
            model_name='questionblock',
            name='show_if_option',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='dependent_blocks', to='surveys_builder.questionoptions', verbose_name='Показывать при выборе варианта'),
        ),
    ]
