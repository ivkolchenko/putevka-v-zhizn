from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import QuestionBlock, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder


class DeletedRecipientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.staff = user_model.objects.create_user(username='anonymize-staff', is_staff=True)
        cls.student = user_model.objects.create_user(username='anonymize-student')
        cls.other = user_model.objects.create_user(username='anonymize-other')
        cls.survey = SurveysBuilder.objects.create(title='Исторический опрос', status=SurveysBuilder.Status.PUBLISHED)
        block = QuestionBlock.objects.create(survey=cls.survey, title='Основной блок')
        cls.question = QuestionsBuilder.objects.create(survey=cls.survey, block=block, title='Вопрос')
        cls.assignment = SurveyAssignment.objects.create(
            survey=cls.survey,
            user=cls.student,
            visible_block_ids=[block.pk],
            recipient_snapshot={
                'name': 'Анна Иванова', 'username': cls.student.username,
                'grade': 11, 'profile': 'it', 'status': 'SCHOLAR',
            },
        )
        cls.response = SurveyResponse.objects.create(
            assignment=cls.assignment, status=SurveyResponse.Status.SUBMITTED,
            submitted_at=timezone.now(),
        )
        cls.answer = SurveyAnswer.objects.create(response=cls.response, question=cls.question, value='Да')

    def test_direct_user_deletion_preserves_response_and_anonymizes_all_staff_outputs(self):
        student_pk = self.student.pk
        self.student.delete()
        self.assertFalse(get_user_model().objects.filter(pk=student_pk).exists())
        assignment = SurveyAssignment.objects.get(pk=self.assignment.pk)
        self.assertIsNone(assignment.user_id)
        self.assertEqual(assignment.recipient_snapshot, {})
        self.assertEqual(assignment.recipient_display, {'name': 'Удалённый участник', 'username': ''})
        self.assertTrue(SurveyResponse.objects.filter(pk=self.response.pk, assignment=assignment).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk, value='Да').exists())
        self.client.force_login(self.staff)
        pages = [
            reverse('surveys_builder:results', args=[self.survey.pk]),
            reverse('surveys_builder:response_detail', args=[self.survey.pk, self.response.pk]),
        ]
        for url in pages:
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)
            self.assertContains(result, 'Удалённый участник')
            self.assertNotContains(result, 'Анна Иванова')
            self.assertNotContains(result, 'anonymize-student')
        csv_response = self.client.get(reverse('surveys_builder:results_csv', args=[self.survey.pk]))
        csv_text = b''.join(csv_response.streaming_content).decode('utf-8-sig')
        self.assertIn('Удалённый участник', csv_text)
        self.assertNotIn('Анна Иванова', csv_text)
        self.assertNotIn('anonymize-student', csv_text)
        results = self.client.get(reverse('surveys_builder:surveys_api:results', args=[self.survey.pk])).json()
        item = results['recipients']['results'][0]
        self.assertIsNone(item['user_id'])
        self.assertEqual(item['recipient'], assignment.recipient_display)
        detail = self.client.get(reverse(
            'surveys_builder:surveys_api:response_detail', args=[self.survey.pk, self.response.pk]
        )).json()
        self.assertEqual(detail['recipient'], assignment.recipient_display)

    def test_draft_survives_queryset_deletion_and_other_student_cannot_open_it(self):
        SurveyResponse.objects.filter(pk=self.response.pk).update(status=SurveyResponse.Status.DRAFT)
        get_user_model().objects.filter(pk=self.student.pk).delete()
        assignment = SurveyAssignment.objects.get(pk=self.assignment.pk)
        self.assertIsNone(assignment.user_id)
        self.assertEqual(assignment.recipient_snapshot, {})
        self.assertTrue(SurveyResponse.objects.filter(pk=self.response.pk, status=SurveyResponse.Status.DRAFT).exists())
        self.assertTrue(SurveyAnswer.objects.filter(pk=self.answer.pk).exists())
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('student_surveys:answer', args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse(
            'student_surveys:student_surveys_api:detail', args=[assignment.pk]
        )).status_code, 404)
