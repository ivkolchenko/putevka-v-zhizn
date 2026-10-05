from django.db import models

class SurveysBuilder(models.Model):
    title = models.CharField(
        "Название",
        max_length=200,
    )
    description = models.TextField(
        "Описание",
        blank=True,
    )
    created_at = models.DateTimeField(
        "Дата создания",
        auto_now_add=True,
    )

    def __str__(self):
        return self.title


class QuestionsBuilder(models.Model):
    class AnswerType(models.TextChoices):
        SHORT_TEXT = "short_text", "Короткий текст"
        LONG_TEXT = "long_text", "Развернутый текст"

    survey = models.ForeignKey(
        SurveysBuilder,
        on_delete=models.CASCADE,
        related_name='questions',
        verbose_name='Опросник',
    )
    title = models.CharField(
        'Текст вопроса',
        max_length=500,
    )
    answer_type = models.CharField(
        'Тип ответа',
        max_length=20,
        choices=AnswerType.choices,
        default=AnswerType.SHORT_TEXT,
    )
    required = models.BooleanField(
        'Обязательный вопрос',
        default=False,
    )
    position = models.PositiveIntegerField(
        'Порядок',
        default=0,
    )

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return self.title

