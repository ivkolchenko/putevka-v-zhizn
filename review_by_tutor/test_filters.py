from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import RegistrationPersonalData
from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo

from .services.staff_users import build_staff_users_queryset


class StaffFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(username='filter-staff', is_staff=True)
        cls.student = User.objects.create_user(username='filter-student')
        cls.other = User.objects.create_user(username='filter-other')
        cls.unfinished = User.objects.create_user(username='filter-unfinished')
        UserInfo.objects.create(user=cls.student, first_name='Анна', last_name='Иванова',
            middle_name='Петровна', email='anna@example.test', next_year_class_digit=11,
            internal_study_profile='it', status='SCHOLAR')
        UserInfo.objects.create(user=cls.other, next_year_class_digit=10, internal_study_profile='medic')
        for user in (cls.student, cls.other):
            RegistrationPersonalData.objects.create(user=user, email=user.username + '@example.test', current_step='finish')
        subject = Subject.objects.create(name='Предмет фильтров', slug='filters-subject')
        cls.school_a = School.objects.create(name='Школа A')
        cls.school_b = School.objects.create(name='Школа B')
        cls.course_a = Course.objects.create(school=cls.school_a, subject=subject, title='Курс A')
        cls.course_b = Course.objects.create(school=cls.school_b, subject=subject, title='Курс B')
        CourseSelection.objects.create(user=cls.student, course=cls.course_a, need_tutor=True)
        CourseSelection.objects.create(user=cls.student, course=cls.course_b, need_tutor=False)
        CourseSelection.objects.create(user=cls.other, course=cls.course_b, need_tutor=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def users(self, **filters):
        request = RequestFactory().get('/staff/users/', filters)
        return set(build_staff_users_queryset(request).values_list('pk', flat=True))

    def test_school_course_and_curator_must_match_one_enrollment(self):
        self.assertEqual(self.users(school=self.school_a.pk, course=self.course_b.pk), set())
        self.assertEqual(self.users(school=self.school_a.pk, curator_need='0'), set())
        self.assertEqual(self.users(course=self.course_a.pk, curator_need='0'), set())
        self.assertEqual(self.users(school=self.school_b.pk, course=self.course_b.pk, curator_need='0'), {self.student.pk})
        self.assertEqual(self.users(course=self.course_b.pk, curator_need='1'), {self.other.pk})

    def test_search_uses_profile_names_and_email_with_multiple_words(self):
        for query in ('Иванова Анна', 'Анна Петровна', 'anna@example.test'):
            self.assertEqual(self.users(q=query), {self.student.pk})
        self.assertEqual(self.users(q='Анна Несуществующий'), set())

    def test_removing_last_multivalue_filter_restores_users(self):
        self.assertEqual(self.users(grade_group=['10', '11']), {self.student.pk, self.other.pk})
        self.assertEqual(self.users(grade_group=['10']), {self.other.pk})
        self.assertEqual(self.users(grade_group=[]), {self.student.pk, self.other.pk})
        self.assertEqual(self.users(profile=['it']), {self.student.pk})
        self.assertEqual(self.users(profile=[]), {self.student.pk, self.other.pk})

    def test_clear_one_group_keeps_other_filters_and_resets_page(self):
        for field, values, remaining in [('profile', ['it', 'medic'], 'grade_group'),
                                          ('grade_group', ['10', '11'], 'profile')]:
            response = self.client.get(reverse('staff_users_list'), {
                field: values, remaining: ['11' if remaining == 'grade_group' else 'it'],
                'q': 'Иванова', 'page': 50, 'clear_filter': field,
            })
            self.assertEqual(response.status_code, 302)
            query = parse_qs(urlparse(response.url).query)
            self.assertNotIn(field, query)
            self.assertNotIn('page', query)
            self.assertNotIn('clear_filter', query)
            self.assertEqual(query['q'], ['Иванова'])
            self.assertIn(remaining, query)
            self.assertEqual(self.client.get(response.url).status_code, 200)

    def test_clear_registration_disables_default_constraint(self):
        response = self.client.get(reverse('staff_users_list'), {'clear_filter': 'registration_confirmed'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['registration_confirmed'], '0')
        self.assertIn(self.unfinished, response.context['page_obj'])

    def test_reset_link_removes_registration_constraint_too(self):
        response = self.client.get(reverse('staff_users_list'), {'profile': 'it'})
        self.assertContains(response, reverse('staff_users_list') + '?registration_confirmed=0')
        response = self.client.get(reverse('staff_users_list'), {'registration_confirmed': '0'})
        self.assertIn(self.unfinished, response.context['page_obj'])

    def test_invalid_ids_do_not_crash_or_expand_selection(self):
        for name in ('school', 'course'):
            for value in ('bad', '-1', '1.5', '9' * 30):
                with self.subTest(name=name, value=value):
                    response = self.client.get(reverse('staff_users_list'), {name: value})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertEqual(self.users(grade_group=['bad']), set())

    def test_filter_controls_work_with_empty_results_and_show_conflicting_course(self):
        response = self.client.get(reverse('staff_users_list'), {
            'school': self.school_a.pk, 'course': self.course_b.pk,
        })
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertIn(self.course_b, response.context['courses'])
        self.assertContains(response, 'Снять выбор классов')
        self.assertContains(response, 'Снять выбор профилей')
        self.assertContains(response, "bindLabel('gradeDropdownBtn'")
        self.assertContains(response, 'for="grade11"')
