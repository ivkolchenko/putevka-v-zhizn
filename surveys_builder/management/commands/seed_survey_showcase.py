import secrets
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import RegistrationPersonalData
from scholar_form.models import UserInfo
from surveys_builder.forms import BlockTextSectionForm, QuestionsBuilderForm
from surveys_builder.models import BlockTextSection, QuestionBlock, QuestionsBuilder, SurveysBuilder
from surveys_builder.services import InvalidForm, publication_errors, publish_survey, publish_token, save_audience


ASSETS = Path(__file__).resolve().parents[2] / 'demo_assets'
SHOWCASE = [
    ('Учебный опыт', [
        ('Знакомство', 'Расскажите, как проходит ваша учёба.', 'study-desk.png', [
            ('Как к вам обращаться?', 'short_text', True, ()),
            ('В каком классе вы сейчас учитесь?', 'single_choice', True, ('9 класс', '10 класс', '11 класс')),
        ]),
        ('Впечатления', 'Поделитесь тем, что помогает вам заниматься.', None, [
            ('Что особенно помогает учиться?', 'long_text', False, ()),
            ('Насколько удобен текущий формат занятий?', 'scale', True, ()),
        ]),
    ]),
    ('Удобное расписание', [
        ('Текущий ритм', 'Вспомните обычную учебную неделю.', 'study-plans.png', [
            ('Сколько часов в неделю вы занимаетесь дополнительно?', 'number', True, ()),
            ('Какие дни вам удобны?', 'multiple_choice', True, ('Понедельник', 'Среда', 'Пятница', 'Суббота')),
        ]),
        ('Начало занятий', 'Выберите удобную дату и формат.', None, [
            ('Когда удобно начать новый курс?', 'date', False, ()),
            ('Где вам удобнее заниматься?', 'single_choice', True, ('Онлайн', 'Очно', 'Смешанно')),
        ]),
        ('Дополнения', 'Здесь можно оставить любые пожелания.', None, [
            ('Что стоит учесть при составлении расписания?', 'long_text', False, ()),
        ]),
    ]),
    ('Предметы и интересы', [
        ('Интересы', 'Отметьте интересующие вас направления.', 'study-desk.png', [
            ('Какие предметы вам интересны?', 'multiple_choice', True, ('Математика', 'Информатика', 'Физика', 'Литература')),
            ('Какой предмет нравится больше всего?', 'short_text', False, ()),
        ]),
        ('Цели', 'Расскажите, чего хотите достичь.', None, [
            ('Какая учебная цель для вас сейчас главная?', 'long_text', True, ()),
            ('Сколько занятий в месяц было бы удобно?', 'number', False, ()),
        ]),
        ('Формат', 'Представьте занятие, которое вам понравилось бы.', None, [
            ('Какой формат предпочитаете?', 'single_choice', True, ('Индивидуально', 'В группе', 'Самостоятельно')),
        ]),
        ('Итог', 'Оцените интерес к новым направлениям.', 'study-plans.png', [
            ('Насколько вам интересно попробовать новое направление?', 'scale', True, ()),
        ]),
    ]),
    ('Планы на будущее', [
        ('Ориентиры', 'Подумайте о следующем учебном году.', 'study-plans.png', [
            ('Когда вы планируете принять решение о дальнейшем обучении?', 'date', False, ()),
            ('Какие направления рассматриваете?', 'multiple_choice', True, ('Техническое', 'Медицинское', 'Гуманитарное', 'Творческое')),
        ]),
        ('Поддержка', 'Расскажите, какая помощь полезна.', None, [
            ('Какая поддержка нужна больше всего?', 'long_text', True, ()),
            ('С кем вам удобнее обсуждать планы?', 'single_choice', False, ('С наставником', 'С преподавателем', 'С семьёй')),
        ]),
        ('Подготовка', 'Оцените текущую нагрузку.', None, [
            ('Сколько часов в неделю вы готовы уделять подготовке?', 'number', True, ()),
            ('Насколько вы уверены в своём плане?', 'scale', True, ()),
        ]),
        ('Полезные материалы', 'Изучите памятку и отметьте полезные темы.', 'study-desk.png', [
            ('Какие материалы вам пригодятся?', 'multiple_choice', False, ('Календарь', 'Подборка курсов', 'Советы выпускников')),
        ]),
        ('Обратная связь', 'Добавьте любую мысль, которую мы не спросили.', None, [
            ('Что ещё важно знать о ваших планах?', 'short_text', False, ()),
        ]),
    ]),
    ('Обратная связь о программе', [
        ('Участие', 'Расскажите о своём опыте участия.', 'study-desk.png', [
            ('Как давно вы участвуете в программе?', 'single_choice', True, ('Менее месяца', '1–6 месяцев', 'Более полугода')),
            ('Какие форматы уже пробовали?', 'multiple_choice', False, ('Курсы', 'Наставничество', 'Встречи')),
        ]),
        ('Оценка', 'Оцените впечатления и поделитесь идеями.', None, [
            ('Насколько полезной была программа?', 'scale', True, ()),
            ('Что стоит улучшить?', 'long_text', False, ()),
        ]),
        ('Следующие шаги', 'Подумайте, что было бы полезно дальше.', 'study-plans.png', [
            ('Какую тему обсудить на следующей встрече?', 'short_text', False, ()),
            ('Сколько встреч в месяц вам подходит?', 'number', False, ()),
        ]),
    ]),
]


class Command(BaseCommand):
    help = 'Создать локальные тестовые аккаунты и пять опубликованных опросов для ученика.'

    @transaction.atomic
    def handle(self, *args, **options):
        user_model = get_user_model()
        admin_password = secrets.token_urlsafe(15)
        student_password = secrets.token_urlsafe(15)
        admin, _ = user_model.objects.get_or_create(username='survey_demo_admin')
        admin.email = 'survey_demo_admin@example.test'
        admin.is_active = True
        admin.is_staff = True
        admin.is_superuser = True
        admin.set_password(admin_password)
        admin.save()
        student = user_model.objects.get(username='survey_demo_student')
        if student.is_staff or student.is_superuser:
            raise CommandError('Тестовая учётная запись ученика имеет права сотрудника.')
        student.is_active = True
        student.set_password(student_password)
        student.save(update_fields=['is_active', 'password'])
        if not RegistrationPersonalData.objects.filter(user=student, current_step='finish').exists():
            raise CommandError('У тестового ученика не завершена регистрация.')
        if not UserInfo.objects.filter(user=student).exists():
            raise CommandError('У тестового ученика нет профиля.')
        created = []
        for name, blocks in SHOWCASE:
            title = f'Демо · {name}'
            if SurveysBuilder.objects.filter(title=title).exists():
                raise CommandError(f'Опрос «{title}» уже существует; данные не изменены.')
            survey = SurveysBuilder.objects.create(
                title=title,
                description='Тестовый опрос для проверки блоков, текстовых секций, иллюстраций и разных типов ответов.',
                author=admin,
            )
            question_position = 0
            for block_position, (block_title, intro, image_name, questions) in enumerate(blocks, 1):
                block = QuestionBlock.objects.create(survey=survey, title=block_title, position=block_position)
                image = {}
                if image_name:
                    image['image'] = SimpleUploadedFile(
                        image_name, (ASSETS / image_name).read_bytes(), content_type='image/png'
                    )
                section_form = BlockTextSectionForm(
                    {'title': block_title, 'text': intro,
                     'image_alt': f'Иллюстрация к блоку «{block_title}».' if image_name else ''},
                    image,
                    instance=BlockTextSection(block=block, position=1),
                )
                if not section_form.is_valid():
                    raise CommandError(section_form.errors.as_text())
                section_form.save()
                for item_position, (question_title, answer_type, required, options) in enumerate(questions, 2):
                    question_position += 1
                    form = QuestionsBuilderForm(
                        {'block': block.pk, 'title': question_title, 'answer_type': answer_type,
                         'required': required, 'options_text': '\n'.join(options)},
                        instance=QuestionsBuilder(
                            survey=survey, position=question_position, block_position=item_position
                        ),
                    )
                    if not form.is_valid():
                        raise CommandError(form.errors.as_text())
                    question = form.save()
                    form.save_options(question)
            try:
                save_audience(survey.pk, admin, {'mode': 'individual', 'included_users': [student.pk]}, survey.revision)
            except InvalidForm as error:
                raise CommandError(error.form.errors.as_text()) from error
            survey.refresh_from_db()
            errors = publication_errors(survey)
            if errors:
                raise CommandError('; '.join(errors))
            publish_survey(survey.pk, admin, publish_token(survey, admin, [student.pk]))
            created.append((survey.pk, len(blocks)))
        self.stdout.write(self.style.SUCCESS(f'Создано и назначено опросов: {len(created)}'))
        for pk, block_count in created:
            self.stdout.write(f'Опрос #{pk}: блоков {block_count}')
        self.stdout.write(f'Администратор: survey_demo_admin / {admin_password}')
        self.stdout.write(f'Ученик: survey_demo_student / {student_password}')
