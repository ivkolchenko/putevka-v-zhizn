import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from . import services
from .models import QuestionOptions, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder


class SurveyDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='delete-staff', is_staff=True)
        cls.student = User.objects.create_user(username='delete-student')
        cls.survey = SurveysBuilder.objects.create(title='Удаляемый опрос', status=SurveysBuilder.Status.PUBLISHED)
        cls.question = QuestionsBuilder.objects.create(survey=cls.survey, title='Вопрос', answer_type='single_choice')
        cls.option = QuestionOptions.objects.create(question=cls.question, text='Ответ')
        cls.assignment = SurveyAssignment.objects.create(survey=cls.survey, user=cls.student)
        cls.response = SurveyResponse.objects.create(assignment=cls.assignment, status=SurveyResponse.Status.SUBMITTED)
        cls.answer = SurveyAnswer.objects.create(response=cls.response, question=cls.question, value=cls.option.pk)
        cls.keep = SurveysBuilder.objects.create(title='Оставить этот опрос')

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse('surveys_builder:survey_delete', args=[self.survey.pk])

    def test_list_has_delete_link_and_get_only_shows_confirmation(self):
        self.assertContains(self.client.get(reverse('staff_surveys_list')), self.url)
        response = self.client.get(self.url)
        self.assertContains(response, 'Удаление недоступно')
        self.assertNotContains(response, 'type="submit"')
        self.assertEqual(response.context['counts'], {'recipients': 1, 'responses': 1, 'submitted': 1})
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.survey.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_submitted_answers_block_deletion_and_preserve_dependents(self):
        for status in (
            SurveysBuilder.Status.PUBLISHED,
            SurveysBuilder.Status.CLOSED,
            SurveysBuilder.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                SurveysBuilder.objects.filter(pk=self.survey.pk).update(status=status)
                response = self.client.post(self.url, {'revision': self.survey.revision})
                self.assertContains(response, 'Нельзя удалить опрос с ответами или черновиками', status_code=400)
        for model, pk in [(SurveysBuilder, self.survey.pk), (QuestionsBuilder, self.question.pk),
                          (QuestionOptions, self.option.pk), (SurveyAssignment, self.assignment.pk),
                          (SurveyResponse, self.response.pk), (SurveyAnswer, self.answer.pk)]:
            self.assertTrue(model.objects.filter(pk=pk).exists())
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.keep.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.student.pk).exists())

    def test_draft_answers_block_deletion(self):
        SurveyResponse.objects.filter(pk=self.response.pk).update(status=SurveyResponse.Status.DRAFT)
        response = self.client.post(self.url, {'revision': self.survey.revision})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_published_survey_without_responses_can_be_deleted(self):
        survey = SurveysBuilder.objects.create(title='Без ответов', status=SurveysBuilder.Status.PUBLISHED)
        question = QuestionsBuilder.objects.create(survey=survey, title='Вопрос')
        assignment = SurveyAssignment.objects.create(survey=survey, user=self.student)
        url = reverse('surveys_builder:survey_delete', args=[survey.pk])
        self.assertContains(self.client.get(url), 'Удалить опрос и назначения')
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
            response = self.client.post(reverse('surveys_builder:survey_delete', args=[survey.pk]), {'revision': survey.revision})
            self.assertEqual(response.status_code, 302)
            self.assertFalse(SurveysBuilder.objects.filter(pk=survey.pk).exists())

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
        survey = SurveysBuilder.objects.create(title='Без ответов', status=SurveysBuilder.Status.PUBLISHED)
        assignment = SurveyAssignment.objects.create(survey=survey, user=self.student)
        with patch.object(SurveysBuilder, 'delete', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                services.delete_survey(survey.pk, self.staff, survey.revision, allow_published=True)
        self.assertTrue(SurveyAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertTrue(SurveysBuilder.objects.filter(pk=survey.pk).exists())
