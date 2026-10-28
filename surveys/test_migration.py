from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LegacyResponseRemovalTests(TransactionTestCase):
    old = ('surveys', '0002_surveysubmission_submission_key')
    new = ('surveys', '0003_remove_legacy_responses')

    def test_migration_refuses_to_remove_nonempty_legacy_tables(self):
        MigrationExecutor(connection).migrate([self.old])
        apps = MigrationExecutor(connection).loader.project_state([self.old]).apps
        submission_model = apps.get_model('surveys', 'SurveySubmission')
        user = get_user_model().objects.create_user(username='legacy-migration-student')
        submission = submission_model.objects.create(
            user_id=user.pk,
            source_survey_id=1,
            survey_title='Старый опрос',
        )
        try:
            with self.assertRaisesMessage(RuntimeError, 'Удаление таблиц остановлено'):
                MigrationExecutor(connection).migrate([self.new])
            self.assertTrue(submission_model.objects.filter(pk=submission.pk).exists())
        finally:
            submission_model.objects.filter(pk=submission.pk).delete()
            MigrationExecutor(connection).migrate([self.new])
