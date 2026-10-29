from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import RegistrationPersonalData
from .. import services
from ..models import SurveysBuilder


class GuidedSurveyFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='guided-staff', is_staff=True)
        cls.student = User.objects.create_user(username='guided-student')
        RegistrationPersonalData.objects.create(user=cls.student, email='guided@example.test', current_step='finish')

    def setUp(self):
        self.client.force_login(self.staff)

    def test_create_two_blocks_then_continue_to_assignment(self):
        response = self.client.post(reverse('surveys_builder:constructor'), {'title': 'Линейный опрос'})
        survey = SurveysBuilder.objects.get(title='Линейный опрос')
        self.assertRedirects(response, reverse('surveys_builder:block_create', args=[survey.pk]))
        response = self.client.post(response.url, {'title': 'О себе', 'revision': survey.revision})
        first = survey.blocks.get()
        content_url = reverse('surveys_builder:block_content', args=[survey.pk, first.pk])
        self.assertRedirects(response, content_url)
        page = self.client.get(content_url)
        self.assertContains(page, 'Добавить следующий блок')
        self.assertContains(page, reverse('surveys_builder:block_create', args=[survey.pk]))
        self.assertFalse(page.context['structure_ready'])
        self.assertContains(page, 'aria-disabled="true"')
        survey.refresh_from_db()
        response = self.client.post(content_url, {'revision': survey.revision, 'title': 'Ваше имя', 'answer_type': 'short_text'})
        self.assertRedirects(response, content_url)
        page = self.client.get(content_url)
        self.assertTrue(page.context['structure_ready'])
        self.assertContains(page, 'Далее: получатели и срок')
        self.assertContains(page, reverse('surveys_builder:audience', args=[survey.pk]))
        survey.refresh_from_db()
        response = self.client.post(reverse('surveys_builder:block_create', args=[survey.pk]),
                                    {'title': 'Об учёбе', 'revision': survey.revision})
        second = survey.blocks.exclude(pk=first.pk).get()
        self.assertRedirects(response, reverse('surveys_builder:block_content', args=[survey.pk, second.pk]))
        self.assertEqual(self.client.get(content_url).context['next_block'], second)
        survey.refresh_from_db()
        services.save_text_section(survey.pk, second.pk, self.staff, {'text': 'Спасибо за ответы!'}, {}, survey.revision)
        page = self.client.get(reverse('surveys_builder:block_content', args=[survey.pk, second.pk]))
        self.assertTrue(page.context['structure_ready'])
        self.assertContains(page, 'Блок 2 из 2')
        survey.refresh_from_db()
        response = self.client.post(reverse('surveys_builder:audience', args=[survey.pk]),
            {'revision': survey.revision, 'mode': 'individual', 'included_users': [self.student.pk], 'action': 'save'})
        self.assertRedirects(response, reverse('surveys_builder:preview', args=[survey.pk]))
        self.assertEqual(survey.blocks.count(), 2)
        self.assertEqual(first.questions.get().title, 'Ваше имя')

    def test_imported_block_has_next_actions_in_target_survey(self):
        template = services.create_block_template(self.staff, {'title': 'Готовый блок'})
        block = template.blocks.get()
        services.save_text_section(template.pk, block.pk, self.staff, {'text': 'Инструкция'}, {}, template.revision)
        survey = services.create_survey(self.staff, {'title': 'Целевой опрос'})
        response = self.client.post(reverse('surveys_builder:import_block_template', args=[survey.pk]),
            {'template_id': template.pk, 'revision': survey.revision})
        copied = survey.blocks.get()
        self.assertRedirects(response, reverse('surveys_builder:block_content', args=[survey.pk, copied.pk]))
        page = self.client.get(response.url)
        self.assertContains(page, reverse('surveys_builder:block_create', args=[survey.pk]))
        self.assertContains(page, reverse('surveys_builder:audience', args=[survey.pk]))
        self.assertContains(page, 'Добавить следующий блок')
        self.assertTrue(page.context['structure_ready'])

    def test_question_actions_and_move_return_to_correct_block(self):
        survey = services.create_survey(self.staff, {'title': 'Без лишних переходов'})
        first = services.save_block(survey.pk, self.staff, {'title': 'Первый'}, survey.revision)
        survey.refresh_from_db()
        second = services.save_block(survey.pk, self.staff, {'title': 'Второй'}, survey.revision)
        survey.refresh_from_db()
        question = services.save_question(survey.pk, self.staff,
            {'title': 'Вопрос', 'answer_type': 'short_text', 'block': first.pk}, survey.revision)
        survey.refresh_from_db()
        response = self.client.post(reverse('surveys_builder:question_action', args=[survey.pk, question.pk, 'duplicate']),
                                    {'revision': survey.revision})
        self.assertRedirects(response, reverse('surveys_builder:block_content', args=[survey.pk, first.pk]))
        survey.refresh_from_db()
        response = self.client.post(reverse('surveys_builder:question_edit', args=[survey.pk, question.pk]),
            {'title': question.title, 'answer_type': question.answer_type, 'block': second.pk, 'revision': survey.revision})
        self.assertRedirects(response, reverse('surveys_builder:block_content', args=[survey.pk, second.pk]))

    def test_template_editor_and_empty_library_have_clear_exits(self):
        survey = services.create_survey(self.staff, {'title': 'Пустая библиотека'})
        page = self.client.get(reverse('surveys_builder:import_block_template', args=[survey.pk]))
        self.assertContains(page, 'Создать новый блок')
        self.assertNotContains(page, 'name="template_id"')
        template = services.create_block_template(self.staff, {'title': 'Черновик'})
        page = self.client.get(reverse('surveys_builder:block_content', args=[template.pk, template.blocks.get().pk]))
        self.assertContains(page, 'Готово: вернуться в библиотеку')
        self.assertNotContains(page, 'Добавить следующий блок')

    def test_create_block_and_template_keeps_user_in_survey_and_preserves_filters(self):
        survey = services.create_survey(self.staff, {'title': 'Опрос с шаблоном'})
        url = reverse('surveys_builder:block_create', args=[survey.pk])
        self.assertContains(self.client.get(url), 'Создать и добавить в шаблоны')
        response = self.client.post(url, {'revision': survey.revision, 'title': 'Повторяемый блок',
            'description': 'Инструкция', 'grades': ['11'], 'action': 'save_template'})
        block = survey.blocks.get()
        self.assertRedirects(response, reverse('surveys_builder:block_content', args=[survey.pk, block.pk]))
        template = SurveysBuilder.objects.get(is_block_template=True)
        copied = template.blocks.get()
        self.assertNotEqual(copied.pk, block.pk)
        self.assertEqual(copied.title, block.title)
        self.assertEqual(copied.description, block.description)
        self.assertEqual(copied.visibility_filters, block.visibility_filters)
        self.assertEqual(copied.visibility_filters['grades'], ['11'])
        page = self.client.get(reverse('surveys_builder:block_content', args=[template.pk, copied.pk]))
        self.assertNotContains(page, 'Добавить блок в шаблоны')

    def test_save_content_to_template_stays_in_source_and_copies_questions_and_text(self):
        survey = services.create_survey(self.staff, {'title': 'Сохранить готовый блок'})
        block = services.save_block(survey.pk, self.staff, {'title': 'Первый'}, survey.revision)
        survey.refresh_from_db()
        question = services.save_question(survey.pk, self.staff,
            {'title': 'Выберите ответ', 'answer_type': 'single_choice', 'options_text': 'Да\nНет', 'block': block.pk}, survey.revision)
        survey.refresh_from_db()
        section = services.save_text_section(survey.pk, block.pk, self.staff, {'text': 'Текст блока'}, {}, survey.revision)
        survey.refresh_from_db()
        content_url = reverse('surveys_builder:block_content', args=[survey.pk, block.pk])
        self.assertContains(self.client.get(content_url), 'Добавить блок в шаблоны')
        endpoint = reverse('surveys_builder:save_block_template', args=[survey.pk, block.pk])
        self.assertEqual(self.client.post(endpoint, {'revision': 0}).status_code, 400)
        self.assertFalse(SurveysBuilder.objects.filter(is_block_template=True).exists())
        response = self.client.post(endpoint, {'revision': survey.revision})
        self.assertRedirects(response, content_url)
        template = SurveysBuilder.objects.get(is_block_template=True)
        copied = template.blocks.get()
        copied_question = copied.questions.get()
        self.assertNotEqual(copied_question.pk, question.pk)
        self.assertEqual(list(copied_question.options.values_list('text', flat=True)), ['Да', 'Нет'])
        self.assertEqual(copied.text_sections.get().text, section.text)
        question.title = 'Изменён исходник'
        question.save(update_fields=['title'])
        copied_question.refresh_from_db()
        self.assertEqual(copied_question.title, 'Выберите ответ')
