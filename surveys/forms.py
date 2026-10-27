import hashlib
import json
import uuid

from django import forms

from surveys_builder.models import QuestionsBuilder


def survey_fingerprint(survey, questions):
    structure = {
        "survey": [survey.pk, survey.title, survey.description],
        "questions": [
            [
                question.pk,
                question.title,
                question.answer_type,
                question.required,
                question.position,
                [
                    [option.pk, option.text, option.position]
                    for option in question.options.all()
                ],
            ]
            for question in questions
        ],
    }
    serialized = json.dumps(structure, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SurveyResponseForm(forms.Form):
    def __init__(self, *args, survey, **kwargs):
        super().__init__(*args, **kwargs)
        self.survey = survey
        self.questions = list(survey.questions.prefetch_related("options"))
        self.fingerprint = survey_fingerprint(survey, self.questions)
        self.question_fields = []

        for question in self.questions:
            field_name = f"question_{question.pk}"
            if question.answer_type == QuestionsBuilder.AnswerType.SHORT_TEXT:
                field = forms.CharField(
                    label=question.title,
                    required=question.required,
                    widget=forms.TextInput(attrs={"class": "form-control"}),
                )
            elif question.answer_type == QuestionsBuilder.AnswerType.LONG_TEXT:
                field = forms.CharField(
                    label=question.title,
                    required=question.required,
                    widget=forms.Textarea(attrs={"class": "form-control", "rows": 5}),
                )
            elif question.answer_type == QuestionsBuilder.AnswerType.SINGLE_CHOICE:
                field = forms.ModelChoiceField(
                    label=question.title,
                    required=question.required,
                    queryset=question.options.all(),
                    empty_label=None if question.required else "Без ответа",
                    widget=forms.RadioSelect,
                )
            else:
                raise ValueError(f"Неизвестный тип вопроса: {question.answer_type}")

            self.fields[field_name] = field
            self.question_fields.append((question, field_name))

        self.fields["submission_key"] = forms.UUIDField(
            widget=forms.HiddenInput,
            initial=uuid.uuid4,
        )
        self.fields["survey_fingerprint"] = forms.CharField(
            widget=forms.HiddenInput,
            initial=self.fingerprint,
        )

    def clean(self):
        cleaned_data = super().clean()
        if not self.questions:
            raise forms.ValidationError("В опросе пока нет вопросов.")
        submitted_fingerprint = cleaned_data.get("survey_fingerprint")
        if submitted_fingerprint and submitted_fingerprint != self.fingerprint:
            raise forms.ValidationError(
                "Опрос изменился. Обновите страницу и заполните его заново."
            )
        return cleaned_data
