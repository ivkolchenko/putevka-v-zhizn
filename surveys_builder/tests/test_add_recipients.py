import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Notification, RegistrationPersonalData, UserNotification
from scholar_form.models import UserInfo
from .. import services
from ..models import BlockTextSection, QuestionBlock, QuestionOptions, QuestionsBuilder, SurveyAssignment, SurveyResponse, SurveysBuilder
from ..selectors import assignment_questions


class AdditionalRecipientsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='add-staff', is_staff=True)
        cls.existing = User.objects.create_user(username='add-existing')
        cls.it = User.objects.create_user(username='add-it')
        cls.medic = User.objects.create_user(username='add-medic')
        cls.inactive = User.objects.create_user(username='add-inactive', is_active=False)
        cls.unfinished = User.objects.create_user(username='add-unfinished')
        cls.superuser = User.objects.create_user(username='add-superuser', is_superuser=True)
        for user in (cls.staff, cls.existing, cls.it, cls.medic, cls.inactive, cls.superuser):
            RegistrationPersonalData.objects.create(user=user, email=user.username+'@example.test', current_step='finish')
        for user in (cls.existing, cls.it, cls.medic, cls.inactive, cls.unfinished):
            UserInfo.objects.create(user=user, internal_study_profile='medic' if user == cls.medic else 'it',
                                    next_year_class_digit=10 if user == cls.medic else 11)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Доназначение'})
        self.common = self.block('Общий блок')
        self.question = services.save_question(self.survey.pk, self.staff,
            {'block': self.common.pk, 'title': 'Общий вопрос', 'answer_type': 'short_text', 'required': True}, self.version())
        self.it_block = self.block('IT', {'profiles': ['it']})
        self.medic_block = self.block('Медицина', {'profiles': ['medic']})
        services.save_audience(self.survey.pk, self.staff,
            {'mode': 'individual', 'included_users': [self.existing.pk]}, self.version())
        self.version()
        services.publish_survey(self.survey.pk, self.staff,
            services.publish_token(self.survey, self.staff, [self.existing.pk]))
        self.assignment = self.survey.assignments.get()
        self.response = services.save_response(self.assignment.pk, self.existing,
            {f'q_{self.question.pk}': 'Сохранённый ответ'}, final=True, revision=0)
        self.url = reverse('surveys_builder:additional_recipients', args=[self.survey.pk])
        self.api_url = reverse('surveys_builder:surveys_api:additional_recipients', args=[self.survey.pk])

    def version(self):
        self.survey.refresh_from_db()
        return self.survey.revision

    def block(self, title, filters=None):
        block = services.save_block(self.survey.pk, self.staff,
            {'title': title, 'visibility_filters': filters or {}}, self.version())
        services.save_text_section(self.survey.pk, block.pk, self.staff, {'text': 'Инструкция '+title}, {}, self.version())
        return block

    def add(self, *users):
        return services.add_recipients(self.survey.pk, self.staff,
            {'mode': 'individual', 'included_users': [user.pk for user in users]}, self.version())

    def test_manual_addition_preserves_existing_assignments_answers_deadline_and_selection(self):
        deadline = timezone.now() + timedelta(days=2)
        SurveysBuilder.objects.filter(pk=self.survey.pk).update(deadline=deadline)
        old = SurveyAssignment.objects.values().get(pk=self.assignment.pk)
        UserInfo.objects.filter(user=self.existing).update(internal_study_profile='medic')
        before_answers = list(self.response.answers.values())
        old_filters = self.survey.audience_filters.copy()
        survey, count = self.add(self.it, self.medic)
        self.assertEqual(count, 2)
        self.assertEqual(survey.status, 'published')
        self.assertEqual(survey.deadline, deadline)
        self.assertEqual(survey.audience_filters, old_filters)
        self.assertEqual(list(survey.included_users.all()), [self.existing])
        self.assertEqual(SurveyAssignment.objects.values().get(pk=self.assignment.pk), old)
        self.response.refresh_from_db()
        self.assertEqual(self.response.status, 'submitted')
        self.assertEqual(list(self.response.answers.values()), before_answers)
        self.assertEqual(survey.assignments.get(user=self.it).visible_block_ids, [self.common.pk, self.it_block.pk])
        self.assertEqual(survey.assignments.get(user=self.medic).visible_block_ids, [self.common.pk, self.medic_block.pk])
        self.assertEqual(survey.assignments.get(user=self.it).recipient_snapshot['profile'], 'it')
        self.assertFalse(SurveyResponse.objects.filter(assignment__user__in=[self.it, self.medic]).exists())

    def test_filter_addition_excludes_existing_and_only_notifies_new_recipients(self):
        services.add_recipients(self.survey.pk, self.staff, {'mode': 'filters', 'grades': ['11']}, self.version())
        self.assertEqual(set(self.survey.assignments.values_list('user_id', flat=True)), {self.existing.pk, self.it.pk})
        self.assertEqual(UserNotification.objects.filter(recipient=self.existing).count(), 1)
        self.assertEqual(UserNotification.objects.filter(recipient=self.it).count(), 1)
        self.assertEqual(UserNotification.objects.filter(recipient=self.medic).count(), 0)
        with self.assertRaises(services.InvalidForm):
            services.add_recipients(self.survey.pk, self.staff, {'mode': 'filters', 'grades': ['11']}, self.version())
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(self.survey.assignments.count(), 2)

    def test_invalid_and_ineligible_selection_is_atomic(self):
        for user in (self.staff, self.superuser, self.inactive, self.unfinished, self.existing):
            with self.subTest(user=user.username), self.assertRaises(services.InvalidForm):
                self.add(self.it, user)
        self.assertEqual(self.survey.assignments.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)

    def test_no_visible_blocks_blocks_entire_batch(self):
        self.survey.blocks.all().update(visibility_filters={'profiles': ['it']})
        with self.assertRaises(services.InvalidForm):
            self.add(self.it, self.medic)
        self.assertEqual(self.survey.assignments.count(), 1)
        self.assertEqual(UserNotification.objects.count(), 1)

    def test_states_and_expired_deadline_reject_addition(self):
        for status in ('draft', 'closed', 'archived'):
            with self.subTest(status=status):
                SurveysBuilder.objects.filter(pk=self.survey.pk).update(status=status)
                with self.assertRaises(ValidationError):
                    self.add(self.it)
                self.assertEqual(self.client.get(self.url).status_code, 400)
        SurveysBuilder.objects.filter(pk=self.survey.pk).update(status='published', deadline=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            self.add(self.it)
        self.assertEqual(self.survey.assignments.count(), 1)

    def test_stale_revision_and_notification_failure_roll_back_new_assignments(self):
        with self.assertRaises(services.RevisionConflict):
            services.add_recipients(self.survey.pk, self.staff, {'mode': 'filters'}, 0)
        version = self.version()
        with patch.object(UserNotification.objects, 'bulk_create', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.add(self.it)
        self.assertEqual(self.version(), version)
        self.assertEqual(self.survey.assignments.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)

    def test_editor_lists_only_new_eligible_students_and_preview_does_not_assign(self):
        page = self.client.get(self.url)
        self.assertEqual(set(page.context['form'].fields['included_users'].queryset), {self.it, self.medic})
        self.assertNotIn('deadline', page.context['form'].fields)
        self.assertContains(self.client.get(reverse('staff_surveys_list')), self.url)
        self.assertContains(self.client.get(reverse('surveys_builder:results', args=[self.survey.pk])), self.url)
        preview = self.client.post(self.url, {'revision': self.version(), 'mode': 'filters', 'grades': ['10'], 'action': 'preview'})
        self.assertEqual(preview.context['recipient_count'], 1)
        self.assertEqual(self.survey.assignments.count(), 1)
        added = self.client.post(self.url, {'revision': self.version(), 'mode': 'individual',
            'included_users': [self.medic.pk], 'action': 'add'})
        self.assertRedirects(added, reverse('surveys_builder:results', args=[self.survey.pk]))
        self.assertEqual(self.survey.assignments.count(), 2)

    def test_api_adds_students_rejects_bad_types_and_stale_requests(self):
        response = self.client.post(self.api_url, json.dumps({'revision': self.version(),
            'included_users': [self.it.pk]}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['added_count'], 1)
        self.assertEqual(response.json()['recipient_count'], 2)
        for data, status in [({'revision': 0, 'mode': 'filters'}, 409),
                             ({'revision': self.version(), 'included_users': '1'}, 400),
                             ({'revision': self.version(), 'deadline': None}, 400)]:
            response = self.client.post(self.api_url, json.dumps(data), content_type='application/json')
            self.assertEqual(response.status_code, status, response.content)

    def test_added_student_can_read_answer_and_receives_notification(self):
        self.add(self.medic)
        assignment = self.survey.assignments.get(user=self.medic)
        self.client.force_login(self.medic)
        self.assertContains(self.client.get(reverse('student_surveys:list')), self.survey.title)
        answer_url = reverse('student_surveys:answer', args=[assignment.pk])
        self.assertContains(self.client.get(answer_url), self.question.title)
        self.assertNotContains(self.client.get(answer_url), self.it_block.title)
        self.assertTrue(UserNotification.objects.filter(recipient=self.medic, is_seen=False).exists())
        response = self.client.post(answer_url, {'action': 'next', 'response_revision': 0, f'q_{self.question.pk}': 'Новый ответ'})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(answer_url+'?page=2', {'action': 'submit', 'response_revision': assignment.response.revision})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SurveyResponse.objects.get(assignment=assignment).status, 'submitted')

    def test_permissions_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.url, {'revision': self.version(), 'mode': 'filters'}).status_code, 403)
        self.assertEqual(client.post(self.api_url, json.dumps({'revision': self.version(), 'mode': 'filters'}),
                                     content_type='application/json').status_code, 403)
        self.client.force_login(self.it)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.api_url, '{}', content_type='application/json').status_code, 403)
        with self.assertRaises(PermissionDenied):
            services.add_recipients(self.survey.pk, self.it, {'mode': 'filters'}, self.version())

    def test_no_available_students_shows_explanation_instead_of_empty_form(self):
        self.add(self.it, self.medic)
        page = self.client.get(self.url)
        self.assertEqual(page.context['available_count'], 0)
        self.assertContains(page, 'Опрос уже назначен всем доступным участникам')
        self.assertNotContains(page, 'data-assignment-form')

    def test_addition_freezes_question_visibility_for_new_students(self):
        self.question.visibility_filters = {'profiles': ['it']}
        self.question.save(update_fields=['visibility_filters'])
        self.add(self.it, self.medic)
        it_assignment = self.survey.assignments.get(user=self.it)
        medic_assignment = self.survey.assignments.get(user=self.medic)
        self.assertEqual(it_assignment.visible_question_ids, [self.question.pk])
        self.assertEqual(medic_assignment.visible_question_ids, [])
        UserInfo.objects.filter(user=self.medic).update(internal_study_profile='it')
        self.assertFalse(assignment_questions(medic_assignment).exists())
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.visible_question_ids, [self.question.pk])

    def test_invalid_question_visibility_returns_selection_form_without_assigning(self):
        self.question.visibility_filters = {'grades': ['9']}
        self.question.save(update_fields=['visibility_filters'])
        page = self.client.post(self.url, {'revision': self.version(), 'mode': 'individual',
            'included_users': [self.it.pk], 'action': 'preview'})
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['form'].non_field_errors())
        self.assertIn('included_users', page.context['form'].fields)
        self.assertEqual(self.survey.assignments.count(), 1)

    def test_addition_removes_conditional_blocks_when_parent_question_is_unavailable(self):
        parent = QuestionsBuilder.objects.create(survey=self.survey, block=self.common,
            title='Выбор для IT', answer_type='single_choice', visibility_filters={'profiles': ['it']},
            position=2, block_position=3)
        option = QuestionOptions.objects.create(question=parent, text='Показать подробности')
        branch = QuestionBlock.objects.create(survey=self.survey, title='Условный блок', position=4, show_if_option=option)
        BlockTextSection.objects.create(block=branch, text='Подробности для IT')
        self.add(self.it, self.medic)
        it_assignment = self.survey.assignments.get(user=self.it)
        medic_assignment = self.survey.assignments.get(user=self.medic)
        self.assertIn(branch.pk, it_assignment.visible_block_ids)
        self.assertIn(parent.pk, it_assignment.visible_question_ids)
        self.assertNotIn(branch.pk, medic_assignment.visible_block_ids)
        self.assertNotIn(parent.pk, medic_assignment.visible_question_ids)

