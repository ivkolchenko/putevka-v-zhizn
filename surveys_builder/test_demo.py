from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import ResponseForm
from .management.commands.seed_demo_survey import DEMO_BLOCKS_TITLE, DEMO_IMAGES_TITLE, DEMO_TITLE
from .models import QuestionsBuilder, SurveyAssignment, SurveyResponse, SurveysBuilder
from .selectors import block_items


class DemoSurveyTests(TestCase):
    def test_illustrated_demo_has_all_types_ordered_mixed_content_and_private_images(self):
        staff = get_user_model().objects.create_user(username='illustrated-demo-staff', is_staff=True)
        with TemporaryDirectory() as media, override_settings(SURVEY_PRIVATE_MEDIA_ROOT=media):
            call_command('seed_demo_survey', with_images=True, author_id=staff.pk, stdout=StringIO())
            survey = SurveysBuilder.objects.get(title=DEMO_IMAGES_TITLE)
            blocks = list(survey.blocks.all())
            self.assertEqual([b.questions.count() for b in blocks], [1, 3, 3, 0])
            self.assertEqual(list(survey.questions.values_list('answer_type', flat=True)), list(QuestionsBuilder.AnswerType.values))
            self.assertEqual([item['type'] for item in block_items(blocks[1])], ['question', 'section', 'question', 'question'])
            self.assertEqual([item['type'] for item in block_items(blocks[3])], ['section', 'section'])
            for block in blocks:
                items = block_items(block)
                self.assertEqual([item['position'] for item in items], list(range(1, len(items) + 1)))
            self.assertTrue(survey.is_draft)
            self.assertFalse(survey.audience_configured)
            self.assertFalse(survey.assignments.exists())
            self.assertIsNone(survey.deadline)
            self.client.force_login(staff)
            response = self.client.get(reverse('surveys_builder:preview', args=[survey.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'name="q_')
            images = [section for block in blocks for section in block.text_sections.all() if section.image]
            self.assertEqual(len(images), 2)
            for section in images:
                self.assertTrue(section.image.storage.exists(section.image.name))
                url = reverse('surveys_builder:section_image', args=[section.pk])
                self.assertContains(response, url)
                image = self.client.get(url)
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image['Content-Type'], 'image/png')
                self.assertGreater(sum(len(chunk) for chunk in image.streaming_content), 1000)
                image.close()

    def test_illustrated_demo_repeat_preserves_edits_and_does_not_create_image_copies(self):
        with TemporaryDirectory() as media, override_settings(SURVEY_PRIVATE_MEDIA_ROOT=media):
            call_command('seed_demo_survey', with_images=True, stdout=StringIO())
            survey = SurveysBuilder.objects.get(title=DEMO_IMAGES_TITLE)
            block = survey.blocks.first()
            section = block.text_sections.first()
            section.text = 'Изменено вручную'
            section.save(update_fields=['text'])
            files = sorted(str(path) for path in Path(media).rglob('*.png'))
            call_command('seed_demo_survey', with_images=True, stdout=StringIO())
            section.refresh_from_db()
            self.assertEqual(section.text, 'Изменено вручную')
            self.assertEqual(SurveysBuilder.objects.filter(title=DEMO_IMAGES_TITLE).count(), 1)
            self.assertEqual(sorted(str(path) for path in Path(media).rglob('*.png')), files)

    def test_block_demo_is_separate_complete_and_repeatable(self):
        call_command('seed_demo_survey', stdout=StringIO())
        call_command('seed_demo_survey', with_blocks=True, stdout=StringIO())
        survey = SurveysBuilder.objects.get(title=DEMO_BLOCKS_TITLE)
        blocks = list(survey.blocks.all())
        self.assertEqual([block.title for block in blocks], ['О вас', 'Учёба и интересы', 'Планы и оценка'])
        self.assertEqual([block.questions.count() for block in blocks], [1, 3, 3])
        self.assertEqual(list(survey.questions.values_list('answer_type', flat=True)), list(QuestionsBuilder.AnswerType.values))
        self.assertFalse(survey.questions.filter(block=None).exists())
        self.assertTrue(survey.is_draft)
        self.assertFalse(survey.assignments.exists())
        self.assertEqual(SurveysBuilder.objects.get(title=DEMO_TITLE).blocks.count(), 1)
        blocks[0].description = 'Сохранить пользовательское изменение'
        blocks[0].save()
        call_command('seed_demo_survey', with_blocks=True, stdout=StringIO())
        blocks[0].refresh_from_db()
        self.assertEqual(blocks[0].description, 'Сохранить пользовательское изменение')
        self.assertEqual(SurveysBuilder.objects.filter(title=DEMO_BLOCKS_TITLE).count(), 1)

    def test_block_demo_renders_headings_and_all_question_fields(self):
        staff = get_user_model().objects.create_user(username='block-demo-staff', is_staff=True)
        call_command('seed_demo_survey', with_blocks=True, author_id=staff.pk, stdout=StringIO())
        survey = SurveysBuilder.objects.get(title=DEMO_BLOCKS_TITLE)
        self.client.force_login(staff)
        response = self.client.get(f'/staff/surveys_builder/{survey.pk}/preview/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['sections']), 3)
        for block in survey.blocks.all():
            self.assertContains(response, block.title)
            self.assertContains(response, block.description)
        for question in survey.questions.all():
            self.assertContains(response, question.title)
            self.assertNotContains(response, f'name="q_{question.pk}"')

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
