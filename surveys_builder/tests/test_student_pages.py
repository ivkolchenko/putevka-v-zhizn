from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from ..models import (
    QuestionBlock,
    QuestionOptions,
    QuestionsBuilder,
    SurveyAnswer,
    SurveyAssignment,
    SurveyResponse,
    SurveysBuilder,
)


class StudentSurveyPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.student = get_user_model().objects.create_user(username='survey-page-student')
        cls.survey = SurveysBuilder.objects.create(
            title='Опрос о занятиях',
            description='Расскажите о занятиях.',
            status=SurveysBuilder.Status.PUBLISHED,
            deadline=timezone.now() + timedelta(days=1),
        )
        cls.block = QuestionBlock.objects.create(
            survey=cls.survey, title='Первый блок', description='Подсказка для блока'
        )
        cls.questions = {}
        for position, kind in enumerate(QuestionsBuilder.AnswerType.values, 1):
            cls.questions[kind] = QuestionsBuilder.objects.create(
                survey=cls.survey,
                block=cls.block,
                title=f'Вопрос {kind}',
                description=f'Подсказка {kind}',
                answer_type=kind,
                required=True,
                position=position,
            )
        for kind in (QuestionsBuilder.AnswerType.SINGLE_CHOICE, QuestionsBuilder.AnswerType.MULTIPLE_CHOICE):
            question = cls.questions[kind]
            QuestionOptions.objects.create(question=question, text='Первый вариант', position=1)
            QuestionOptions.objects.create(question=question, text='Второй вариант', position=2)
        cls.assignment = SurveyAssignment.objects.create(survey=cls.survey, user=cls.student, visible_block_ids=[cls.block.pk])

    def setUp(self):
        self.client.force_login(self.student)
        self.url = reverse('student_surveys:answer', args=[self.assignment.pk])

    def answers(self):
        return {
            f'q_{self.questions["short_text"].pk}': 'Коротко',
            f'q_{self.questions["long_text"].pk}': 'Развёрнутый ответ',
            f'q_{self.questions["single_choice"].pk}': str(self.questions['single_choice'].options.first().pk),
            f'q_{self.questions["multiple_choice"].pk}': [
                str(option.pk) for option in self.questions['multiple_choice'].options.all()
            ],
            f'q_{self.questions["number"].pk}': '12.5',
            f'q_{self.questions["date"].pk}': '2026-10-28',
            f'q_{self.questions["scale"].pk}': '4',
        }

    def test_pages_use_portal_and_show_question_blocks_and_hints(self):
        listing = self.client.get(reverse('student_surveys:list'))
        self.assertTemplateUsed(listing, 'core/base.html')
        self.assertContains(listing, 'Опрос о занятиях')
        self.assertContains(listing, 'Ожидает ответа')

        page = self.client.get(self.url)
        self.assertTemplateUsed(page, 'core/base.html')
        self.assertContains(page, 'Первый блок')
        self.assertContains(page, 'Подсказка для блока')
        self.assertContains(page, 'form-control')
        self.assertContains(page, 'name="response_revision" value="0"')
        for kind in QuestionsBuilder.AnswerType.values:
            self.assertContains(page, f'Вопрос {kind}')
            self.assertContains(page, f'Подсказка {kind}')

    def test_draft_resumes_and_all_answer_types_submit(self):
        draft = self.client.post(self.url, {'action': 'save', 'response_revision': '0'})
        self.assertRedirects(draft, self.url)
        saved = SurveyResponse.objects.get(assignment=self.assignment)
        self.assertEqual(saved.status, SurveyResponse.Status.DRAFT)
        self.assertContains(self.client.get(reverse('student_surveys:list')), 'Есть черновик')

        invalid = self.client.post(
            self.url, {'action': 'submit', 'response_revision': str(saved.revision)}
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertTrue(invalid.context['form'].errors)
        saved.refresh_from_db()
        self.assertEqual(saved.status, SurveyResponse.Status.DRAFT)

        draft = self.client.post(
            self.url,
            {'action': 'save', 'response_revision': str(saved.revision), **self.answers()},
        )
        self.assertRedirects(draft, self.url)
        saved.refresh_from_db()
        resumed = self.client.get(self.url)
        self.assertContains(resumed, 'Коротко')
        self.assertContains(resumed, f'value="{saved.revision}"')

        final = self.client.post(
            self.url,
            {'action': 'submit', 'response_revision': str(saved.revision), **self.answers()},
        )
        self.assertRedirects(final, self.url)
        saved.refresh_from_db()
        self.assertEqual(saved.status, SurveyResponse.Status.SUBMITTED)
        self.assertEqual(saved.answers.count(), 7)
        self.assertEqual(
            SurveyAnswer.objects.get(response=saved, question=self.questions['number']).value,
            '12.5',
        )
        page = self.client.get(self.url)
        self.assertContains(page, 'Ваш ответ отправлен')
        self.assertContains(page, 'Первый вариант')
        self.assertNotContains(page, 'name="action"')
        self.assertContains(self.client.get(reverse('student_surveys:list')), 'Ответ отправлен')

    def test_stale_revision_and_repeat_submission_do_not_overwrite(self):
        first = self.client.post(
            self.url,
            {'action': 'save', 'response_revision': '0', **self.answers()},
        )
        self.assertRedirects(first, self.url)
        response = SurveyResponse.objects.get(assignment=self.assignment)
        stale = self.client.post(
            self.url,
            {'action': 'submit', 'response_revision': '0', **self.answers()},
        )
        self.assertEqual(stale.status_code, 400)
        self.assertContains(stale, 'Ответ изменён в другой вкладке', status_code=400)
        response.refresh_from_db()
        self.assertEqual(response.status, SurveyResponse.Status.DRAFT)

        final = self.client.post(
            self.url,
            {'action': 'submit', 'response_revision': str(response.revision), **self.answers()},
        )
        self.assertRedirects(final, self.url)
        response.refresh_from_db()
        repeated = self.client.post(
            self.url,
            {'action': 'submit', 'response_revision': str(response.revision), **self.answers()},
        )
        self.assertContains(repeated, 'Ответ уже отправлен', status_code=400)
        self.assertEqual(SurveyResponse.objects.count(), 1)
        self.assertEqual(SurveyAnswer.objects.filter(response=response).count(), 7)

    def test_expired_closed_and_archived_surveys_are_read_only(self):
        states = (
            (SurveysBuilder.Status.PUBLISHED, timezone.now() - timedelta(minutes=1)),
            (SurveysBuilder.Status.CLOSED, None),
            (SurveysBuilder.Status.ARCHIVED, None),
        )
        for status, deadline in states:
            with self.subTest(status=status, deadline=deadline):
                SurveysBuilder.objects.filter(pk=self.survey.pk).update(status=status, deadline=deadline)
                page = self.client.get(self.url)
                self.assertContains(page, 'Приём ответов завершён')
                self.assertNotContains(page, 'name="action"')
                posted = self.client.post(
                    self.url,
                    {'action': 'submit', 'response_revision': '0', **self.answers()},
                )
                self.assertContains(posted, 'Приём ответов завершён или опрос недоступен', status_code=400)
                self.assertFalse(SurveyResponse.objects.exists())

    def test_direct_link_does_not_open_draft_survey(self):
        SurveysBuilder.objects.filter(pk=self.survey.pk).update(status=SurveysBuilder.Status.DRAFT)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.post(self.url, {'action': 'save', 'response_revision': '0'}).status_code,
            404,
        )

    def test_legacy_student_path_is_not_routed(self):
        with self.assertRaises(Resolver404):
            resolve('/study/surveys/1/')
