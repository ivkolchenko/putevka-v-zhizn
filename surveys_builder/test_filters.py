from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import RegistrationPersonalData
from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo
from . import services
from .forms import AudienceForm
from .models import QuestionBlock


class AudienceFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='audience-staff', is_staff=True)
        cls.student = User.objects.create_user(username='audience-student')
        cls.other = User.objects.create_user(username='audience-other')
        cls.inactive = User.objects.create_user(username='audience-inactive', is_active=False)
        for user, grade in ((cls.student, 11), (cls.other, 10), (cls.inactive, 11)):
            UserInfo.objects.create(user=user, next_year_class_digit=grade, internal_study_profile='it')
            RegistrationPersonalData.objects.create(user=user, email=user.username+'@example.test', current_step='finish')
        subject = Subject.objects.create(name='Предмет', slug='audience-filters')
        school = School.objects.create(name='Школа')
        cls.course = Course.objects.create(school=school, subject=subject, title='Курс')
        for user in (cls.student, cls.other):
            CourseSelection.objects.create(user=user, course=cls.course)

    def setUp(self):
        self.client.force_login(self.staff)
        self.survey = services.create_survey(self.staff, {'title': 'Фильтры'})
        QuestionBlock.objects.create(survey=self.survey, title='Блок')
        self.survey = services.save_audience(self.survey.pk, self.staff,
            {'mode': 'filters', 'grades': ['11'], 'profiles': ['it'], 'courses': [self.course.pk]}, self.survey.revision)
        self.url = reverse('surveys_builder:audience', args=[self.survey.pk])

    def test_only_added_filters_are_open_and_selected_values_are_retained(self):
        form = self.client.get(self.url).context['form']
        self.assertEqual({group['name'] for group in form.filter_groups if group['active']}, {'grades', 'profiles', 'courses'})
        self.assertEqual(form['grades'].value(), ['11'])
        self.assertNotIn('account_active', form.fields)

    def test_removing_filter_restores_eligible_recipients_and_can_be_saved(self):
        response = self.client.post(self.url, {'revision': self.survey.revision, 'mode': 'filters',
            'profiles': ['it'], 'courses': [self.course.pk], 'action': 'save'})
        self.assertRedirects(response, reverse('surveys_builder:preview', args=[self.survey.pk]))
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.audience_filters['grades'], [])
        self.assertEqual(set(services.audience_for(self.survey)), {self.student, self.other})

    def test_individual_mode_ignores_obsolete_filter_choices(self):
        form = AudienceForm({'mode': 'individual', 'included_users': [self.other.pk], 'grades': ['9'], 'courses': [999999]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['grades'], [])
        self.assertFalse(AudienceForm({'mode': 'individual', 'included_users': []}).is_valid())

    def test_preview_does_not_save_and_reset_cannot_include_inactive(self):
        response = self.client.post(self.url, {'revision': self.survey.revision, 'mode': 'filters', 'grades': ['10'], 'action': 'preview'})
        self.assertEqual(response.context['recipient_count'], 1)
        self.assertEqual(list(services.audience_for(self.survey)), [self.student])
        response = self.client.post(self.url, {'revision': self.survey.revision, 'action': 'reset'})
        self.assertEqual(response.context['recipient_count'], 2)
        self.assertEqual(list(services.audience_for(self.survey)), [self.student])

    def test_invalid_clear_and_stale_version_do_not_change_settings(self):
        self.assertEqual(self.client.post(self.url, {'revision': self.survey.revision, 'clear_field': 'mode'}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'revision': 0, 'mode': 'filters', 'action': 'save'}).status_code, 400)
