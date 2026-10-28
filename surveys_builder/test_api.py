"""Публичный JSON-контракт, контроль доступа и полный цикл назначения/ответа."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo

from . import services
from .models import SurveyAssignment, SurveyResponse, SurveysBuilder


class SurveyApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='api-staff', is_staff=True)
        cls.student = User.objects.create_user(username='api-student')
        cls.other = User.objects.create_user(username='api-other')
        cls.inactive = User.objects.create_user(username='api-inactive', is_active=False)
        UserInfo.objects.create(user=cls.student, first_name='Анна', last_name='Иванова',
            next_year_class_digit=11, internal_study_profile='it', status='SCHOLAR', email='anna@example.test')
        UserInfo.objects.create(user=cls.other, next_year_class_digit=10, status='ALUMNUS')
        cls.subject = Subject.objects.create(name='Математика API', slug='api-math')
        cls.school = School.objects.create(name='Школа API')
        cls.course = Course.objects.create(school=cls.school, subject=cls.subject, title='Курс API')
        CourseSelection.objects.create(user=cls.student, course=cls.course)

    def setUp(self):
        self.client.force_login(self.staff)

    def url(self, name, *args, student=False):
        namespace = 'student_surveys:student_surveys_api' if student else 'surveys_builder:surveys_api'
        return reverse(namespace + ':' + name, args=args)

    def send(self, method, name, data, *args, student=False):
        return getattr(self.client, method)(self.url(name, *args, student=student),
            data=json.dumps(data), content_type='application/json')

    def make_survey(self, *, publish=False, kind='short_text'):
        survey = services.create_survey(self.staff, {'title': 'Опрос API'})
        services.save_audience(survey.pk, self.staff,
            {'mode': 'individual', 'included_users': [self.student.pk]}, survey.revision)
        survey.refresh_from_db()
        services.save_question(survey.pk, self.staff,
            {'title': 'Вопрос API', 'answer_type': kind, 'required': True, 'options_text': 'Да\nНет'}, survey.revision)
        survey.refresh_from_db()
        if publish:
            services.publish_survey(survey.pk, self.staff,
                services.publish_token(survey, self.staff, [self.student.pk]))
            survey.refresh_from_db()
        return survey

    def test_full_json_flow_and_results(self):
        response = self.send('post', 'list', {'title': 'Обратная связь', 'description': 'Учёба'})
        self.assertEqual(response.status_code, 201)
        survey = response.json()
        pk = survey['id']
        response = self.send('put', 'audience', {'revision': survey['revision'],
            'mode': 'individual', 'included_users': [self.student.pk]}, pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['recipients']['count'], 1)
        response = self.send('post', 'questions', {'revision': response.json()['revision'],
            'title': 'Как проходит учёба?', 'answer_type': 'single_choice', 'required': True,
            'options': ['Хорошо', 'Нужна помощь']}, pk)
        self.assertEqual(response.status_code, 201)
        question = response.json()['question']
        preview = self.client.get(self.url('preview', pk)).json()
        self.assertEqual(preview['errors'], [])
        published = self.send('post', 'publish', {'token': preview['publish_token']}, pk)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()['status'], 'published')

        self.client.force_login(self.student)
        listing = self.client.get(self.url('list', student=True)).json()
        self.assertEqual(listing['count'], 1)
        assignment_pk = listing['results'][0]['id']
        detail = self.client.get(self.url('detail', assignment_pk, student=True)).json()
        self.assertNotIn('audience', detail['survey'])
        self.assertEqual(detail['response']['revision'], 0)
        submitted = self.send('put', 'response', {'revision': 0, 'final': True,
            'answers': {str(question['id']): question['options'][0]['id']}}, assignment_pk, student=True)
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.json()['status'], 'submitted')

        self.client.force_login(self.staff)
        results = self.client.get(self.url('results', pk)).json()
        self.assertEqual(results['counts'], {'total': 1, 'submitted': 1, 'started': 0, 'not_started': 0})
        response_pk = results['recipients']['results'][0]['response_id']
        detail = self.client.get(self.url('response_detail', pk, response_pk)).json()
        self.assertEqual(detail['display_answers'][str(question['id'])], 'Хорошо')
        exported = self.client.get(self.url('export', pk))
        self.assertIn('Хорошо', b''.join(exported.streaming_content).decode('utf-8-sig'))

    def test_staff_api_rejects_anonymous_and_student_without_redirects(self):
        self.client.logout()
        response = self.client.get(self.url('list'))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'unauthenticated')
        self.client.force_login(self.student)
        for name in ('list', 'metadata', 'recipients'):
            response = self.client.get(self.url(name))
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['error']['code'], 'forbidden')

    def test_metadata_and_paginated_searchable_lookups(self):
        response = self.client.get(self.url('metadata'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['answer_types']), 7)
        self.assertTrue(response.json()['csrf_token'])
        self.assertIn('profiles', response.json()['audience_choices'])
        self.assertNotIn('included_users', response.json()['audience_choices'])
        for kind in ('subjects', 'schools', 'courses'):
            data = self.client.get(self.url('lookups', kind), {'q': 'API', 'page_size': 1}).json()
            self.assertEqual(data['count'], 1)
        self.assertEqual(self.client.get(self.url('lookups', 'users')).status_code, 404)

    def test_recipient_search_defaults_to_active_and_excludes_staff(self):
        response = self.client.get(self.url('recipients'))
        self.assertEqual({u['id'] for u in response.json()['results']}, {self.student.pk, self.other.pk})
        data = self.client.get(self.url('recipients'), {'q': 'Анна', 'grades': ['11'],
            'courses': [self.course.pk]}).json()
        self.assertEqual([u['id'] for u in data['results']], [self.student.pk])
        for query in ('Иванова Анна', 'Анна Иванова', 'anna@example.test'):
            response = self.client.get(self.url('recipients'), {'q': query})
            self.assertEqual([u['id'] for u in response.json()['results']], [self.student.pk])

    def test_audience_preview_is_read_only_and_exclusions_win(self):
        count = SurveysBuilder.objects.count()
        response = self.send('post', 'audience_preview', {'mode': 'filters', 'grades': ['11'],
            'included_users': [self.other.pk], 'excluded_users': [self.student.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([u['id'] for u in response.json()['results']], [self.other.pk])
        self.assertEqual(SurveysBuilder.objects.count(), count)

    def test_invalid_audience_types_and_ids_do_not_change_selection(self):
        survey = self.make_survey()
        for changes in ({'included_users': [self.staff.pk]}, {'mode': 'filters', 'grades': ['99']},
                        {'mode': 'filters', 'courses': [99999]}, {'included_users': [True]}, {'grades': '11'},
                        {'region': {}}, {'mode': 'typo'}, {'account_actve': 'yes'}):
            with self.subTest(changes=changes):
                response = self.send('put', 'audience', {'revision': survey.revision,
                    'mode': 'individual', 'included_users': [self.student.pk], **changes}, survey.pk)
                self.assertEqual(response.status_code, 400)
        survey.refresh_from_db()
        self.assertEqual(list(services.audience_for(survey)), [self.student])

    def test_audience_pagination_error_rolls_back_mutation(self):
        survey = self.make_survey()
        response = self.client.put(self.url('audience', survey.pk) + '?page=999',
            data=json.dumps({'revision': survey.revision, 'mode': 'individual',
                             'included_users': [self.other.pk]}), content_type='application/json')
        self.assertEqual(response.status_code, 404)
        survey.refresh_from_db()
        self.assertEqual(survey.revision, 3)
        self.assertEqual(list(services.audience_for(survey)), [self.student])

    def test_revision_conflict_and_partial_update(self):
        survey = self.make_survey()
        original_revision = survey.revision
        response = self.send('patch', 'detail', {'revision': original_revision, 'description': 'Новое'}, survey.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['title'], survey.title)
        response = self.send('patch', 'detail', {'revision': original_revision, 'title': 'Перезаписать'}, survey.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'revision_conflict')
        survey.refresh_from_db()
        self.assertEqual(survey.title, 'Опрос API')

    def test_invalid_json_types_unknown_fields_and_content_type(self):
        url = self.url('list')
        for body in ('[]', '{', 'null', '{"title": NaN}', '{"title": {}}',
                     '{"title": "test", "status": "published"}'):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post(url, {'title': 'No JSON'}).status_code, 415)
        self.assertFalse(SurveysBuilder.objects.exists())

    def test_revision_requires_integer(self):
        survey = self.make_survey()
        for value in (None, True, 1.1, '3', [], {}):
            with self.subTest(value=value):
                response = self.send('patch', 'detail', {'title': 'Bad', 'revision': value}, survey.pk)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error']['code'], 'invalid_revision')

    def test_field_errors_and_missing_survey_are_json(self):
        response = self.send('post', 'list', {'title': ''})
        self.assertEqual(response.status_code, 400)
        self.assertIn('title', response.json()['error']['fields'])
        response = self.client.get(self.url('detail', 99999))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'not_found')

    def test_pagination_validation_and_status_search(self):
        self.make_survey()
        self.make_survey(publish=True)
        response = self.client.get(self.url('list'), {'status': 'published', 'q': 'API', 'page_size': 1})
        self.assertEqual(response.json()['count'], 1)
        for params in ({'page': 0}, {'page_size': 101}, {'page': 'no'}, {'status': 'unknown'}):
            self.assertEqual(self.client.get(self.url('list'), params).status_code, 400)
        self.assertEqual(self.client.get(self.url('list'), {'page': 50}).status_code, 404)

    def test_question_patch_duplicate_reorder_delete(self):
        survey = self.make_survey(kind='single_choice')
        question = survey.questions.get()
        option_ids = list(question.options.values_list('pk', flat=True))
        response = self.send('patch', 'question_detail', {'revision': survey.revision,
            'title': 'Другой заголовок'}, survey.pk, question.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([o['id'] for o in response.json()['question']['options']], option_ids)
        response = self.send('post', 'duplicate_question', {'revision': response.json()['revision']}, survey.pk, question.pk)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        ids = [q['id'] for q in data['questions']]
        response = self.send('put', 'reorder', {'revision': data['revision'], 'question_ids': ids[::-1]}, survey.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([q['id'] for q in response.json()['questions']], ids[::-1])
        response = self.send('delete', 'question_detail', {'revision': response.json()['revision']}, survey.pk, ids[-1])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(survey.questions.count(), 1)

    def test_bad_reorder_is_atomic(self):
        survey = self.make_survey()
        original = list(survey.questions.values_list('pk', 'position'))
        for ids in ([], [99999], [original[0][0], original[0][0]], [True], [[1]]):
            response = self.send('put', 'reorder', {'revision': survey.revision, 'question_ids': ids}, survey.pk)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(list(survey.questions.values_list('pk', 'position')), original)
        survey.refresh_from_db()
        self.assertEqual(survey.revision, 3)

    def test_question_option_validation_and_foreign_question(self):
        survey = self.make_survey()
        other = self.make_survey()
        for options in (['Один'], ['Один', 'Один'], ['Два\nТри', 'Один'], [False, 'Да'], {'a': 'b'}):
            response = self.send('post', 'questions', {'revision': survey.revision,
                'title': 'Выбор', 'answer_type': 'single_choice', 'options': options}, survey.pk)
            self.assertEqual(response.status_code, 400)
        response = self.send('patch', 'question_detail', {'revision': survey.revision,
            'title': 'Чужой'}, survey.pk, other.questions.get().pk)
        self.assertEqual(response.status_code, 404)

    def test_publish_rechecks_live_audience(self):
        survey = self.make_survey()
        services.save_audience(survey.pk, self.staff, {'mode': 'filters', 'grades': ['11']}, survey.revision)
        preview = self.client.get(self.url('preview', survey.pk)).json()
        UserInfo.objects.filter(user=self.other).update(next_year_class_digit=11)
        response = self.send('post', 'publish', {'token': preview['publish_token']}, survey.pk)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(survey.assignments.exists())
        preview = self.client.get(self.url('preview', survey.pk)).json()
        self.assertEqual(self.send('post', 'publish', {'token': preview['publish_token']}, survey.pk).status_code, 200)
        self.assertEqual(survey.assignments.count(), 2)

    def test_published_audience_is_snapshot_and_cannot_be_edited(self):
        survey = self.make_survey(publish=True)
        UserInfo.objects.filter(user=self.student).update(first_name='Изменено', next_year_class_digit=10)
        data = self.client.get(self.url('audience', survey.pk)).json()
        self.assertEqual(data['recipients']['results'][0]['name'], 'Иванова Анна')
        response = self.send('put', 'audience', {'revision': survey.revision,
            'mode': 'individual', 'included_users': [self.other.pk]}, survey.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(survey.assignments.get().user_id, self.student.pk)

    def test_copy_close_archive_and_delete_draft(self):
        survey = self.make_survey(publish=True)
        response = self.send('post', 'copy', {}, survey.pk)
        self.assertEqual(response.status_code, 201)
        copied = response.json()
        self.assertEqual(copied['status'], 'draft')
        self.assertFalse(SurveyAssignment.objects.filter(survey_id=copied['id']).exists())
        response = self.send('delete', 'detail', {'revision': copied['revision']}, copied['id'])
        self.assertEqual(response.status_code, 204)
        response = self.send('post', 'close', {'revision': survey.revision}, survey.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'closed')
        response = self.send('post', 'archive', {'revision': response.json()['revision']}, survey.pk)
        self.assertEqual(response.json()['status'], 'archived')

    def test_csrf_required_for_all_mutations_and_metadata_supplies_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url('list'), '{}', content_type='application/json').status_code, 403)
        token = client.get(self.url('metadata')).json()['csrf_token']
        response = client.post(self.url('list'), '{"title":"CSRF"}', content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201)
        self.client.force_login(self.student)
        self.assertTrue(self.client.get(self.url('list', student=True)).json()['csrf_token'])

    def test_get_never_mutates(self):
        survey = self.make_survey()
        for name in ('publish', 'copy', 'close', 'archive', 'questions', 'reorder'):
            response = self.client.get(self.url(name, survey.pk))
            self.assertEqual(response.status_code, 405)
            self.assertTrue(response['Allow'])
        survey.refresh_from_db()
        self.assertEqual(survey.revision, 3)
        self.assertEqual(survey.status, 'draft')
        self.assertFalse(survey.assignments.exists())

    def test_preview_reports_unfinished_survey_without_token(self):
        survey = services.create_survey(self.staff, {'title': 'Пустой'})
        response = self.client.get(self.url('preview', survey.pk))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['errors'])
        self.assertIsNone(response.json()['publish_token'])

    def test_only_recipient_can_read_or_answer(self):
        survey = self.make_survey(publish=True)
        assignment = survey.assignments.get()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url('list', student=True)).json()['count'], 0)
        self.assertEqual(self.client.get(self.url('detail', assignment.pk, student=True)).status_code, 404)
        response = self.send('put', 'response', {'revision': 0, 'answers': {}, 'final': False}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SurveyResponse.objects.exists())

    def test_draft_privacy_required_answers_and_stale_revision(self):
        survey = self.make_survey(publish=True)
        assignment = survey.assignments.get()
        question = survey.questions.get()
        self.client.force_login(self.student)
        response = self.send('put', 'response', {'revision': 0, 'answers': {}}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 200)
        response_pk = response.json()['id']
        draft_revision = response.json()['revision']
        response = self.send('put', 'response', {'revision': 0, 'answers': {}}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 409)
        response = self.send('put', 'response', {'revision': draft_revision, 'answers': {}, 'final': True}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn(f'q_{question.pk}', response.json()['error']['fields'])
        response = self.send('put', 'response', {'revision': draft_revision,
            'answers': {str(question.pk): 'Личный черновик'}}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 200)
        self.client.force_login(self.staff)
        response = self.client.get(self.url('response_detail', survey.pk, response_pk))
        self.assertEqual(response.status_code, 404)
        results = self.client.get(self.url('results', survey.pk))
        self.assertEqual(results.json()['counts']['started'], 1)
        self.assertIsNone(results.json()['recipients']['results'][0]['response_id'])
        self.assertNotIn('Личный черновик', results.content.decode())

    def test_foreign_question_and_malformed_answers_rejected(self):
        survey = self.make_survey(publish=True)
        other_survey = self.make_survey()
        assignment = survey.assignments.get()
        question = survey.questions.get()
        self.client.force_login(self.student)
        for answers in ({str(other_survey.questions.get().pk): 'Чужой'}, {str(question.pk): {}},
                        {str(question.pk): []}, [], None):
            response = self.send('put', 'response', {'revision': 0, 'answers': answers}, assignment.pk, student=True)
            self.assertEqual(response.status_code, 400)
        response = self.send('put', 'response', {'revision': 0, 'answers': {}, 'final': 'false'}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SurveyResponse.objects.exists())

    def test_choice_cannot_use_option_from_another_question(self):
        survey = self.make_survey(publish=True, kind='single_choice')
        other = self.make_survey(kind='single_choice')
        assignment = survey.assignments.get()
        self.client.force_login(self.student)
        response = self.send('put', 'response', {'revision': 0, 'final': True,
            'answers': {str(survey.questions.get().pk): other.questions.get().options.first().pk}}, assignment.pk, student=True)
        self.assertEqual(response.status_code, 400)

    def test_all_answer_types_roundtrip(self):
        for kind, value in [('short_text', 'Текст'), ('long_text', 'Длинный текст'), ('number', '12.500000'),
                            ('date', '2026-10-28'), ('scale', 4), ('multiple_choice', None)]:
            with self.subTest(kind=kind):
                self.client.force_login(self.staff)
                survey = self.make_survey(publish=True, kind=kind)
                assignment = survey.assignments.get()
                question = survey.questions.get()
                if kind == 'multiple_choice':
                    value = list(question.options.values_list('pk', flat=True))
                self.client.force_login(self.student)
                response = self.send('put', 'response', {'revision': 0, 'final': True,
                    'answers': {str(question.pk): value}}, assignment.pk, student=True)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['answers'][str(question.pk)], value)
                revision = response.json()['revision']
                response = self.send('put', 'response', {'revision': revision, 'answers': {}}, assignment.pk, student=True)
                self.assertEqual(response.status_code, 400)

    def test_deadline_and_closed_surveys_reject_writes(self):
        for close in (False, True):
            survey = self.make_survey(publish=True)
            assignment = survey.assignments.get()
            if close:
                services.change_status(survey.pk, self.staff, 'close', survey.revision)
            else:
                SurveysBuilder.objects.filter(pk=survey.pk).update(deadline=timezone.now() - timedelta(seconds=1))
            self.client.force_login(self.student)
            response = self.send('put', 'response', {'revision': 0, 'answers': {}}, assignment.pk, student=True)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(SurveyResponse.objects.filter(assignment=assignment).exists())
