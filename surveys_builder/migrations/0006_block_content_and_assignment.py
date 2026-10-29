import django.db.models.deletion
from django.db import migrations, models
import surveys_builder.storage


def preserve_existing_content(apps, schema_editor):
    Survey = apps.get_model('surveys_builder', 'SurveysBuilder')
    Block = apps.get_model('surveys_builder', 'QuestionBlock')
    Question = apps.get_model('surveys_builder', 'QuestionsBuilder')
    Assignment = apps.get_model('surveys_builder', 'SurveyAssignment')
    db = schema_editor.connection.alias
    allowed = {'grades', 'profiles', 'statuses', 'subjects', 'planned_exams', 'schools', 'courses', 'steps'}
    for survey in Survey.objects.using(db).all().iterator():
        blocks = list(Block.objects.using(db).filter(survey=survey).order_by('position', 'pk'))
        ungrouped = Question.objects.using(db).filter(survey=survey, block=None)
        if ungrouped.exists():
            for position, block in enumerate(blocks, 2):
                Block.objects.using(db).filter(pk=block.pk).update(position=position)
            main = Block.objects.using(db).create(survey=survey, title='Основной блок', position=1)
            ungrouped.update(block=main)
            blocks.insert(0, main)
        for block in blocks:
            for position, question in enumerate(Question.objects.using(db).filter(block=block).order_by('position', 'pk'), 1):
                Question.objects.using(db).filter(pk=question.pk).update(block_position=position)
        Assignment.objects.using(db).filter(survey=survey).update(visible_block_ids=[block.pk for block in blocks])
        if survey.status == 'draft':
            filters = {key: value for key, value in survey.audience_filters.items() if key in allowed}
            filters['grades'] = [str(value) for value in filters.get('grades', []) if str(value) in ('10', '11')]
            Survey.objects.using(db).filter(pk=survey.pk).update(audience_filters=filters, audience_configured=False)


class Migration(migrations.Migration):
    dependencies = [('surveys_builder', '0005_question_blocks')]
    operations = [
        migrations.AddField(model_name='surveysbuilder', name='is_block_template',
                            field=models.BooleanField(default=False, db_index=True)),
        migrations.AddField(model_name='questionblock', name='visibility_filters',
                            field=models.JSONField(default=dict, blank=True)),
        migrations.AddField(model_name='questionsbuilder', name='block_position',
                            field=models.PositiveIntegerField(default=1)),
        migrations.AddField(model_name='surveyassignment', name='visible_block_ids',
                            field=models.JSONField(default=list)),
        migrations.CreateModel(name='BlockTextSection', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('title', models.CharField('Заголовок секции', max_length=200, blank=True)),
            ('text', models.TextField('Текст', max_length=20000, blank=True)),
            ('image', models.ImageField('Картинка', upload_to='surveys/sections/%Y/%m/', blank=True,
                                       storage=surveys_builder.storage.PrivateSurveyImageStorage())),
            ('image_alt', models.CharField('Описание картинки', max_length=500, blank=True)),
            ('position', models.PositiveIntegerField(default=1)),
            ('block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='text_sections', to='surveys_builder.questionblock')),
        ], options={'ordering': ['position', 'pk']}),
        migrations.RunPython(preserve_existing_content, migrations.RunPython.noop),
        migrations.AlterField(model_name='questionsbuilder', name='block',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='questions', to='surveys_builder.questionblock', verbose_name='Блок')),
    ]
