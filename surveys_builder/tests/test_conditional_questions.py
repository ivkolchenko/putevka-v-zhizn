import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import RegistrationPersonalData
from scholar_form.models import UserInfo
from .. import services
from ..forms import ResponseForm
from ..models import QuestionsBuilder, SurveyAssignment
from ..selectors import assignment_questions


class ConditionalQuestionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.staff = users.objects.create_user(username='condition-staff', is_staff=True)
        cls.student = users.objects.create_user(username='condition-student')
        cls.other = users.objects.create_user(username='condition-other')
        for user in (cls.student, cls.other):
            RegistrationPersonalData.objects.create(user=user, email=user.username + '@example.test', current_step='finish')
            UserInfo.objects.create(user=user, next_year_class_digit=11)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Условные вопросы'})
        self.block = services.save_block(self.survey.pk, self.staff, {'title': 'Блок'}, self.version())
        self.parent = self.question('Родитель', answer_type='single_choice', options_text='Да\nНет')
        self.yes, self.no = self.parent.options.all()

    def version(self):
        self.survey.refresh_from_db()
        return self.survey.revision

    def question(self, title, **extra):
        return services.save_question(self.survey.pk, self.staff, {
            'title': title, 'block': self.block.pk, 'answer_type': 'short_text', **extra,
        }, self.version())

    def child(self, **extra):
        return self.question('Подвопрос', **{'show_if_option': self.yes.pk, **extra})

    def publish(self):
        services.save_audience(self.survey.pk, self.staff,
            {'mode': 'individual', 'included_users': [self.student.pk]}, self.version())
        self.version()
        services.publish_survey(self.survey.pk, self.staff,
            services.publish_token(self.survey, self.staff, [self.student.pk]))
        return self.survey.assignments.get(user=self.student)

    def test_create_and_edit_condition_in_web_form(self):
        url = reverse('surveys_builder:block_content', args=[self.survey.pk, self.block.pk])
        self.assertContains(self.client.get(url), 'Показывать при выборе варианта')
        self.assertContains(self.client.get(url), 'Добавить подвопрос')
        response = self.client.get(url, {'show_if_option': self.yes.pk})
        self.assertEqual(response.context['form']['show_if_option'].value(), self.yes.pk)
        self.assertEqual(self.client.get(url, {'show_if_option': 'wrong'}).status_code, 404)
        response = self.client.post(url, {'revision': self.version(), 'title': 'Детали',
            'answer_type': 'long_text', 'show_if_option': self.yes.pk})
        self.assertEqual(response.status_code, 302)
        child = self.survey.questions.get(title='Детали')
        self.assertEqual(child.show_if_option_id, self.yes.pk)
        edit = reverse('surveys_builder:question_edit', args=[self.survey.pk, child.pk])
        response = self.client.get(edit)
        self.assertEqual(response.context['form']['show_if_option'].value(), self.yes.pk)
        response = self.client.post(edit, {'revision': self.version(), 'title': child.title,
            'block': self.block.pk, 'answer_type': 'long_text', 'show_if_option': ''})
        self.assertEqual(response.status_code, 302)
        child.refresh_from_db()
        self.assertIsNone(child.show_if_option_id)

    def test_api_condition_and_partial_update(self):
        url = reverse('surveys_builder:surveys_api:questions', args=[self.survey.pk])
        response = self.client.post(url, json.dumps({'title': 'API', 'block_id': self.block.pk,
            'revision': self.version(), 'show_if_option_id': self.yes.pk}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        question = response.json()['question']
        self.assertEqual(question['show_if_option_id'], self.yes.pk)
        self.assertEqual(question['parent_question_id'], self.parent.pk)
        edit = reverse('surveys_builder:surveys_api:question_detail', args=[self.survey.pk, question['id']])
        response = self.client.patch(edit, json.dumps({'revision': self.version(), 'title': 'Изменено'}), content_type='application/json')
        self.assertEqual(response.json()['question']['show_if_option_id'], self.yes.pk)
        for value in (True, [], '1', -1):
            response = self.client.patch(edit, json.dumps({'revision': self.version(), 'show_if_option_id': value}), content_type='application/json')
            self.assertEqual(response.status_code, 400)
        response = self.client.patch(edit, json.dumps({'revision': self.version(), 'show_if_option_id': None}), content_type='application/json')
        self.assertIsNone(response.json()['question']['show_if_option_id'])

    def test_foreign_block_foreign_survey_self_and_cycles_are_rejected(self):
        child = self.child(answer_type='single_choice', options_text='Первый\nВторой')
        with self.assertRaises(services.InvalidForm):
            services.save_question(self.survey.pk, self.staff, {'title': 'Цикл', 'answer_type': 'single_choice',
                'options_text': 'Да\nНет', 'show_if_option': child.options.first().pk}, self.version(), self.parent.pk)
        with self.assertRaises(services.InvalidForm):
            services.save_question(self.survey.pk, self.staff, {'title': 'Сам', 'answer_type': 'single_choice',
                'options_text': 'Да\nНет', 'show_if_option': self.yes.pk}, self.version(), self.parent.pk)
        block = services.save_block(self.survey.pk, self.staff, {'title': 'Другой блок'}, self.version())
        with self.assertRaises(services.InvalidForm):
            self.child(block=block.pk)
        foreign = services.create_survey(self.staff, {'title': 'Чужой опрос'})
        foreign_block = services.save_block(foreign.pk, self.staff, {'title': 'Чужой'}, foreign.revision)
        foreign_question = services.save_question(foreign.pk, self.staff, {'title': 'Чужой', 'block': foreign_block.pk,
            'answer_type': 'single_choice', 'options_text': 'Да\nНет'}, foreign_block.survey.revision)
        with self.assertRaises(services.InvalidForm):
            self.child(show_if_option=foreign_question.options.first().pk)
        with self.assertRaises(services.InvalidForm):
            services.save_question(self.survey.pk, self.staff, {'title': self.parent.title, 'block': block.pk,
                'answer_type': 'single_choice', 'options_text': 'Да\nНет'}, self.version(), self.parent.pk)

    def test_required_and_invalid_values_are_only_checked_when_active_for_all_types(self):
        for kind in QuestionsBuilder.AnswerType.values:
            with self.subTest(kind=kind):
                child = self.child(answer_type=kind, required=True, options_text='Один\nДва')
                questions = [self.parent, child]
                form = ResponseForm({f'q_{self.parent.pk}': self.no.pk, f'q_{child.pk}': 'неверное'}, questions=questions)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertIsNone(form.answer_values()[child.pk])
                form = ResponseForm({f'q_{self.parent.pk}': self.yes.pk}, questions=questions)
                self.assertFalse(form.is_valid())
                self.assertIn(f'q_{child.pk}', form.errors)

    def test_multi_choice_deselection_clears_nested_answers_and_api_enforces_it(self):
        services.save_question(self.survey.pk, self.staff, {'title': 'Родитель', 'answer_type': 'multiple_choice',
            'options_text': 'Да\nНет'}, self.version(), self.parent.pk)
        child = self.child(answer_type='single_choice', options_text='Уточнить\nПропустить', required=True)
        nested = self.question('Вложенный', show_if_option=child.options.first().pk, required=True)
        assignment = self.publish()
        self.client.force_login(self.student)
        api = reverse('student_surveys:student_surveys_api:response', args=[assignment.pk])
        data = {'revision': 0, 'answers': {str(self.parent.pk): [self.yes.pk, self.no.pk],
                str(child.pk): child.options.first().pk, str(nested.pk): 'Сохранённый ответ'}}
        response = self.client.put(api, json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = {'revision': response.json()['revision'], 'answers': {
            str(self.parent.pk): [self.no.pk], str(child.pk): {'подмена': True}, str(nested.pk): 'Старое'}}
        response = self.client.put(api, json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['answers'][str(child.pk)])
        self.assertIsNone(response.json()['answers'][str(nested.pk)])
        stored = assignment.response
        self.assertIsNone(stored.answers.get(question=nested).value)
        revision = stored.revision
        response = self.client.put(api, json.dumps({'revision': revision, 'final': True,
            'answers': {str(self.parent.pk): [self.yes.pk]}}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        stored.refresh_from_db()
        self.assertEqual(stored.revision, revision)

    def test_html_clears_child_and_preserves_other_pages(self):
        child = self.child(required=True)
        other_block = services.save_block(self.survey.pk, self.staff, {'title': 'Вторая страница'}, self.version())
        other = self.question('Другой блок', block=other_block.pk)
        assignment = self.publish()
        services.save_response(assignment.pk, self.student, {f'q_{self.parent.pk}': self.yes.pk,
            f'q_{child.pk}': 'Ответ подвопроса', f'q_{other.pk}': 'Другая страница'}, final=False, revision=0)
        self.client.force_login(self.student)
        url = reverse('student_surveys:answer', args=[assignment.pk])
        response = self.client.get(url)
        self.assertContains(response, 'data-parent-option="' + str(self.yes.pk) + '"')
        self.assertContains(response, 'surveys_builder/js/conditions.js')
        stored = assignment.response
        response = self.client.post(url, {'action': 'save', 'response_revision': stored.revision,
            f'q_{self.parent.pk}': self.no.pk, f'q_{child.pk}': 'Старый ответ'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(stored.answers.get(question=child).value)
        self.assertEqual(stored.answers.get(question=other).value, 'Другая страница')
        stored.refresh_from_db()
        response = self.client.post(url + '?page=2', {'action': 'submit', 'response_revision': stored.revision,
            f'q_{other.pk}': 'Готово'})
        self.assertEqual(response.status_code, 302)
        self.assertNotContains(self.client.get(url), child.title)
        stored.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.save_response(assignment.pk, self.student, {}, final=False, revision=stored.revision)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_copy_duplicate_and_deletion_keep_conditions_consistent(self):
        child = self.child()
        services.question_action(self.survey.pk, child.pk, self.staff, 'duplicate', self.version())
        duplicate = self.survey.questions.exclude(pk__in=[self.parent.pk, child.pk]).get()
        self.assertEqual(duplicate.show_if_option_id, self.yes.pk)
        copy = services.copy_survey(self.survey.pk, self.staff)
        copied_parent = copy.questions.get(title=self.parent.title)
        for copied_child in copy.questions.exclude(pk=copied_parent.pk):
            self.assertEqual(copied_child.show_if_option.question_id, copied_parent.pk)
            self.assertNotEqual(copied_child.show_if_option_id, self.yes.pk)
        revision = self.version()
        with self.assertRaises(ValidationError):
            services.save_question(self.survey.pk, self.staff, {'title': self.parent.title, 'answer_type': 'single_choice',
                'options_text': 'Нет\nНе знаю'}, revision, self.parent.pk)
        self.assertEqual(self.version(), revision)
        self.assertTrue(self.parent.options.filter(pk=self.yes.pk).exists())
        with self.assertRaises(ValidationError):
            services.question_action(self.survey.pk, self.parent.pk, self.staff, 'delete', self.version())
        services.block_action(self.survey.pk, self.block.pk, self.staff, 'delete', self.version())
        self.assertFalse(self.survey.questions.exists())

    def test_child_cannot_bypass_parent_audience_filters(self):
        child = self.child()
        self.parent.visibility_filters = {'grades': ['10']}
        self.parent.save()
        assignment = self.publish()
        self.assertNotIn(child.pk, assignment.visible_question_ids)
        self.assertFalse(assignment_questions(assignment).exists())
        legacy = SurveyAssignment(survey=self.survey, user=self.student, visible_block_ids=[self.block.pk], visible_question_ids=[child.pk])
        self.assertFalse(assignment_questions(legacy).exists())
