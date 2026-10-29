from django.db import migrations


def require_empty_legacy_tables(apps, schema_editor):
    alias = schema_editor.connection.alias
    submission = apps.get_model('surveys', 'SurveySubmission')
    answer = apps.get_model('surveys', 'SurveyAnswer')
    if submission.objects.using(alias).exists() or answer.objects.using(alias).exists():
        raise RuntimeError('Старые ответы на опросы найдены. Удаление таблиц остановлено.')


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0002_surveysubmission_submission_key'),
    ]

    operations = [
        migrations.RunPython(require_empty_legacy_tables, migrations.RunPython.noop),
        migrations.DeleteModel(name='SurveyAnswer'),
        migrations.DeleteModel(name='SurveySubmission'),
    ]
