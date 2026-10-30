from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys_builder', '0008_surveyassignment_nullable_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='questionsbuilder',
            name='visibility_filters',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='surveyassignment',
            name='visible_question_ids',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
