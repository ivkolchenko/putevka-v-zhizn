import uuid

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from surveys_builder.models import QuestionsBuilder, SurveysBuilder

from .forms import SurveyResponseForm
from .models import SurveyAnswer, SurveySubmission


def submit_survey_response(*, user, form):
    if not form.is_bound or user.pk is None:
        raise ValueError("Требуются пользователь и заполненная форма.")

    try:
        submission_key = uuid.UUID(str(form.data.get("submission_key")))
    except (TypeError, ValueError, AttributeError):
        form.is_valid()
        return None, False

    try:
        with transaction.atomic():
            existing = SurveySubmission.objects.filter(
                submission_key=submission_key
            ).first()
            if existing:
                if existing.user_id != user.pk or existing.source_survey_id != form.survey.pk:
                    raise PermissionDenied
                return existing, False

            if not form.is_valid():
                return None, False

            survey = SurveysBuilder.objects.filter(pk=form.survey.pk).first()
            if survey is None:
                form.add_error(None, "Опрос больше не доступен.")
                return None, False

            current_form = SurveyResponseForm(survey=survey)
            if current_form.fingerprint != form.cleaned_data["survey_fingerprint"]:
                form.add_error(
                    None,
                    "Опрос изменился. Обновите страницу и заполните его заново.",
                )
                return None, False

            submission = SurveySubmission.from_survey(user=user, survey=survey)
            submission.submission_key = submission_key
            submission.save()

            for question, field_name in form.question_fields:
                value = form.cleaned_data.get(field_name)
                if value is None or value == "":
                    continue

                if question.answer_type == QuestionsBuilder.AnswerType.SINGLE_CHOICE:
                    answer = SurveyAnswer.from_question(
                        submission=submission, question=question, option=value
                    )
                else:
                    answer = SurveyAnswer.from_question(
                        submission=submission, question=question, text_value=value
                    )
                answer.save()

            return submission, True
    except IntegrityError:
        existing = SurveySubmission.objects.filter(submission_key=submission_key).first()
        if existing is None:
            raise
        if existing.user_id != user.pk or existing.source_survey_id != form.survey.pk:
            raise PermissionDenied
        return existing, False
