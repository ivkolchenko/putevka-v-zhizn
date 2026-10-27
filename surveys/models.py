from django.conf import settings
from django.db import models

from surveys_builder.models import QuestionsBuilder


class SurveySubmission(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="survey_submissions",
        verbose_name="Ученик",
    )
    survey = models.ForeignKey(
        "surveys_builder.SurveysBuilder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submissions",
        verbose_name="Исходный опрос",
    )
    source_survey_id = models.PositiveBigIntegerField("ID исходного опроса", db_index=True)
    survey_title = models.CharField("Название опроса при отправке", max_length=200)
    survey_description = models.TextField("Описание опроса при отправке", blank=True)
    submission_key = models.UUIDField(unique=True, null=True, blank=True, editable=False)
    submitted_at = models.DateTimeField("Отправлено", auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        indexes = [
            models.Index(fields=["user", "source_survey_id"]),
        ]

    @classmethod
    def from_survey(cls, *, user, survey):
        return cls(
            user=user,
            survey=survey,
            source_survey_id=survey.pk,
            survey_title=survey.title,
            survey_description=survey.description,
        )

    def __str__(self):
        return f"{self.survey_title} — {self.user}"


class SurveyAnswer(models.Model):
    submission = models.ForeignKey(
        SurveySubmission,
        on_delete=models.CASCADE,
        related_name="answers",
        verbose_name="Прохождение",
    )
    question = models.ForeignKey(
        "surveys_builder.QuestionsBuilder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_answers",
        verbose_name="Исходный вопрос",
    )
    source_question_id = models.PositiveBigIntegerField("ID исходного вопроса")
    question_title = models.CharField("Вопрос при отправке", max_length=500)
    question_type = models.CharField(
        "Тип вопроса при отправке",
        max_length=20,
        choices=QuestionsBuilder.AnswerType.choices,
    )
    question_position = models.PositiveIntegerField("Порядок вопроса при отправке")
    option = models.ForeignKey(
        "surveys_builder.QuestionOptions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_answers",
        verbose_name="Исходный вариант",
    )
    source_option_id = models.PositiveBigIntegerField(
        "ID выбранного варианта", null=True, blank=True
    )
    option_text = models.CharField("Выбранный вариант при отправке", max_length=200, blank=True)
    text_value = models.TextField("Текст ответа", blank=True)

    class Meta:
        ordering = ["question_position", "source_question_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "source_question_id"],
                name="surveys_one_answer_per_question_submission",
            ),
        ]

    @classmethod
    def from_question(cls, *, submission, question, text_value="", option=None):
        if question.survey_id != submission.source_survey_id:
            raise ValueError("The question does not belong to the submitted survey.")
        if option is not None and option.question_id != question.pk:
            raise ValueError("The selected option does not belong to the question.")

        return cls(
            submission=submission,
            question=question,
            source_question_id=question.pk,
            question_title=question.title,
            question_type=question.answer_type,
            question_position=question.position,
            option=option,
            source_option_id=option.pk if option is not None else None,
            option_text=option.text if option is not None else "",
            text_value=text_value,
        )

    def __str__(self):
        return self.question_title
