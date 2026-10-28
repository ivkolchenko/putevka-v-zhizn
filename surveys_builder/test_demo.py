from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from .forms import ResponseForm
from .management.commands.seed_demo_survey import DEMO_TITLE
from .models import QuestionsBuilder, SurveyAssignment, SurveyResponse, SurveysBuilder


class DemoSurveyTests(TestCase):
    def test_demo_covers_all_types_and_can_be_filled_without_assignments(self):
        call_command('seed_demo_survey', stdout=StringIO())
        survey = SurveysBuilder.objects.get(title=DEMO_TITLE)
        questions = list(survey.questions.prefetch_related('options'))
        self.assertEqual([q.answer_type for q in questions], list(QuestionsBuilder.AnswerType.values))
        self.assertEqual([q.position for q in questions], list(range(1, 8)))
        self.assertEqual(survey.status, SurveysBuilder.Status.DRAFT)
        self.assertFalse(survey.audience_configured)
        self.assertFalse(SurveyAssignment.objects.exists())
        self.assertFalse(SurveyResponse.objects.exists())
        self.assertTrue(any(q.required for q in questions))
        self.assertTrue(any(not q.required for q in questions))
        values = {}
        for question in questions:
            value = {'short_text': 'Анна Иванова', 'long_text': 'Хотелось бы больше практики.',
                     'number': '7.5', 'date': '2026-11-01', 'scale': '5'}.get(question.answer_type)
            if question.answer_type == 'single_choice':
                value = str(question.options.first().pk)
            elif question.answer_type == 'multiple_choice':
                value = [str(o.pk) for o in question.options.all()[:2]]
            values[f'q_{question.pk}'] = value
        form = ResponseForm(values, questions=questions)
        self.assertTrue(form.is_valid(), form.errors)

    def test_repeated_seed_preserves_existing_demo_and_edits(self):
        call_command('seed_demo_survey', stdout=StringIO())
        survey = SurveysBuilder.objects.get(title=DEMO_TITLE)
        question = survey.questions.first()
        question.title = 'Изменённый пользователем вопрос'
        question.save()
        call_command('seed_demo_survey', stdout=StringIO())
        self.assertEqual(SurveysBuilder.objects.filter(title=DEMO_TITLE).count(), 1)
        question.refresh_from_db()
        self.assertEqual(question.title, 'Изменённый пользователем вопрос')
        self.assertEqual(survey.questions.count(), 7)

    def test_demo_preview_renders_all_questions_for_staff(self):
        staff = get_user_model().objects.create_user(username='demo-staff', is_staff=True)
        call_command('seed_demo_survey', author_id=staff.pk, stdout=StringIO())
        survey = SurveysBuilder.objects.get(title=DEMO_TITLE)
        self.assertEqual(survey.author, staff)
        self.client.force_login(staff)
        response = self.client.get(f'/staff/surveys_builder/{survey.pk}/preview/')
        self.assertEqual(response.status_code, 200)
        for question in survey.questions.all():
            self.assertContains(response, question.title)
        self.assertNotContains(response, 'Опубликовать опросник')
