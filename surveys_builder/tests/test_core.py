"""Интеграционные проверки конструктора на настоящих моделях проекта."""
import csv
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Notification, RegistrationPersonalData, UserNotification
from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo
from .. import services
from ..forms import AudienceForm, ResponseForm
from ..models import QuestionBlock, QuestionOptions, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder
from ..selectors import resolve_audience

User = get_user_model()


class SurveyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username='staff', is_staff=True)
        cls.student = User.objects.create_user(username='student')
        RegistrationPersonalData.objects.create(user=cls.student, email='student-registration@example.test', current_step='finish')
        cls.other = User.objects.create_user(username='other')
        RegistrationPersonalData.objects.create(user=cls.other, email='other-registration@example.test', current_step='finish')
        cls.inactive = User.objects.create_user(username='inactive', is_active=False)
        UserInfo.objects.create(user=cls.student, first_name='Анна', last_name='Иванова',
                                next_year_class_digit=11, internal_study_profile='it', status='SCHOLAR', region='Москва')
        UserInfo.objects.create(user=cls.other, next_year_class_digit=10, internal_study_profile='medic', status='ALUMNUS')
        cls.subject = Subject.objects.create(name='Математика', slug='survey-math')
        cls.school = School.objects.create(name='Школа опросов')
        cls.course = Course.objects.create(school=cls.school, subject=cls.subject, title='Подготовка')
        CourseSelection.objects.create(user=cls.student, course=cls.course)
        cls.student.user_info.planned_exams.add(cls.subject)

    def setUp(self):
        self.client.force_login(self.staff)

    def make_survey(self, *, question=True):
        survey = services.create_survey(self.staff, {'title': 'Учёба', 'description': 'Описание'})
        survey = services.save_audience(survey.pk, self.staff,
                                       {'mode': 'individual', 'included_users': [self.student.pk]}, survey.revision)
        if question:
            self.add_question(survey)
        survey.refresh_from_db()
        return survey

    def add_question(self, survey, kind='short_text', required=True):
        survey.refresh_from_db()
        return services.save_question(survey.pk, self.staff, {'title': kind, 'answer_type': kind,
                                      'required': required, 'block': (survey.blocks.first() or QuestionBlock.objects.create(survey=survey, title='Основной блок')).pk, 'options_text': 'Первый\nВторой'}, survey.revision)

    def publish(self, survey):
        survey.refresh_from_db()
        ids = list(services.audience_for(survey).values_list('pk', flat=True))
        services.publish_survey(survey.pk, self.staff, services.publish_token(survey, self.staff, ids))
        survey.refresh_from_db()
        return survey.assignments.get(user=self.student)

    def audience_ids(self, **data):
        form = AudienceForm({'mode': 'filters', **data})
        self.assertTrue(form.is_valid(), form.errors)
        return set(resolve_audience(form.cleaned_data).values_list('pk', flat=True))

    def test_filters_use_project_profiles_grades_statuses_and_exams(self):
        self.assertEqual(self.audience_ids(grades=['11'], profiles=['it'], statuses=['SCHOLAR'],
            planned_exams=[self.subject.pk]), {self.student.pk})
        self.assertEqual(self.audience_ids(statuses=['ALUMNUS']), {self.other.pk})
        self.assertNotIn(self.inactive.pk, self.audience_ids())
        self.assertNotIn(self.staff.pk, self.audience_ids())

    def test_course_filters_must_match_same_course(self):
        school = School.objects.create(name='Другая школа')
        subject = Subject.objects.create(name='Химия', slug='survey-chemistry')
        course = Course.objects.create(school=school, subject=subject, title='Химия')
        CourseSelection.objects.create(user=self.student, course=course)
        self.assertEqual(self.audience_ids(schools=[school.pk], subjects=[self.subject.pk]), set())
        self.assertEqual(self.audience_ids(schools=[self.school.pk], subjects=[self.subject.pk]), {self.student.pk})

    def test_assignment_modes_are_mutually_exclusive(self):
        form = AudienceForm({'mode': 'filters', 'grades': ['11'], 'included_users': [self.other.pk]})
        self.assertFalse(form.is_valid())
        self.assertEqual(self.audience_ids(mode='individual', grades=['10'], included_users=[self.student.pk]), {self.student.pk})

    def test_invalid_audience_ids_and_empty_individual_rejected(self):
        for data in ({'mode': 'individual'}, {'mode': 'filters', 'grades': ['99']},
                     {'mode': 'individual', 'included_users': [self.staff.pk]},
                     {'mode': 'filters', 'subjects': [99999]}):
            self.assertFalse(AudienceForm(data).is_valid())

    def test_empty_audience_is_not_saved(self):
        survey = services.create_survey(self.staff, {'title': 'Новый'})
        with self.assertRaises(services.InvalidForm):
            services.save_audience(survey.pk, self.staff, {'mode': 'filters', 'grades': ['1']}, survey.revision)
        survey.refresh_from_db()
        self.assertFalse(survey.audience_configured)

    def test_wizard_blocks_then_questions_then_assignment_and_submission(self):
        response = self.client.post(reverse('surveys_builder:constructor'), {'title': 'Полный путь'})
        survey = SurveysBuilder.objects.get(title='Полный путь')
        self.assertRedirects(response, reverse('surveys_builder:block_create', args=[survey.pk]))
        self.client.post(reverse('surveys_builder:block_create', args=[survey.pk]), {'title': 'Учёба', 'revision': survey.revision})
        block = survey.blocks.get()
        survey.refresh_from_db()
        self.client.post(reverse('surveys_builder:block_content', args=[survey.pk, block.pk]),
            {'title': 'Оцените', 'answer_type': 'scale', 'required': True, 'revision': survey.revision})
        survey.refresh_from_db()
        self.assertFalse(survey.audience_configured)
        response = self.client.post(reverse('surveys_builder:audience', args=[survey.pk]),
            {'mode': 'individual', 'included_users': [self.student.pk], 'revision': survey.revision, 'action': 'save'})
        self.assertRedirects(response, reverse('surveys_builder:preview', args=[survey.pk]))
        preview = self.client.get(response.url)
        self.assertNotContains(preview, 'name="q_')
        self.client.post(reverse('surveys_builder:publish', args=[survey.pk]), {'token': preview.context['publish_token']})
        self.client.force_login(self.student)
        assignment = survey.assignments.get()
        question = survey.questions.get()
        response = self.client.post(reverse('student_surveys:answer', args=[assignment.pk]),
            {'action': 'submit', 'response_revision': 0, f'q_{question.pk}': '5'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(assignment.response.answers.get().value, 5)

    def test_publication_notifies_each_assigned_student_once(self):
        survey = self.make_survey()
        survey.included_users.add(self.other)
        self.publish(survey)
        notification = Notification.objects.get(message__contains=survey.title)
        self.assertEqual(notification.sender, self.staff)
        self.assertEqual(
            set(UserNotification.objects.filter(notification=notification).values_list('recipient_id', flat=True)),
            {self.student.pk, self.other.pk},
        )
        self.assertFalse(UserNotification.objects.filter(notification=notification, is_seen=True).exists())

        self.client.force_login(self.student)
        inbox = self.client.get(reverse('notifications'))
        self.assertContains(inbox, notification.message)
        self.assertEqual(inbox.context['unread_notifications_count'], 1)

        services.publish_survey(survey.pk, self.staff, None)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(UserNotification.objects.count(), 2)

    def test_notification_failure_rolls_back_publication(self):
        survey = self.make_survey()
        ids = list(services.audience_for(survey).values_list('pk', flat=True))
        token = services.publish_token(survey, self.staff, ids)
        with patch.object(UserNotification.objects, 'bulk_create', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                services.publish_survey(survey.pk, self.staff, token)
        survey.refresh_from_db()
        self.assertTrue(survey.is_draft)
        self.assertFalse(survey.assignments.exists())
        self.assertFalse(Notification.objects.exists())

    def test_questions_can_be_created_before_audience_but_require_block(self):
        survey = services.create_survey(self.staff, {'title': 'Новый'})
        with self.assertRaises(ValidationError):
            services.save_question(survey.pk, self.staff, {'title': 'Вопрос'}, survey.revision)
        question = self.add_question(survey)
        self.assertIsNotNone(question.block_id)
        self.assertFalse(survey.audience_configured)
        self.assertEqual(self.client.get(reverse('surveys_builder:survey_detail', args=[survey.pk])).status_code, 200)

    def test_invalid_form_errors_render_without_losing_input(self):
        response = self.client.post(reverse('surveys_builder:constructor'), {'title': ''})
        self.assertTrue(response.context['form'].errors)
        survey = self.make_survey()
        block = survey.blocks.get()
        response = self.client.post(reverse('surveys_builder:block_content', args=[survey.pk, block.pk]),
            {'title': 'Вопрос', 'answer_type': 'single_choice', 'options_text': 'Один', 'revision': survey.revision})
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response, 'Вопрос')

    def test_options_validation_and_ids_preserved_on_reorder(self):
        survey = self.make_survey(question=False)
        question = self.add_question(survey, 'single_choice')
        old = dict(question.options.values_list('text', 'pk'))
        survey.refresh_from_db()
        question = services.save_question(survey.pk, self.staff, {'title': 'Выбор', 'answer_type': 'single_choice',
                                          'options_text': 'Второй\nПервый'}, survey.revision, question.pk)
        self.assertEqual(list(question.options.values_list('pk', flat=True)), [old['Второй'], old['Первый']])
        for options in ('Один', 'Один\nОдин', 'x' * 201 + '\nДва'):
            survey.refresh_from_db()
            with self.assertRaises(services.InvalidForm):
                services.save_question(survey.pk, self.staff, {'title': 'Ошибка', 'answer_type': 'single_choice',
                                        'options_text': options}, survey.revision, question.pk)
        question.refresh_from_db()
        self.assertEqual(question.title, 'Выбор')

    def test_question_reorder_duplicate_delete(self):
        survey = self.make_survey()
        first = survey.questions.get()
        second = self.add_question(survey, 'single_choice')
        survey.refresh_from_db()
        services.question_action(survey.pk, second.pk, self.staff, 'up', survey.revision)
        self.assertEqual(list(survey.questions.values_list('pk', flat=True)), [second.pk, first.pk])
        survey.refresh_from_db()
        services.question_action(survey.pk, second.pk, self.staff, 'duplicate', survey.revision)
        duplicate = survey.questions.all()[1]
        self.assertEqual(duplicate.options.count(), 2)
        survey.refresh_from_db()
        services.question_action(survey.pk, first.pk, self.staff, 'delete', survey.revision)
        self.assertEqual(list(survey.questions.values_list('position', flat=True)), [1, 2])

    def test_question_from_other_survey_cannot_be_edited(self):
        survey, other = self.make_survey(), self.make_survey()
        with self.assertRaises(Http404):
            services.save_question(survey.pk, self.staff, {}, survey.revision, other.questions.get().pk)

    def test_stale_survey_revision_prevents_overwrite(self):
        survey = self.make_survey()
        services.update_survey(survey.pk, self.staff, {'title': 'Новое'}, survey.revision)
        with self.assertRaises(ValidationError):
            services.update_survey(survey.pk, self.staff, {'title': 'Старое'}, survey.revision)
        survey.refresh_from_db()
        self.assertEqual(survey.title, 'Новое')

    def test_preview_does_not_accept_answers_or_publish(self):
        survey = self.make_survey()
        url = reverse('surveys_builder:preview', args=[survey.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertFalse(SurveyResponse.objects.exists())
        self.assertFalse(survey.assignments.exists())

    def test_publication_requires_questions_and_audience(self):
        survey = services.create_survey(self.staff, {'title': 'Пустой'})
        token = services.publish_token(survey, self.staff, [])
        with self.assertRaises(ValidationError):
            services.publish_survey(survey.pk, self.staff, token)
        self.assertFalse(survey.assignments.exists())

    def test_publish_tokens_tampered_expired_actor_and_revision(self):
        survey = self.make_survey()
        token = services.publish_token(survey, self.staff, [self.student.pk])
        with self.assertRaises(ValidationError):
            services.publish_survey(survey.pk, self.staff, token + 'x')
        other_staff = User.objects.create_user(username='staff2', is_staff=True)
        with self.assertRaises(ValidationError):
            services.publish_survey(survey.pk, other_staff, token)
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 7200):
            with self.assertRaises(ValidationError):
                services.publish_survey(survey.pk, self.staff, token)
        self.add_question(survey)
        with self.assertRaises(ValidationError):
            services.publish_survey(survey.pk, self.staff, token)
        self.assertFalse(survey.assignments.exists())

    def test_audience_change_after_preview_requires_new_confirmation(self):
        survey = self.make_survey()
        survey = services.save_audience(survey.pk, self.staff, {'mode': 'filters', 'grades': ['11']}, survey.revision)
        token = services.publish_token(survey, self.staff, [self.student.pk])
        UserInfo.objects.filter(user=self.other).update(next_year_class_digit=11)
        with self.assertRaises(ValidationError):
            services.publish_survey(survey.pk, self.staff, token)
        self.assertFalse(survey.assignments.exists())

    def test_publication_snapshot_stays_fixed_and_publish_is_idempotent(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        UserInfo.objects.filter(user=self.student).update(last_name='Изменено', next_year_class_digit=9)
        services.publish_survey(survey.pk, self.staff, 'duplicate-request')
        assignment.refresh_from_db()
        self.assertEqual(assignment.recipient_snapshot['name'], 'Иванова Анна')
        self.assertEqual(survey.assignments.count(), 1)

    def test_published_survey_all_content_mutations_rejected(self):
        survey = self.make_survey()
        self.publish(survey)
        question = survey.questions.get()
        actions = [
            lambda: services.update_survey(survey.pk, self.staff, {'title': 'Нельзя'}, survey.revision),
            lambda: services.save_audience(survey.pk, self.staff, {'mode': 'filters'}, survey.revision),
            lambda: services.save_question(survey.pk, self.staff, {}, survey.revision),
            lambda: services.question_action(survey.pk, question.pk, self.staff, 'delete', survey.revision),
            lambda: services.delete_survey(survey.pk, self.staff, survey.revision),
        ]
        for action in actions:
            with self.assertRaises(ValidationError):
                action()
        self.assertEqual(survey.questions.count(), 1)

    def test_copy_has_questions_and_audience_but_no_answers_or_assignments(self):
        survey = self.make_survey()
        self.add_question(survey, 'multiple_choice')
        self.publish(survey)
        copy = services.copy_survey(survey.pk, self.staff)
        self.assertTrue(copy.is_draft)
        self.assertFalse(copy.audience_configured)
        self.assertEqual(copy.questions.count(), 2)
        self.assertEqual(copy.questions.last().options.count(), 2)
        self.assertFalse(copy.assignments.exists())
        self.assertIsNone(copy.deadline)

    def test_all_answer_types_and_serialization(self):
        survey = self.make_survey(question=False)
        questions = [self.add_question(survey, kind) for kind in QuestionsBuilder.AnswerType.values]
        assignment = self.publish(survey)
        values = ['Коротко', 'Длинно\nОтвет', str(questions[2].options.first().pk),
                  [str(o.pk) for o in questions[3].options.all()], '12.500000', '2026-10-16', '4']
        data = {f'q_{q.pk}': value for q, value in zip(questions, values)}
        response = services.save_response(assignment.pk, self.student, data, final=True, revision=0)
        saved = list(response.answers.values_list('value', flat=True))
        self.assertEqual(saved, ['Коротко', 'Длинно\nОтвет', questions[2].options.first().pk,
                                list(questions[3].options.values_list('pk', flat=True)), '12.500000', '2026-10-16', 4])
        self.assertEqual(response.status, 'submitted')

    def test_required_fields_only_on_final_and_draft_resumes(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        response = services.save_response(assignment.pk, self.student, {}, final=False, revision=0)
        self.assertEqual(response.status, 'draft')
        with self.assertRaises(services.InvalidForm):
            services.save_response(assignment.pk, self.student, {}, final=True, revision=response.revision)
        response.refresh_from_db()
        self.assertEqual(response.status, 'draft')
        data = {f'q_{survey.questions.get().pk}': 'Сохранено'}
        response = services.save_response(assignment.pk, self.student, data, final=False, revision=response.revision)
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse('student_surveys:answer', args=[assignment.pk])), 'Сохранено')
        services.save_response(assignment.pk, self.student, data, final=True, revision=response.revision)

    def test_choice_cannot_reference_option_from_other_question(self):
        survey = self.make_survey(question=False)
        a, b = self.add_question(survey, 'single_choice'), self.add_question(survey, 'multiple_choice')
        assignment = self.publish(survey)
        for data in ({f'q_{a.pk}': b.options.first().pk, f'q_{b.pk}': [b.options.first().pk]},
                     {f'q_{a.pk}': a.options.first().pk, f'q_{b.pk}': [a.options.first().pk]}):
            with self.assertRaises(services.InvalidForm):
                services.save_response(assignment.pk, self.student, data, final=True, revision=0)
        self.assertFalse(SurveyResponse.objects.exists())

    def test_unknown_question_and_invalid_date_number_scale_rejected(self):
        survey = self.make_survey(question=False)
        for kind, value in [('number', 'NaN'), ('date', '2026-02-31'), ('scale', '6')]:
            question = self.add_question(survey, kind)
            form = ResponseForm({f'q_{question.pk}': value}, questions=[question])
            self.assertFalse(form.is_valid())
        self.assertFalse(ResponseForm({'q_999999': 'Чужой'}, questions=[]).is_valid())

    def test_submitted_answer_and_stale_draft_cannot_be_overwritten(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        data = {f'q_{survey.questions.get().pk}': 'Первый'}
        response = services.save_response(assignment.pk, self.student, data, final=False, revision=0)
        with self.assertRaises(ValidationError):
            services.save_response(assignment.pk, self.student, data, final=False, revision=0)
        response = services.save_response(assignment.pk, self.student, data, final=True, revision=response.revision)
        with self.assertRaises(ValidationError):
            services.save_response(assignment.pk, self.student, data, final=False, revision=response.revision)
        self.assertEqual(response.answers.get().value, 'Первый')

    def test_only_assignment_owner_can_answer_or_read(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        with self.assertRaises(Http404):
            services.save_response(assignment.pk, self.other, {}, final=False, revision=0)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('student_surveys:answer', args=[assignment.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('student_surveys:list')), survey.title)

    def test_inactive_user_cannot_submit(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        self.student.is_active = False
        with self.assertRaises(PermissionDenied):
            services.save_response(assignment.pk, self.student, {}, final=False, revision=0)

    def test_deadline_close_and_archive_block_answers(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        SurveysBuilder.objects.filter(pk=survey.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            services.save_response(assignment.pk, self.student, {}, final=False, revision=0)
        SurveysBuilder.objects.filter(pk=survey.pk).update(deadline=None)
        services.change_status(survey.pk, self.staff, 'close', survey.revision)
        with self.assertRaises(ValidationError):
            services.save_response(assignment.pk, self.student, {}, final=False, revision=0)
        survey.refresh_from_db()
        services.change_status(survey.pk, self.staff, 'archive', survey.revision)
        with self.assertRaises(ValidationError):
            services.publish_survey(survey.pk, self.staff, None)
        self.assertEqual(survey.assignments.count(), 1)

    def test_past_deadline_cannot_be_saved_or_published(self):
        survey = self.make_survey()
        with self.assertRaises(services.InvalidForm):
            services.save_audience(survey.pk, self.staff, {'mode': 'filters', 'deadline': timezone.now()-timedelta(days=1)}, survey.revision)
        SurveysBuilder.objects.filter(pk=survey.pk).update(deadline=timezone.now()-timedelta(days=1))
        survey.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.publish(survey)

    def test_staff_access_required_for_builder_and_results(self):
        survey = self.make_survey()
        with self.assertRaises(PermissionDenied):
            services.create_survey(self.student, {'title': 'Нет'})
        self.client.force_login(self.student)
        for name in ('constructor', 'survey_detail', 'audience', 'preview', 'results', 'results_csv'):
            response = self.client.get(reverse('surveys_builder:' + name, args=[] if name == 'constructor' else [survey.pk]))
            self.assertEqual(response.status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('student_surveys:list')).status_code, 302)

    def test_get_never_publishes_or_deletes_and_posts_require_csrf(self):
        survey = self.make_survey()
        self.assertEqual(self.client.get(reverse('surveys_builder:publish', args=[survey.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse('surveys_builder:survey_copy', args=[survey.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse('surveys_builder:survey_delete', args=[survey.pk])).status_code, 200)
        self.assertTrue(SurveysBuilder.objects.filter(pk=survey.pk).exists())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(reverse('surveys_builder:survey_delete', args=[survey.pk]), {'revision': survey.revision}).status_code, 403)

    def test_draft_response_not_visible_to_staff_or_export(self):
        survey = self.make_survey()
        assignment = self.publish(survey)
        response = services.save_response(assignment.pk, self.student, {f'q_{survey.questions.get().pk}': 'Приватный черновик'}, final=False, revision=0)
        self.assertEqual(self.client.get(reverse('surveys_builder:response_detail', args=[survey.pk, response.pk])).status_code, 404)
        results = self.client.get(reverse('surveys_builder:results', args=[survey.pk]))
        self.assertEqual(results.context['counts'], {'total': 1, 'submitted': 0, 'started': 1, 'not_started': 0})
        exported = b''.join(self.client.get(reverse('surveys_builder:results_csv', args=[survey.pk])).streaming_content).decode('utf-8-sig')
        self.assertNotIn('Приватный черновик', exported)
        self.assertEqual(len(list(csv.reader(io.StringIO(exported), delimiter=';'))), 1)

    def test_results_and_csv_include_labels_and_escape_formulas(self):
        survey = self.make_survey()
        question = survey.questions.get()
        question.title = '=заголовок'
        question.save()
        assignment = self.publish(survey)
        response = services.save_response(assignment.pk, self.student, {f'q_{question.pk}': '=SUM(1,2)'}, final=True, revision=0)
        self.assertContains(self.client.get(reverse('surveys_builder:response_detail', args=[survey.pk, response.pk])), '=SUM(1,2)')
        export = self.client.get(reverse('surveys_builder:results_csv', args=[survey.pk]))
        rows = list(csv.reader(io.StringIO(b''.join(export.streaming_content).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[0][-1], "[Основной блок] =заголовок")
        self.assertEqual(rows[1][-1], "'=SUM(1,2)")
        self.assertEqual(rows[1][1], 'Иванова Анна')
        self.assertEqual(self.client.get(reverse('surveys_builder:results', args=[survey.pk])).context['counts']['submitted'], 1)

    def test_unique_recipient_constraint(self):
        survey = self.make_survey()
        self.publish(survey)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SurveyAssignment.objects.create(survey=survey, user=self.student)

    def test_every_staff_page_renders(self):
        survey = self.make_survey()
        for name in ('survey_edit', 'audience', 'survey_detail', 'preview', 'results', 'survey_delete'):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse('surveys_builder:' + name, args=[survey.pk])).status_code, 200)
        question = survey.questions.get()
        for name in ('question_edit', 'question_delete'):
            self.assertEqual(self.client.get(reverse('surveys_builder:' + name, args=[survey.pk, question.pk])).status_code, 200)
        self.publish(survey)
        for name in ('survey_edit', 'audience', 'survey_detail', 'preview', 'results', 'survey_delete'):
            self.assertEqual(self.client.get(reverse('surveys_builder:' + name, args=[survey.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('surveys_builder:lifecycle', args=[survey.pk, 'close'])).status_code, 200)

    def test_missing_lookup_shows_reselect_error(self):
        survey = self.make_survey()
        survey = services.save_audience(survey.pk, self.staff, {'mode': 'filters', 'courses': [self.course.pk]}, survey.revision)
        self.course.delete()
        response = self.client.get(reverse('surveys_builder:audience', args=[survey.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Состав справочников изменился')

    def test_preview_audience_does_not_change_saved_selection(self):
        survey = self.make_survey()
        response = self.client.post(reverse('surveys_builder:audience', args=[survey.pk]),
                                    {'mode': 'filters', 'statuses': ['ALUMNUS'], 'action': 'preview', 'revision': survey.revision})
        self.assertEqual(response.context['recipient_count'], 1)
        self.assertEqual(list(services.audience_for(survey)), [self.student])

    def test_registration_is_mandatory_and_selection_filters_still_work(self):
        RegistrationPersonalData.objects.filter(user=self.other).update(current_step='initial_data')
        self.assertEqual(self.audience_ids(), {self.student.pk})
        info = self.student.user_info
        self.assertIn(self.student.pk, self.audience_ids(steps=[info.selection_step]))

    def test_project_navigation_links_render(self):
        from django.template.loader import render_to_string
        sidebar = render_to_string('core/_sidebar.html', {'user': self.student})
        self.assertEqual(sidebar.count(reverse('student_surveys:list')), 2)
        response = self.client.get(reverse('staff_users_list'))
        self.assertContains(response, reverse('surveys_builder:constructor'))

    def test_publication_is_atomic_if_assignment_write_fails(self):
        survey = self.make_survey()
        with patch.object(SurveyAssignment.objects, 'bulk_create', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.publish(survey)
        survey.refresh_from_db()
        self.assertTrue(survey.is_draft)
        self.assertFalse(survey.assignments.exists())

    def test_changing_question_type_clears_unused_options(self):
        survey = self.make_survey(question=False)
        question = self.add_question(survey, 'single_choice')
        survey.refresh_from_db()
        services.save_question(survey.pk, self.staff, {'title': 'Текст', 'answer_type': 'short_text'}, survey.revision, question.pk)
        self.assertFalse(question.options.exists())

    def test_optional_choice_can_be_cleared_and_multiple_values_are_unique(self):
        survey = self.make_survey(question=False)
        single = self.add_question(survey, 'single_choice', required=False)
        multi = self.add_question(survey, 'multiple_choice', required=False)
        option = multi.options.first()
        form = ResponseForm({f'q_{single.pk}': '', f'q_{multi.pk}': [str(option.pk), str(option.pk)]}, questions=[single, multi])
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.answer_values(), {single.pk: None, multi.pk: [option.pk]})


class LegacySurveyMigrationTests(TransactionTestCase):
    """Существующие опросы, вопросы и варианты должны пережить обновление."""
    old = ('surveys_builder', '0003_alter_questionsbuilder_answer_type_questionoptions')
    new = ('surveys_builder', '0007_merge_block_content_and_response_protection')

    def test_legacy_survey_becomes_draft_without_data_loss(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.old])
        try:
            apps = executor.loader.project_state([self.old]).apps
            survey = apps.get_model('surveys_builder', 'SurveysBuilder').objects.create(title='Старый опрос')
            question = apps.get_model('surveys_builder', 'QuestionsBuilder').objects.create(survey=survey, title='Выбор', answer_type='single_choice')
            option = apps.get_model('surveys_builder', 'QuestionOptions').objects.create(question=question, text='Старый вариант')
            executor = MigrationExecutor(connection)
            executor.migrate([self.new])
            migrated = SurveysBuilder.objects.get(pk=survey.pk)
            self.assertTrue(migrated.is_draft)
            self.assertFalse(migrated.audience_configured)
            self.assertIsNotNone(migrated.updated_at)
            self.assertEqual(migrated.questions.get().options.get().pk, option.pk)
        finally:
            MigrationExecutor(connection).migrate([self.new])
