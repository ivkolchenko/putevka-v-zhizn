from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.models import RegistrationPersonalData
from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo
from surveys_builder.selectors import filter_students, student_queryset


class DemoStudentsTests(TestCase):
    def test_students_cover_profiles_grades_and_educational_filters(self):
        call_command('seed_demo_students', password='test-only-password', stdout=StringIO())
        students = student_queryset()
        self.assertEqual(students.count(), 16)
        for profile in UserInfo.InternalStudyProfile.values:
            for grade in (10, 11):
                users = filter_students(students, {'profiles': [profile], 'grades': [grade]})
                self.assertEqual(users.count(), 1)
                user = users.get()
                course = user.course_selections.first().course
                filtered = filter_students(users, {'subjects': [course.subject_id],
                    'schools': [course.school_id], 'courses': [course.pk],
                    'planned_exams': list(user.user_info.planned_exams.values_list('pk', flat=True))})
                self.assertEqual(list(filtered), [user])
                self.assertTrue(user.check_password('test-only-password'))
                self.assertFalse(user.is_staff or user.is_superuser)
                self.assertTrue(user.registrationpersonaldata.email_verified)

    def test_repeat_does_not_duplicate_data_or_overwrite_existing_accounts(self):
        call_command('seed_demo_students', password='original-password', stdout=StringIO())
        user = get_user_model().objects.get(username='demo_student_01')
        user.first_name = 'Изменено вручную'
        user.is_active = False
        user.save(update_fields=['first_name', 'is_active'])
        info = user.user_info
        info.internal_study_profile = 'other'
        info.save(update_fields=['internal_study_profile'])
        info.planned_exams.clear()
        models = (get_user_model(), UserInfo, RegistrationPersonalData, Subject, School, Course, CourseSelection)
        counts = [model.objects.count() for model in models]
        call_command('seed_demo_students', password='replacement-password', stdout=StringIO())
        self.assertEqual([model.objects.count() for model in models], counts)
        user.refresh_from_db()
        info.refresh_from_db()
        self.assertEqual(user.first_name, 'Изменено вручную')
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password('original-password'))
        self.assertEqual(info.internal_study_profile, 'other')
        self.assertFalse(info.planned_exams.exists())
