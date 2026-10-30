import json
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import RegistrationPersonalData
from scholar_form.models import UserInfo
from .. import services
from ..models import BlockTextSection, QuestionsBuilder, SurveyResponse


class ConditionalBlockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.staff = users.objects.create_user(username='block-condition-staff', is_staff=True)
        cls.student = users.objects.create_user(username='block-condition-student')
        cls.other = users.objects.create_user(username='block-condition-other')
        RegistrationPersonalData.objects.create(user=cls.student, email='block-condition@example.test', current_step='finish')
        UserInfo.objects.create(user=cls.student, next_year_class_digit=11)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Условные блоки'})
        self.first = self.block('Начало')
        self.parent = self.question(self.first, 'Открыть подробности?', answer_type='single_choice', options_text='Да\nНет')
        self.yes, self.no = self.parent.options.all()

    def version(self):
        self.survey.refresh_from_db()
        return self.survey.revision

    def block(self, title, **extra):
        return services.save_block(self.survey.pk, self.staff, {'title': title, **extra}, self.version())

    def question(self, block, title, **extra):
        return services.save_question(self.survey.pk, self.staff, {
            'block': block.pk, 'title': title, 'answer_type': 'short_text', **extra,
        }, self.version())

    def publish(self):
        services.save_audience(self.survey.pk, self.staff,
            {'mode': 'individual', 'included_users': [self.student.pk]}, self.version())
        self.version()
        services.publish_survey(self.survey.pk, self.staff,
            services.publish_token(self.survey, self.staff, [self.student.pk]))
        self.assignment = self.survey.assignments.get(user=self.student)
        self.client.force_login(self.student)
        self.url = reverse('student_surveys:answer', args=[self.assignment.pk])
        return self.assignment

    def test_create_edit_block_condition_in_forms_and_api(self):
        url = reverse('surveys_builder:block_create', args=[self.survey.pk])
        response = self.client.get(url)
        self.assertContains(response, 'Показывать при выборе варианта')
        self.assertIn(self.yes, response.context['form'].fields['show_if_option'].queryset)
        response = self.client.post(url, {'title': 'Ветка', 'revision': self.version(), 'show_if_option': self.yes.pk})
        self.assertEqual(response.status_code, 302)
        branch = self.survey.blocks.get(title='Ветка')
        self.assertEqual(branch.show_if_option_id, self.yes.pk)
        edit = reverse('surveys_builder:block_edit', args=[self.survey.pk, branch.pk])
        self.assertEqual(self.client.get(edit).context['form']['show_if_option'].value(), self.yes.pk)
        api = reverse('surveys_builder:surveys_api:block_detail', args=[self.survey.pk, branch.pk])
        response = self.client.patch(api, json.dumps({'revision': self.version(), 'title': 'Новое имя'}), content_type='application/json')
        self.assertEqual(response.json()['block']['show_if_option_id'], self.yes.pk)
        response = self.client.patch(api, json.dumps({'revision': self.version(), 'show_if_option_id': None}), content_type='application/json')
        self.assertIsNone(response.json()['block']['show_if_option_id'])
        create = reverse('surveys_builder:surveys_api:blocks', args=[self.survey.pk])
        response = self.client.post(create, json.dumps({'title': 'API', 'revision': self.version(),
            'show_if_option_id': self.yes.pk}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['block']['parent_question_id'], self.parent.pk)

    def test_navigation_opens_selected_branch_and_hides_page_count(self):
        branch = self.block('Подробности', show_if_option=self.yes.pk)
        detail = self.question(branch, 'Расскажите подробнее', required=True)
        self.publish()
        initial = self.client.get(self.url)
        self.assertNotContains(initial, 'Страница 1 из')
        self.assertNotContains(initial, 'Сохранить и далее')
        self.assertContains(initial, '>Далее</button>')
        self.assertNotContains(initial, detail.title)
        self.assertEqual(self.client.get(self.url + '?page=2').status_code, 404)
        response = self.client.post(self.url, {'action': 'next', 'response_revision': 0, f'q_{self.parent.pk}': self.yes.pk})
        self.assertRedirects(response, self.url + '?page=2')
        self.assertContains(self.client.get(self.url + '?page=2'), detail.title)
        stored = self.assignment.response
        response = self.client.post(self.url + '?page=2', {'action': 'submit', 'response_revision': stored.revision})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'q_{detail.pk}', response.context['form'].errors)
        response = self.client.post(self.url + '?page=2', {'action': 'submit', 'response_revision': stored.revision,
            f'q_{detail.pk}': 'Готово'})
        self.assertEqual(response.status_code, 302)
        stored.refresh_from_db()
        self.assertEqual(stored.status, SurveyResponse.Status.SUBMITTED)

    def test_submit_can_open_new_branch_without_javascript(self):
        branch = self.block('Ветка', show_if_option=self.yes.pk)
        self.question(branch, 'Детали', required=True)
        self.publish()
        response = self.client.post(self.url, {'action': 'submit', 'response_revision': 0, f'q_{self.parent.pk}': self.yes.pk})
        self.assertRedirects(response, self.url + '?page=2')
        self.assertEqual(self.assignment.response.status, SurveyResponse.Status.DRAFT)

    def test_hidden_blocks_are_skipped_and_do_not_require_answers(self):
        hidden = self.block('Скрытый', show_if_option=self.yes.pk)
        self.question(hidden, 'Скрытый обязательный', required=True)
        last = self.block('Последний')
        visible = self.question(last, 'Обычный вопрос', required=True)
        self.publish()
        response = self.client.post(self.url, {'action': 'next', 'response_revision': 0, f'q_{self.parent.pk}': self.no.pk})
        self.assertRedirects(response, self.url + '?page=2')
        page = self.client.get(self.url + '?page=2')
        self.assertContains(page, visible.title)
        self.assertNotContains(page, hidden.title)
        self.assertEqual(self.client.get(self.url + '?page=3').status_code, 404)
        stored = self.assignment.response
        response = self.client.post(self.url + '?page=2', {'action': 'submit', 'response_revision': stored.revision,
            f'q_{visible.pk}': 'Ответ'})
        self.assertEqual(response.status_code, 302)

    def test_deselection_clears_nested_blocks_preserving_unrelated_answers(self):
        branch = self.block('Ветка', show_if_option=self.yes.pk)
        parent = self.question(branch, 'Продолжить?', answer_type='multiple_choice', options_text='Да\nНет')
        nested = self.block('Вложенная ветка', show_if_option=parent.options.first().pk)
        child = self.question(nested, 'Секрет', required=True)
        common = self.block('Общее')
        other = self.question(common, 'Сохраняемый ответ')
        assignment = self.publish()
        saved = services.save_response(assignment.pk, self.student, {f'q_{self.parent.pk}': self.yes.pk,
            f'q_{parent.pk}': [parent.options.first().pk], f'q_{child.pk}': 'Удалить', f'q_{other.pk}': 'Оставить'},
            final=False, revision=0)
        response = self.client.post(self.url, {'action': 'next', 'response_revision': saved.revision,
            f'q_{self.parent.pk}': self.no.pk})
        self.assertRedirects(response, self.url + '?page=2')
        self.assertIsNone(saved.answers.get(question=parent).value)
        self.assertIsNone(saved.answers.get(question=child).value)
        self.assertEqual(saved.answers.get(question=other).value, 'Оставить')
        self.assertContains(self.client.get(self.url + '?page=2'), other.title)
        saved.refresh_from_db()
        with self.assertRaises(services.RevisionConflict):
            services.save_response(assignment.pk, self.student, {}, final=False, revision=0)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_api_hides_blocks_and_clears_spoofed_hidden_values(self):
        branch = self.block('Ветка', show_if_option=self.yes.pk)
        child = self.question(branch, 'Число', answer_type='number', required=True)
        assignment = self.publish()
        detail = reverse('student_surveys:student_surveys_api:detail', args=[assignment.pk])
        self.assertEqual([b['id'] for b in self.client.get(detail).json()['survey']['blocks']], [self.first.pk])
        api = reverse('student_surveys:student_surveys_api:response', args=[assignment.pk])
        response = self.client.put(api, json.dumps({'revision': 0, 'answers': {str(self.parent.pk): self.yes.pk,
            str(child.pk): '42'}}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.get(detail).json()['survey']['blocks']), 2)
        response = self.client.put(api, json.dumps({'revision': response.json()['revision'], 'final': True,
            'answers': {str(self.parent.pk): self.no.pk, str(child.pk): {'подмена': True}}}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['answers'][str(child.pk)])
        self.assertEqual([b['id'] for b in self.client.get(detail).json()['survey']['blocks']], [self.first.pk])

    def test_dependencies_cannot_point_to_current_later_or_foreign_block(self):
        with self.assertRaises(services.InvalidForm):
            services.save_block(self.survey.pk, self.staff, {'title': 'Нельзя', 'show_if_option': self.yes.pk}, self.version(), self.first.pk)
        branch = self.block('Ветка', show_if_option=self.yes.pk)
        self.question(branch, 'Детали')
        with self.assertRaises(ValidationError):
            services.reorder_blocks(self.survey.pk, self.staff, [branch.pk, self.first.pk], self.version())
        self.first.refresh_from_db()
        self.assertEqual(self.first.position, 1)
        with self.assertRaises(services.InvalidForm):
            services.save_question(self.survey.pk, self.staff, {'block': branch.pk, 'title': 'Перенос',
                'answer_type': 'single_choice', 'options_text': 'Да\nНет'}, self.version(), self.parent.pk)
        foreign = services.create_survey(self.staff, {'title': 'Другой'})
        with self.assertRaises(services.InvalidForm):
            services.save_block(foreign.pk, self.staff, {'title': 'Чужой', 'show_if_option': self.yes.pk}, foreign.revision)

    def test_copy_remaps_conditions_and_deletion_is_protected(self):
        branch = self.block('Ветка', show_if_option=self.yes.pk)
        self.question(branch, 'Детали')
        copy = services.copy_survey(self.survey.pk, self.staff)
        copied_branch = copy.blocks.get(title=branch.title)
        self.assertNotEqual(copied_branch.show_if_option_id, self.yes.pk)
        self.assertEqual(copied_branch.show_if_option.question.survey_id, copy.pk)
        template = services.create_block_template(self.staff, {}, source_pk=self.survey.pk, block_pk=branch.pk, revision=self.version())
        self.assertIsNone(template.blocks.get().show_if_option_id)
        with self.assertRaises(ValidationError):
            services.save_question(self.survey.pk, self.staff, {'title': self.parent.title, 'answer_type': 'short_text'}, self.version(), self.parent.pk)
        with self.assertRaises(ValidationError):
            services.question_action(self.survey.pk, self.parent.pk, self.staff, 'delete', self.version())
        with self.assertRaises(ValidationError):
            services.block_action(self.survey.pk, self.first.pk, self.staff, 'delete', self.version())
        services.delete_survey(self.survey.pk, self.staff, self.version())
        self.assertFalse(QuestionsBuilder.objects.filter(survey_id=self.survey.pk).exists())

    def test_text_only_conditional_block_can_be_opened(self):
        branch = self.block('Инструкция', show_if_option=self.yes.pk)
        services.save_text_section(self.survey.pk, branch.pk, self.staff, {'text': 'Важный текст'}, {}, self.version())
        self.publish()
        response = self.client.post(self.url, {'action': 'next', 'response_revision': 0, f'q_{self.parent.pk}': self.yes.pk})
        self.assertRedirects(response, self.url + '?page=2')
        self.assertContains(self.client.get(self.url + '?page=2'), 'Важный текст')

    def test_hidden_block_image_is_not_available_by_direct_url(self):
        branch = self.block('Картинка', show_if_option=self.yes.pk)
        section = BlockTextSection.objects.create(block=branch, text='Описание', image='surveys/conditional.png')
        assignment = self.publish()
        url = reverse('surveys_builder:section_image', args=[section.pk])
        with patch('surveys_builder.storage.PrivateSurveyImageStorage.open', return_value=BytesIO(b'image')) as opened:
            self.assertEqual(self.client.get(url).status_code, 404)
            opened.assert_not_called()
            services.save_response(assignment.pk, self.student, {f'q_{self.parent.pk}': self.yes.pk}, final=False, revision=0)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'image')
            response.close()
