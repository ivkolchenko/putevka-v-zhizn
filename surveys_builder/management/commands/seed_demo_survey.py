"""Готовый черновик со всеми типами вопросов, без назначения ученикам."""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.urls import reverse

from surveys_builder.forms import BlockTextSectionForm, QuestionsBuilderForm
from surveys_builder.models import BlockTextSection, QuestionBlock, QuestionsBuilder, SurveysBuilder


DEMO_TITLE = 'Тестовый опрос — все типы вопросов'
DEMO_BLOCKS_TITLE = 'Тестовый опрос с блоками — все типы вопросов'
DEMO_IMAGES_TITLE = 'Тестовый опрос — все типы вопросов, блоки и картинки'
DEMO_ASSETS = Path(__file__).resolve().parents[2] / 'demo_assets'
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
        parser.add_argument('--with-images', action='store_true', help='Создать пример с четырьмя блоками, текстом и картинками.')

    @transaction.atomic
    def handle(self, *args, **options):
        with_images = options.get('with_images', False)
        with_blocks = options.get('with_blocks', False) or with_images
        title = DEMO_IMAGES_TITLE if with_images else DEMO_BLOCKS_TITLE if with_blocks else DEMO_TITLE
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
                            'Откройте предпросмотр, чтобы проверить содержимое перед назначением.',
            )
            question_blocks = {}
            if with_blocks:
                for position, (block_title, description, question_positions) in enumerate(DEMO_BLOCKS, 1):
                    block = QuestionBlock.objects.create(survey=survey, title=block_title,
                        description=description, position=position)
                    question_blocks.update({index: block.pk for index in question_positions})
            else:
                main = QuestionBlock.objects.create(survey=survey, title='Основной блок', position=1)
                question_blocks = {index: main.pk for index in range(1, len(DEMO_QUESTIONS) + 1)}
            counters = {}
            for position, data in enumerate(DEMO_QUESTIONS, 1):
                values = {**data, 'block': question_blocks.get(position, '')}
                block_id = question_blocks[position]
                counters[block_id] = counters.get(block_id, 0) + 1
                form = QuestionsBuilderForm(values, instance=QuestionsBuilder(survey=survey, position=position, block_position=counters[block_id]))
                if not form.is_valid():
                    raise CommandError(f'Некорректный демонстрационный вопрос {position}: {form.errors.as_text()}')
                question = form.save()
                form.save_options(question)
            if with_images:
                self._add_illustrated_content(survey)
            self.stdout.write(self.style.SUCCESS(f'Создан тестовый опрос #{survey.pk}: {survey.title}.'))
        self.stdout.write('Предпросмотр: ' + reverse('surveys_builder:preview', args=[survey.pk]))
        self.stdout.write('Все опросы: ' + reverse('staff_surveys_list'))

    def _add_illustrated_content(self, survey):
        blocks = list(survey.blocks.all())
        information = QuestionBlock.objects.create(survey=survey, title='Памятка участнику', position=4,
            description='Пример информационного блока: только текст и картинка, без вопросов.')
        sections = [
            (blocks[0], 1, 'Добро пожаловать!',
             'Это демонстрационный опрос со всеми доступными типами вопросов. '
             'Обязательные вопросы отмечены звёздочкой, остальные можно пропустить.',
             'study-desk.png', 'Уютное учебное место с ноутбуком, книгами и открытой тетрадью.'),
            (blocks[1], 2, 'Перейдём к предпочтениям',
             'В следующем вопросе выбирается один ответ, а в вопросе о предметах — несколько. '
             'Эта текстовая секция расположена между вопросами.', None, ''),
            (blocks[2], 1, 'Подсказка к заполнению',
             'Время занятий можно указать дробным числом. Дата необязательна. '
             'В конце оцените занятия по шкале от 1 до 5.', None, ''),
            (information, 1, 'Спасибо за участие!',
             'В этом блоке нет вопросов. Он показывает, как добавить памятку и изображение '
             'в конец опроса. После публикации ответы можно сохранить и отправить из кабинета ученика.', None, ''),
            (information, 2, '', '', 'study-plans.png',
             'Календарь и тетрадь на столе, за окном — дорога к университету.'),
        ]
        for block, position, title, text, filename, image_alt in sections:
            files = {}
            if filename:
                files['image'] = SimpleUploadedFile(filename, (DEMO_ASSETS / filename).read_bytes(), content_type='image/png')
            form = BlockTextSectionForm({'title': title, 'text': text, 'image_alt': image_alt}, files,
                instance=BlockTextSection(block=block, position=position))
            if not form.is_valid():
                raise CommandError(f'Некорректная демонстрационная секция: {form.errors.as_text()}')
            block.questions.filter(block_position__gte=position).update(block_position=F('block_position') + 1)
            block.text_sections.filter(position__gte=position).update(position=F('position') + 1)
            form.save()
