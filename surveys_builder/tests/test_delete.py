import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from .. import services
from ..models import BlockTextSection, QuestionBlock, QuestionOptions, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder


class SurveyDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='delete-staff', is_staff=True)
        cls.student = User.objects.create_user(username='delete-student')
        cls.survey = SurveysBuilder.objects.create(title='Удаляемый опрос', status=SurveysBuilder.Status.PUBLISHED)
        cls.block = QuestionBlock.objects.create(survey=cls.survey, title='Основной блок')
        cls.section = BlockTextSection.objects.create(block=cls.block, text='Текст блока')
        cls.question = QuestionsBuilder.objects.create(survey=cls.survey, block=cls.block, title='Вопрос', answer_type='single_choice')
        cls.option = QuestionOptions.objects.create(question=cls.question, text='Ответ')
        cls.assignment = SurveyAssignment.objects.create(survey=cls.survey, user=cls.student)
        cls.response = SurveyResponse.objects.create(assignment=cls.assignment, status=SurveyResponse.Status.SUBMITTED)
        cls.answer = SurveyAnswer.objects.create(response=cls.response, question=cls.question, value=cls.option.pk)
        cls.keep = SurveysBuilder.objects.create(title='Оставить этот опрос')
        keep_block = QuestionBlock.objects.create(survey=cls.keep, title='Оставить блок')
        keep_question = QuestionsBuilder.objects.create(survey=cls.keep, block=keep_block, title='Другой вопрос')
        keep_assignment = SurveyAssignment.objects.create(survey=cls.keep, user=cls.student)
        cls.keep_response = SurveyResponse.objects.create(assignment=keep_assignment)
        cls.keep_answer = SurveyAnswer.objects.create(response=cls.keep_response, question=keep_question, value='Сохранить')

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse('surveys_builder:survey_delete', args=[self.survey.pk])

    def test_list_has_delete_link_and_get_only_shows_confirmation(self):
        self.assertContains(self.client.get(reverse('staff_surveys_list')), self.url)
        response = self.client.get(self.url)
        self.assertContains(response, 'Удалить полностью')
        self.assertContains(response, 'В архив')
        self.assertContains(response, 'Полное удаление нельзя отменить')
        self.assertEqual(response.context['counts'], {'recipients': 1, 'responses': 1, 'submitted': 1})
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.survey.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_full_deletion_removes_submitted_answers_and_only_target_survey_dependents(self):
        response = self.client.post(self.url, {'revision': self.survey.revision, 'action': 'delete'})
        self.assertRedirects(response, reverse('staff_surveys_list'))
        for model, pk in [(SurveysBuilder, self.survey.pk), (QuestionBlock, self.block.pk),
                          (BlockTextSection, self.section.pk), (QuestionsBuilder, self.question.pk),
                          (QuestionOptions, self.option.pk), (SurveyAssignment, self.assignment.pk),
                          (SurveyResponse, self.response.pk), (SurveyAnswer, self.answer.pk)]:
            self.assertFalse(model.objects.filter(pk=pk).exists())
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.keep.pk).exists())
        self.assertTrue(SurveyResponse.objects.filter(pk=self.keep_response.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.keep_answer.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.student.pk).exists())

    def test_full_deletion_removes_draft_answers(self):
        SurveyResponse.objects.filter(pk=self.response.pk).update(status=SurveyResponse.Status.DRAFT)
        response = self.client.post(self.url, {'revision': self.survey.revision})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())
        self.assertFalse(SurveyResponse.objects.filter(pk=self.response.pk).exists())

    def test_published_survey_without_responses_can_be_deleted(self):
        survey = SurveysBuilder.objects.create(title='Без ответов', status=SurveysBuilder.Status.PUBLISHED)
        block = QuestionBlock.objects.create(survey=survey, title='Блок')
        question = QuestionsBuilder.objects.create(survey=survey, block=block, title='Вопрос')
        assignment = SurveyAssignment.objects.create(survey=survey, user=self.student)
        url = reverse('surveys_builder:survey_delete', args=[survey.pk])
        self.assertContains(self.client.get(url), 'Удалить полностью')
        response = self.client.post(url, {'revision': survey.revision})
        self.assertRedirects(response, reverse('staff_surveys_list'))
        self.assertFalse(SurveysBuilder.objects.filter(pk=survey.pk).exists())
        self.assertFalse(QuestionsBuilder.objects.filter(pk=question.pk).exists())
        self.assertFalse(SurveyAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.student.pk).exists())

    def test_direct_survey_delete_is_protected_by_assignments(self):
        with self.assertRaises(ProtectedError):
            self.survey.delete()
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_direct_assignment_delete_is_protected_by_response(self):
        with self.assertRaises(ProtectedError):
            self.assignment.delete()
        self.assertTrue(SurveyResponse.objects.filter(pk=self.response.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_api_cannot_delete_published_survey_with_answers(self):
        url = reverse('surveys_builder:surveys_api:detail', args=[self.survey.pk])
        response = self.client.delete(
            url,
            data=json.dumps({'revision': self.survey.revision}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_deletion_available_for_every_status(self):
        for status in SurveysBuilder.Status.values:
            survey = SurveysBuilder.objects.create(title=f'Удалить {status}', status=status)
            assignment = SurveyAssignment.objects.create(survey=survey, user=self.student)
            saved = SurveyResponse.objects.create(assignment=assignment, status=SurveyResponse.Status.SUBMITTED)
            response = self.client.post(reverse('surveys_builder:survey_delete', args=[survey.pk]), {'revision': survey.revision})
            self.assertEqual(response.status_code, 302)
            self.assertFalse(SurveysBuilder.objects.filter(pk=survey.pk).exists())
            self.assertFalse(SurveyResponse.objects.filter(pk=saved.pk).exists())

    def test_archive_from_delete_menu_keeps_content_and_answers(self):
        response = self.client.post(self.url, {'revision': self.survey.revision, 'action': 'archive'})
        self.assertRedirects(response, reverse('staff_surveys_list'))
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.status, SurveysBuilder.Status.ARCHIVED)
        self.assertFalse(self.survey.accepts_answers)
        self.assertIsNotNone(self.survey.closed_at)
        self.assertTrue(QuestionsBuilder.objects.filter(pk=self.question.pk).exists())
        self.assertTrue(SurveyAssignment.objects.filter(pk=self.assignment.pk).exists())
        self.assertTrue(SurveyResponse.objects.filter(pk=self.response.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())
        self.assertNotContains(self.client.get(self.url), 'value="archive"')

    def test_draft_can_be_archived_but_block_template_cannot(self):
        draft = SurveysBuilder.objects.create(title='Черновик')
        response = self.client.post(reverse('surveys_builder:survey_delete', args=[draft.pk]),
            {'revision': draft.revision, 'action': 'archive'})
        self.assertEqual(response.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, SurveysBuilder.Status.ARCHIVED)
        template = services.create_block_template(self.staff, {'title': 'Шаблон'})
        url = reverse('surveys_builder:survey_delete', args=[template.pk])
        self.assertNotContains(self.client.get(url), 'value="archive"')
        self.assertEqual(self.client.post(url, {'revision': template.revision, 'action': 'archive'}).status_code, 400)

    def test_archive_checks_revision_permissions_and_csrf(self):
        self.assertEqual(self.client.post(self.url, {'revision': 0, 'action': 'archive'}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'revision': self.survey.revision, 'action': 'unknown'}).status_code, 400)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url, {'revision': self.survey.revision, 'action': 'archive'}).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(self.url, {'revision': self.survey.revision, 'action': 'archive'}).status_code, 302)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.status, SurveysBuilder.Status.PUBLISHED)

    def test_stale_or_missing_revision_does_not_delete_anything(self):
        for data in ({}, {'revision': 0}):
            self.assertEqual(self.client.post(self.url, data).status_code, 400)
            self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_only_staff_can_delete_and_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url, {'revision': self.survey.revision}).status_code, 403)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'revision': self.survey.revision}).status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {'revision': self.survey.revision}).status_code, 302)
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.survey.pk).exists())

    def test_deletion_rolls_back_dependent_data_if_survey_delete_fails(self):
        with patch.object(SurveysBuilder, 'delete', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                services.delete_survey(self.survey.pk, self.staff, self.survey.revision, allow_published=True)
        self.assertTrue(SurveyAssignment.objects.filter(pk=self.assignment.pk).exists())
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.survey.pk).exists())
        self.assertTrue(SurveyResponse.objects.filter(pk=self.response.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())
