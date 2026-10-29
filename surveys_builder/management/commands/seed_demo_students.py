"""Демонстрационные получатели для фильтров опросов и видимости блоков."""
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import RegistrationPersonalData
from my_study.models import Course, CourseSelection, School, Subject
from scholar_form.models import UserInfo


SUBJECTS = {
    'math': 'Математика', 'russian': 'Русский язык', 'informatics': 'Информатика',
    'physics': 'Физика', 'chemistry': 'Химия', 'biology': 'Биология',
    'geography': 'География', 'literature': 'Литература',
}
PROFILE_SUBJECTS = {
    'it': ('math', 'informatics'), 'phys_math': ('math', 'physics'),
    'medic': ('chemistry', 'biology'), 'chem_bio': ('biology', 'chemistry'),
    'geo_eco': ('geography', 'biology'), 'arch_design': ('math', 'literature'),
    'humanities': ('russian', 'literature'), 'other': ('math', 'russian'),
}
NAMES = [
    ('Анна', 'Иванова'), ('Иван', 'Петров'), ('Мария', 'Соколова'), ('Артём', 'Смирнов'),
    ('Софья', 'Кузнецова'), ('Михаил', 'Волков'), ('Алиса', 'Морозова'), ('Даниил', 'Попов'),
    ('Полина', 'Лебедева'), ('Максим', 'Новиков'), ('Елизавета', 'Орлова'), ('Кирилл', 'Фёдоров'),
    ('Варвара', 'Михайлова'), ('Александр', 'Васильев'), ('Виктория', 'Павлова'), ('Дмитрий', 'Зайцев'),
]


class Command(BaseCommand):
    help = 'Добавить 16 тестовых учеников для назначения опросов, без рассылок и изменения существующих аккаунтов.'

    def add_arguments(self, parser):
        parser.add_argument('--password', help='Пароль новых учеников. По умолчанию генерируется случайный.')

    @transaction.atomic
    def handle(self, *args, **options):
        password = options.get('password') or secrets.token_urlsafe(12)
        User = get_user_model()
        created_count = 0
        subjects = {}
        schools = []
        for index, (profile, label) in enumerate(UserInfo.InternalStudyProfile.choices):
            for grade in (10, 11):
                number = index * 2 + grade - 9
                username = f'demo_student_{number:02d}'
                # Не меняем пароль, права, профиль и учебные данные существующего пользователя.
                if User.objects.filter(username=username).exists():
                    self.stdout.write(f'{username}: уже существует, оставлен без изменений.')
                    continue
                if not subjects:
                    for key, name in SUBJECTS.items():
                        subjects[key], _ = Subject.objects.get_or_create(
                            name=name, defaults={'slug': f'survey-demo-{key}'})
                    for name in ('Тестовая школа — Точные науки', 'Тестовая школа — Естественные науки',
                                 'Тестовая школа — Гуманитарные науки'):
                        school, _ = School.objects.get_or_create(name=name,
                            defaults={'description': 'Демонстрационные курсы для проверки назначения опросов.'})
                        schools.append(school)
                first_name, last_name = NAMES[number - 1]
                email = f'{username}@example.test'
                user = User.objects.create_user(username=username, email=email, password=password,
                    first_name=first_name, last_name=last_name, is_active=True, is_staff=False, is_superuser=False)
                RegistrationPersonalData.objects.create(user=user, email=email, password=user.password,
                    email_verified=True, current_step='finish')
                info = UserInfo.objects.create(user=user, first_name=first_name,
                    last_name=f'{last_name} (тест)', email=email, next_year_class_digit=grade,
                    internal_study_profile=profile, class_profile=label, school_name='Тестовая общеобразовательная школа',
                    status=UserInfo.STATUSES[(number - 1) % len(UserInfo.STATUSES)][0],
                    selection_step=UserInfo.SelectionStep.values[(number - 1) % len(UserInfo.SelectionStep)],
                    form_status=UserInfo.FormStatus.APPROVED)
                chosen_subjects = PROFILE_SUBJECTS[profile]
                info.planned_exams.set({subjects[key] for key in (*chosen_subjects, 'russian')})
                school = schools[0 if index < 2 else 1 if index < 5 else 2]
                for key in chosen_subjects:
                    course, _ = Course.objects.get_or_create(school=school,
                        title=f'Тестовый курс: {SUBJECTS[key]}, {grade} класс',
                        defaults={'subject': subjects[key]})
                    CourseSelection.objects.create(user=user, course=course,
                        motivation='Тестовые данные для проверки фильтров опросов.')
                created_count += 1
                self.stdout.write(f'{username}: {first_name} {last_name} (тест), {grade} класс, {label}.')
        self.stdout.write(self.style.SUCCESS(f'Добавлено учеников: {created_count}.'))
        if created_count:
            self.stdout.write(f'Пароль новых тестовых учеников: {password}')
