import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import RegistrationPersonalData
from scholar_form.models import UserInfo
from .. import services
from ..models import QuestionsBuilder, SurveyAssignment
from ..selectors import assignment_questions


class QuestionVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.staff = users.objects.create_user(username='visibility-staff', is_staff=True)
        cls.senior = users.objects.create_user(username='visibility-senior')
        cls.junior = users.objects.create_user(username='visibility-junior')
        for user, grade in ((cls.senior, 11), (cls.junior, 10)):
            RegistrationPersonalData.objects.create(user=user, current_step='finish', email=user.username + '@example.test')
            UserInfo.objects.create(user=user, next_year_class_digit=grade)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Видимость вопросов'})
        self.block = services.save_block(self.survey.pk, self.staff, {'title': 'Общий блок'}, self.version())

    def version(self):
        self.survey.refresh_from_db()
        return self.survey.revision

    def question(self, title, filters=None, **extra):
        return services.save_question(self.survey.pk, self.staff, {
            'title': title, 'block': self.block.pk, 'answer_type': 'short_text',
            'required': True, 'visibility_filters': filters or {}, **extra,
        }, self.version())

    def publish(self):
        services.save_audience(self.survey.pk, self.staff, {'mode': 'filters'}, self.version())
        self.version()
        ids = [self.senior.pk, self.junior.pk]
        services.publish_survey(self.survey.pk, self.staff, services.publish_token(self.survey, self.staff, ids))
        return self.survey.assignments.get(user=self.senior)

    def test_web_create_edit_clear_and_validation(self):
        url = reverse('surveys_builder:block_content', args=[self.survey.pk, self.block.pk])
        self.assertContains(self.client.get(url), 'Дополнительно: кому виден вопрос')
        data = {'title': 'Для старших', 'answer_type': 'short_text', 'grades': ['11'], 'revision': self.version()}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        question = self.survey.questions.get()
        self.assertEqual(question.visibility_filters['grades'], ['11'])
        edit = reverse('surveys_builder:question_edit', args=[self.survey.pk, question.pk])
        response = self.client.get(edit)
        self.assertTrue(response.context['show_visibility'])
        data.update(block=self.block.pk, revision=self.version(), grades=['9'])
        response = self.client.post(edit, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('grades', response.context['visibility_form'].errors)
        question.refresh_from_db()
        self.assertEqual(question.visibility_filters['grades'], ['11'])
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('grades', response.context['visibility_form'].errors)
        self.assertEqual(self.survey.questions.count(), 1)
        data.update(grades=[], revision=self.version())
        self.assertEqual(self.client.post(edit, data).status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.visibility_filters['grades'], [])

    def test_api_preserves_filters_on_patch_and_rejects_invalid_filters(self):
        url = reverse('surveys_builder:surveys_api:questions', args=[self.survey.pk])
        data = {'title': 'API', 'block_id': self.block.pk, 'revision': self.version(),
                'visibility_filters': {'grades': ['11']}}
        response = self.client.post(url, json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        question = response.json()['question']
        self.assertEqual(question['visibility_filters']['grades'], ['11'])
        url = reverse('surveys_builder:surveys_api:question_detail', args=[self.survey.pk, question['id']])
        response = self.client.patch(url, json.dumps({'title': 'Изменено', 'revision': self.version()}), content_type='application/json')
        self.assertEqual(response.json()['question']['visibility_filters']['grades'], ['11'])
        for filters in ({'grades': ['9']}, {'unknown': []}, [], None):
            data = {'revision': self.version(), 'visibility_filters': filters}
            self.assertEqual(self.client.patch(url, json.dumps(data), content_type='application/json').status_code, 400)

    def test_student_html_api_and_writes_exclude_hidden_questions(self):
        common = self.question('Общий вопрос')
        hidden = self.question('Только десятый класс', {'grades': ['10']})
        assignment = self.publish()
        self.assertEqual(assignment.visible_question_ids, [common.pk])
        self.client.force_login(self.senior)
        url = reverse('student_surveys:answer', args=[assignment.pk])
        response = self.client.get(url)
        self.assertContains(response, common.title)
        self.assertNotContains(response, hidden.title)
        api = reverse('student_surveys:student_surveys_api:detail', args=[assignment.pk])
        survey = self.client.get(api).json()['survey']
        self.assertEqual([q['id'] for q in survey['questions']], [common.pk])
        self.assertNotIn('visibility_filters', survey['questions'][0])
        self.assertEqual([item['id'] for item in survey['blocks'][0]['items']], [common.pk])
        with self.assertRaises(services.InvalidForm):
            services.save_response(assignment.pk, self.senior, {f'q_{hidden.pk}': 'Подмена'}, final=False, revision=0)
        api = reverse('student_surveys:student_surveys_api:response', args=[assignment.pk])
        response = self.client.put(api, json.dumps({'revision': 0, 'answers': {str(hidden.pk): 'Подмена'}}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(hasattr(assignment, 'response'))
        response = services.save_response(assignment.pk, self.senior, {f'q_{common.pk}': 'Ответ'}, final=True, revision=0)
        self.assertEqual(list(response.answers.values_list('question_id', flat=True)), [common.pk])
        with self.assertRaises(ValidationError):
            services.save_response(assignment.pk, self.senior, {}, final=True, revision=response.revision)
        self.client.force_login(self.junior)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_snapshot_survives_profile_change_and_obeys_block_visibility(self):
        question = self.question('Для старших', {'grades': ['11']})
        restricted = services.save_block(self.survey.pk, self.staff,
            {'title': 'Только младшим', 'visibility_filters': {'grades': ['10']}}, self.version())
        other = self.question('В скрытом блоке', block=restricted.pk)
        assignment = self.publish()
        UserInfo.objects.filter(user=self.senior).update(next_year_class_digit=10)
        self.assertEqual(list(assignment_questions(assignment).values_list('pk', flat=True)), [question.pk])
        junior = self.survey.assignments.get(user=self.junior)
        self.assertEqual(junior.visible_question_ids, [other.pk])

    def test_hidden_required_questions_of_all_types_do_not_block_submission(self):
        visible = self.question('Обязательный видимый')
        for kind in QuestionsBuilder.AnswerType.values:
            self.question(kind, {'grades': ['10']}, answer_type=kind, options_text='Да\nНет')
        assignment = self.publish()
        with self.assertRaises(services.InvalidForm):
            services.save_response(assignment.pk, self.senior, {}, final=True, revision=0)
        response = services.save_response(assignment.pk, self.senior,
            {f'q_{visible.pk}': 'Ответ'}, final=True, revision=0)
        self.assertEqual(response.answers.count(), 1)

    def test_page_drafts_and_final_validation_only_use_visible_questions(self):
        first = self.question('Первая страница')
        self.question('Скрытый обязательный', {'grades': ['10']})
        second_block = services.save_block(self.survey.pk, self.staff, {'title': 'Вторая страница'}, self.version())
        second = self.question('Вторая страница', block=second_block.pk)
        assignment = self.publish()
        self.client.force_login(self.senior)
        first_url = reverse('student_surveys:answer', args=[assignment.pk])
        second_url = first_url + '?page=2'
        self.assertEqual(self.client.post(second_url, {
            'action': 'submit', 'response_revision': 0, f'q_{second.pk}': 'Второй',
        }).status_code, 400)
        self.assertEqual(self.client.post(first_url, {
            'action': 'next', 'response_revision': 0, f'q_{first.pk}': 'Первый',
        }).status_code, 302)
        response = assignment.response
        self.assertEqual(self.client.post(second_url, {
            'action': 'submit', 'response_revision': response.revision, f'q_{second.pk}': 'Второй',
        }).status_code, 302)
        self.assertEqual(dict(response.answers.values_list('question_id', 'value')),
                         {first.pk: 'Первый', second.pk: 'Второй'})
        self.assertNotContains(self.client.get(first_url), 'Скрытый обязательный')

    def test_copy_duplicate_and_legacy_assignment(self):
        question = self.question('Копируемый', {'grades': ['11']})
        services.question_action(self.survey.pk, question.pk, self.staff, 'duplicate', self.version())
        duplicate = self.survey.questions.exclude(pk=question.pk).get()
        self.assertEqual(duplicate.visibility_filters, question.visibility_filters)
        copy = services.copy_survey(self.survey.pk, self.staff)
        self.assertTrue(all(q.visibility_filters == question.visibility_filters for q in copy.questions.all()))
        assignment = SurveyAssignment.objects.create(survey=self.survey, user=self.senior, visible_block_ids=[self.block.pk])
        self.assertEqual(assignment_questions(assignment).count(), 2)
        assignment.visible_question_ids = []
        assignment.save()
        self.assertFalse(assignment_questions(assignment).exists())

    def test_publication_checks_filters_and_changed_visibility(self):
        question = self.question('Для старших', {'grades': ['11']})
        services.save_audience(self.survey.pk, self.staff, {'mode': 'filters'}, self.version())
        self.version()
        token = services.publish_token(self.survey, self.staff, [self.senior.pk, self.junior.pk])
        UserInfo.objects.filter(user=self.senior).update(next_year_class_digit=10)
        with self.assertRaises(ValidationError):
            services.publish_survey(self.survey.pk, self.staff, token)
        self.assertFalse(self.survey.assignments.exists())
        question.visibility_filters = {'grades': ['9']}
        question.save()
        self.assertTrue(any('фильтры видимости вопроса' in error for error in services.publication_errors(self.survey)))
