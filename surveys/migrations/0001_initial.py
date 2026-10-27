import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('surveys_builder', '0003_alter_questionsbuilder_answer_type_questionoptions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SurveySubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_survey_id', models.PositiveBigIntegerField(db_index=True, verbose_name='ID исходного опроса')),
                ('survey_title', models.CharField(max_length=200, verbose_name='Название опроса при отправке')),
                ('survey_description', models.TextField(blank=True, verbose_name='Описание опроса при отправке')),
                ('submitted_at', models.DateTimeField(auto_now_add=True, verbose_name='Отправлено')),
                ('survey', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='submissions', to='surveys_builder.surveysbuilder', verbose_name='Исходный опрос')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='survey_submissions', to=settings.AUTH_USER_MODEL, verbose_name='Ученик')),
            ],
            options={
                'ordering': ['-submitted_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='SurveyAnswer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_question_id', models.PositiveBigIntegerField(verbose_name='ID исходного вопроса')),
                ('question_title', models.CharField(max_length=500, verbose_name='Вопрос при отправке')),
                ('question_type', models.CharField(choices=[('short_text', 'Короткий текст'), ('long_text', 'Развернутый текст'), ('single_choice', 'Один вариант ответа')], max_length=20, verbose_name='Тип вопроса при отправке')),
                ('question_position', models.PositiveIntegerField(verbose_name='Порядок вопроса при отправке')),
                ('source_option_id', models.PositiveBigIntegerField(blank=True, null=True, verbose_name='ID выбранного варианта')),
                ('option_text', models.CharField(blank=True, max_length=200, verbose_name='Выбранный вариант при отправке')),
                ('text_value', models.TextField(blank=True, verbose_name='Текст ответа')),
                ('option', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='student_answers', to='surveys_builder.questionoptions', verbose_name='Исходный вариант')),
                ('question', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='student_answers', to='surveys_builder.questionsbuilder', verbose_name='Исходный вопрос')),
                ('submission', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='answers', to='surveys.surveysubmission', verbose_name='Прохождение')),
            ],
            options={
                'ordering': ['question_position', 'source_question_id'],
            },
        ),
        migrations.AddIndex(
            model_name='surveysubmission',
            index=models.Index(fields=['user', 'source_survey_id'], name='surveys_sur_user_id_ed27c6_idx'),
        ),
        migrations.AddConstraint(
            model_name='surveyanswer',
            constraint=models.UniqueConstraint(fields=('submission', 'source_question_id'), name='surveys_one_answer_per_question_submission'),
        ),
    ]
