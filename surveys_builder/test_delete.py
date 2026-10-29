from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from . import services
from .models import QuestionBlock, QuestionOptions, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder


class SurveyDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='delete-staff', is_staff=True)
        cls.student = User.objects.create_user(username='delete-student')
        cls.survey = SurveysBuilder.objects.create(title='Удаляемый опрос', status=SurveysBuilder.Status.PUBLISHED)
        cls.question = QuestionsBuilder.objects.create(survey=cls.survey, block=QuestionBlock.objects.create(survey=cls.survey, title='Основной блок'), title='Вопрос', answer_type='single_choice')
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
        self.assertContains(response, 'Удалить опрос и все ответы')
        self.assertEqual(response.context['counts'], {'recipients': 1, 'responses': 1, 'submitted': 1})
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.survey.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())

    def test_confirmed_deletion_removes_dependents_and_keeps_other_surveys_and_users(self):
        response = self.client.post(self.url, {'revision': self.survey.revision})
        self.assertRedirects(response, reverse('staff_surveys_list'))
        for model, pk in [(SurveysBuilder, self.survey.pk), (QuestionsBuilder, self.question.pk),
                          (QuestionOptions, self.option.pk), (SurveyAssignment, self.assignment.pk),
                          (SurveyResponse, self.response.pk), (SurveyAnswer, self.answer.pk)]:
            self.assertFalse(model.objects.filter(pk=pk).exists())
        self.assertTrue(SurveysBuilder.objects.filter(pk=self.keep.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.student.pk).exists())

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
        with patch.object(SurveysBuilder, 'delete', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                services.delete_survey(self.survey.pk, self.staff, self.survey.revision, allow_published=True)
        self.assertTrue(SurveyAssignment.objects.filter(pk=self.assignment.pk).exists())
        self.assertTrue(SurveyResponse.objects.filter(pk=self.response.pk).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())
