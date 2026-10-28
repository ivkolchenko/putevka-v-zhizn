"""Готовый черновик со всеми типами вопросов, без назначения ученикам."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse

from surveys_builder.forms import QuestionsBuilderForm
from surveys_builder.models import QuestionBlock, QuestionsBuilder, SurveysBuilder


DEMO_TITLE = 'Тестовый опрос — все типы вопросов'
DEMO_BLOCKS_TITLE = 'Тестовый опрос с блоками — все типы вопросов'
DEMO_BLOCKS = [
    ('О вас', 'Короткое знакомство с участником опроса.', range(1, 2)),
    ('Учёба и интересы', 'Расскажите о занятиях, удобном формате и любимых предметах.', range(2, 5)),
    ('Планы и оценка', 'Укажите нагрузку, удобную дату начала и оцените занятия.', range(5, 8)),
]
DEMO_QUESTIONS = [
    {'title': 'Как вас зовут?', 'answer_type': 'short_text', 'required': True,
     'description': 'Короткий текст. Например: Анна Иванова.'},
    {'title': 'Что вам нравится в занятиях и что стоит улучшить?', 'answer_type': 'long_text', 'required': False,
     'description': 'Развёрнутый текст. Можно написать несколько абзацев; вопрос необязательный.'},
    {'title': 'В каком формате вам удобнее учиться?', 'answer_type': 'single_choice', 'required': True,
     'description': 'Выберите один вариант.', 'options_text': 'Онлайн\nОчно\nСмешанный формат'},
    {'title': 'Какие предметы вам интересны?', 'answer_type': 'multiple_choice', 'required': False,
     'description': 'Можно отметить несколько предметов и снять любую отметку.',
     'options_text': 'Математика\nРусский язык\nФизика\nХимия\nИнформатика'},
    {'title': 'Сколько часов в неделю вы готовы уделять занятиям?', 'answer_type': 'number', 'required': True,
     'description': 'Числовой ответ. Допускается дробное значение, например 7.5.'},
    {'title': 'Когда вам удобно начать занятия?', 'answer_type': 'date', 'required': False,
     'description': 'Выберите дату в календаре; поле можно оставить пустым.'},
    {'title': 'Насколько вы довольны занятиями?', 'answer_type': 'scale', 'required': True,
     'description': 'Шкала от 1 до 5: 1 — совсем не доволен, 5 — полностью доволен.'},
]


class Command(BaseCommand):
    help = 'Создать тестовый черновик опроса со всеми семью типами вопросов без рассылки.'

    def add_arguments(self, parser):
        parser.add_argument('--author-id', type=int, help='ID активного сотрудника — автора опроса.')
        parser.add_argument('--with-blocks', action='store_true', help='Создать отдельный пример с тремя блоками вопросов.')

    @transaction.atomic
    def handle(self, *args, **options):
        with_blocks = options.get('with_blocks', False)
        title = DEMO_BLOCKS_TITLE if with_blocks else DEMO_TITLE
        survey = SurveysBuilder.objects.filter(title=title).first()
        if survey:
            self.stdout.write(f'Тестовый опрос #{survey.pk} уже существует. Данные оставлены без изменений.')
        else:
            authors = get_user_model().objects.filter(is_active=True, is_staff=True).order_by('pk')
            author_id = options.get('author_id')
            if author_id is not None:
                authors = authors.filter(pk=author_id)
                if not authors.exists():
                    raise CommandError('Автор должен быть активным сотрудником.')
            survey = SurveysBuilder.objects.create(
                title=title, author=authors.first(),
                description='Демонстрационный опрос: все 7 типов вопросов, варианты выбора, '
                            'обязательные и необязательные ответы. Получатели не назначены. '
                            'Откройте предпросмотр, чтобы попробовать заполнение без сохранения и отправки.',
            )
            question_blocks = {}
            if with_blocks:
                for position, (block_title, description, question_positions) in enumerate(DEMO_BLOCKS, 1):
                    block = QuestionBlock.objects.create(survey=survey, title=block_title,
                        description=description, position=position)
                    question_blocks.update({index: block.pk for index in question_positions})
            for position, data in enumerate(DEMO_QUESTIONS, 1):
                values = {**data, 'block': question_blocks.get(position, '')}
                form = QuestionsBuilderForm(values, instance=QuestionsBuilder(survey=survey, position=position))
                if not form.is_valid():
                    raise CommandError(f'Некорректный демонстрационный вопрос {position}: {form.errors.as_text()}')
                question = form.save()
                form.save_options(question)
            self.stdout.write(self.style.SUCCESS(f'Создан тестовый опрос #{survey.pk}: {survey.title}.'))
        self.stdout.write('Предпросмотр: ' + reverse('surveys_builder:preview', args=[survey.pk]))
        self.stdout.write('Все опросы: ' + reverse('staff_surveys_list'))
