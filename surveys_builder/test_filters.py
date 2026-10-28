import json

from django import forms
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo

from . import services
from .forms import AudienceForm
from .selectors import resolve_audience


class AudienceFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='audience-staff', is_staff=True)
        cls.student = User.objects.create_user(username='audience-student')
        cls.other = User.objects.create_user(username='audience-other')
        cls.inactive = User.objects.create_user(username='audience-inactive', is_active=False)
        for user, grade in [(cls.student, 11), (cls.other, 10)]:
            UserInfo.objects.create(user=user, next_year_class_digit=grade, internal_study_profile='it')
        subject = Subject.objects.create(name='Предмет', slug='audience-filters')
        school = School.objects.create(name='Школа')
        cls.course = Course.objects.create(school=school, subject=subject, title='Курс')
        for user in (cls.student, cls.other):
            CourseSelection.objects.create(user=user, course=cls.course)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Фильтры'})
        self.survey = services.save_audience(self.survey.pk, self.staff, self.data(), self.survey.revision)

    def data(self, **changes):
        return {'mode': 'filters', 'grades': ['11'], 'profiles': ['it'],
                'courses': [self.course.pk], 'account_active': 'yes', **changes}

    def post(self, **data):
        return self.client.post(reverse('surveys_builder:audience', args=[self.survey.pk]),
            {'revision': self.survey.revision, **self.data(), **data})

    def test_multiselects_render_as_individually_toggleable_checkboxes(self):
        response = self.client.get(reverse('surveys_builder:audience', args=[self.survey.pk]))
        for name in ('grades', 'profiles', 'courses', 'included_users', 'excluded_users'):
            self.assertIsInstance(response.context['form'].fields[name].widget, forms.CheckboxSelectMultiple)
        self.assertContains(response, 'name="clear_field" value="grades"')
        self.assertContains(response, 'Сбросить все фильтры и списки')

    def test_clear_group_preserves_other_values_and_does_not_save(self):
        response = self.post(clear_field='grades')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['recipient_count'], 2)
        self.assertEqual(response.context['form'].cleaned_data['grades'], [])
        self.assertEqual(response.context['form'].cleaned_data['profiles'], ['it'])
        self.assertEqual(list(response.context['form'].cleaned_data['courses']), [self.course])
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.revision, 2)
        self.assertEqual(list(services.audience_for(self.survey)), [self.student])

    def test_cleared_group_can_be_saved_without_reappearing(self):
        response = self.post(action='save', grades=[])
        self.assertEqual(response.status_code, 302)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.audience_filters['grades'], [])
        self.assertEqual(set(services.audience_for(self.survey)), {self.student, self.other})
        response = self.client.get(reverse('surveys_builder:audience', args=[self.survey.pk]))
        self.assertEqual(response.context['form']['grades'].value(), [])

    def test_reset_clears_all_filters_inclusions_exclusions_and_activity(self):
        response = self.post(action='reset', mode='individual', included_users=[self.student.pk], excluded_users=[self.other.pk])
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['mode'], 'filters')
        self.assertEqual(form.cleaned_data['account_active'], '')
        self.assertFalse(form.cleaned_data['included_users'].exists())
        self.assertFalse(form.cleaned_data['excluded_users'].exists())
        self.assertEqual(response.context['recipient_count'], 3)
        self.assertEqual(list(services.audience_for(self.survey)), [self.student])

    def test_single_value_can_be_removed_without_resetting_others(self):
        response = self.post(action='preview', grades=['10'])
        self.assertEqual(response.context['recipient_count'], 1)
        self.assertEqual(response.context['recipients'][0][0], self.other.pk)

    def test_individual_selection_ignores_invalid_or_deleted_filter_choices(self):
        form = AudienceForm(self.data(mode='individual', included_users=[self.other.pk],
            grades=['99'], courses=[999999], profiles=['deleted']))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(list(resolve_audience(form.cleaned_data)), [self.other])
        response = self.post(action='save', mode='individual', included_users=[self.other.pk], courses=[99999])
        self.assertEqual(response.status_code, 302)

    def test_empty_individual_selection_remains_invalid(self):
        response = self.post(clear_field='included_users', mode='individual', included_users=[self.other.pk])
        self.assertIn('included_users', response.context['form'].errors)
        self.assertEqual(list(services.audience_for(self.survey)), [self.student])

    def test_invalid_clear_field_and_stale_version_do_not_change_settings(self):
        self.assertEqual(self.post(clear_field='mode').status_code, 400)
        self.assertEqual(self.post(clear_field='grades', revision=1).status_code, 400)
        self.assertEqual(list(services.audience_for(self.survey)), [self.student])

    def test_api_empty_arrays_and_strings_remove_filters(self):
        url = reverse('surveys_builder:surveys_api:audience', args=[self.survey.pk])
        data = self.data(revision=self.survey.revision, grades=[], profiles=[], courses=[],
                         account_active='', included_users=[], excluded_users=[])
        response = self.client.put(url, data=json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['recipients']['count'], 3)
        self.survey.refresh_from_db()
        self.assertEqual(set(services.audience_for(self.survey)), {self.student, self.other, self.inactive})
