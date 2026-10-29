from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .storage import PrivateSurveyImageStorage


class SurveysBuilder(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        PUBLISHED = 'published', 'Опубликован'
        CLOSED = 'closed', 'Закрыт'
        ARCHIVED = 'archived', 'В архиве'

    class AudienceMode(models.TextChoices):
        FILTERS = 'filters', 'По фильтрам'
        INDIVIDUAL = 'individual', 'Конкретные ученики'

    title = models.CharField('Название', max_length=200)
    description = models.TextField('Описание', blank=True, max_length=10000)
    created_at = models.DateTimeField('Дата создания', auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='authored_surveys')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    deadline = models.DateTimeField('Принимать ответы до', null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    audience_mode = models.CharField(max_length=20, choices=AudienceMode.choices, default=AudienceMode.FILTERS)
    audience_filters = models.JSONField(default=dict, blank=True)
    audience_configured = models.BooleanField(default=False)
    included_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='included_in_surveys')
    excluded_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='excluded_from_surveys')
    revision = models.PositiveIntegerField(default=1)
    is_block_template = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return self.title

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def accepts_answers(self):
        return self.status == self.Status.PUBLISHED and (self.deadline is None or self.deadline > timezone.now())


class QuestionBlock(models.Model):
    survey = models.ForeignKey(SurveysBuilder, on_delete=models.CASCADE, related_name='blocks')
    title = models.CharField('Название блока', max_length=200)
    description = models.TextField('Описание блока', blank=True, max_length=3000)
    position = models.PositiveIntegerField('Порядок', default=1)
    visibility_filters = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['position', 'pk']

    def __str__(self):
        return self.title


class BlockTextSection(models.Model):
    block = models.ForeignKey(QuestionBlock, on_delete=models.CASCADE, related_name='text_sections')
    title = models.CharField('Заголовок секции', max_length=200, blank=True)
    text = models.TextField('Текст', max_length=20000, blank=True)
    image = models.ImageField('Картинка', upload_to='surveys/sections/%Y/%m/', blank=True,
                              storage=PrivateSurveyImageStorage())
    image_alt = models.CharField('Описание картинки', max_length=500, blank=True)
    position = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['position', 'pk']


class QuestionsBuilder(models.Model):
    class AnswerType(models.TextChoices):
        SHORT_TEXT = 'short_text', 'Короткий текст'
        LONG_TEXT = 'long_text', 'Развернутый текст'
        SINGLE_CHOICE = 'single_choice', 'Один вариант ответа'
        MULTIPLE_CHOICE = 'multiple_choice', 'Несколько вариантов ответа'
        NUMBER = 'number', 'Число'
        DATE = 'date', 'Дата'
        SCALE = 'scale', 'Шкала от 1 до 5'

    survey = models.ForeignKey(SurveysBuilder, on_delete=models.CASCADE, related_name='questions', verbose_name='Опросник')
    block = models.ForeignKey(QuestionBlock, on_delete=models.CASCADE,
                              related_name='questions', verbose_name='Блок')
    title = models.CharField('Текст вопроса', max_length=500)
    description = models.CharField('Подсказка', max_length=1000, blank=True)
    answer_type = models.CharField('Тип ответа', max_length=20, choices=AnswerType.choices, default=AnswerType.SHORT_TEXT)
    required = models.BooleanField('Обязательный вопрос', default=False)
    position = models.PositiveIntegerField('Порядок', default=0)
    block_position = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self.block_id and self.survey_id and self.block.survey_id != self.survey_id:
            raise ValidationError({'block': 'Блок должен принадлежать этому опросу.'})

    @property
    def has_options(self):
        return self.answer_type in (self.AnswerType.SINGLE_CHOICE, self.AnswerType.MULTIPLE_CHOICE)


class QuestionOptions(models.Model):
    question = models.ForeignKey(QuestionsBuilder, on_delete=models.CASCADE, related_name='options', verbose_name='Вопрос')
    text = models.CharField('Вариант ответа', max_length=200)
    position = models.PositiveIntegerField('Порядок', default=0)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return self.text


class SurveyAssignment(models.Model):
    """Получатели фиксируются при публикации и больше не зависят от фильтров."""
    survey = models.ForeignKey(SurveysBuilder, on_delete=models.PROTECT, related_name='assignments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='survey_assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)
    recipient_snapshot = models.JSONField(default=dict)
    visible_block_ids = models.JSONField(default=list)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['survey', 'user'], name='unique_survey_recipient')]


class SurveyResponse(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        SUBMITTED = 'submitted', 'Отправлен'

    assignment = models.OneToOneField(SurveyAssignment, on_delete=models.CASCADE, related_name='response')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)


class SurveyAnswer(models.Model):
    response = models.ForeignKey(SurveyResponse, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(QuestionsBuilder, on_delete=models.PROTECT, related_name='answers')
    value = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['question__position', 'question_id']
        constraints = [models.UniqueConstraint(fields=['response', 'question'], name='unique_response_question')]

    def clean(self):
        super().clean()
        if self.response_id and self.question_id:
            if self.response.assignment.survey_id != self.question.survey_id:
                raise ValidationError('Вопрос и ответ должны принадлежать одному опроснику.')
