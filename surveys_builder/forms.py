from django import forms
from .models import SurveysBuilder, QuestionsBuilder, QuestionOptions

class SurveysBuilderForm(forms.ModelForm):
    class Meta:
        model = SurveysBuilder
        fields = [
            'title',
        'description',
        ]


class QuestionsBuilderForm(forms.ModelForm):
    options_text = forms.CharField(
        label="Варианты ответа",
        required=False,
        max_length=10000,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text=(
            "Для типа «Один вариант ответа» введи каждый вариант "
            "с новой строки. Для текстовых вопросов оставь пустым."
        ),
    )

    class Meta:
        model = QuestionsBuilder
        fields = [
            "title",
            "answer_type",
            "required",
            "options_text",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance.pk:
            texts = self.instance.options.values_list(
                "text",
                flat=True,
            )

            self.fields["options_text"].initial = "\n".join(texts)

    def clean(self):
        cleaned_data = super().clean()

        answer_type = cleaned_data.get("answer_type")
        options_text = cleaned_data.get("options_text", "")

        option_labels = [
            line.strip()
            for line in options_text.splitlines()
            if line.strip()
        ]

        if answer_type == QuestionsBuilder.AnswerType.SINGLE_CHOICE:
            if not 2 <= len(option_labels) <= 50:
                self.add_error(
                    "options_text",
                    "Добавь от 2 до 50 вариантов ответа.",
                )

            if any(len(text) > 200 for text in option_labels):
                self.add_error(
                    "options_text",
                    "Вариант не должен быть длиннее 200 символов.",
                )

            if len(option_labels) != len(set(option_labels)):
                self.add_error(
                    "options_text",
                    "Варианты не должны повторяться.",
                )
        else:
            option_labels = []

        cleaned_data["option_labels"] = option_labels

        return cleaned_data

    def save_options(self, question):
        question.options.all().delete()

        for position, text in enumerate(
            self.cleaned_data["option_labels"],
            start=1,
        ):
            QuestionOptions.objects.create(
                question=question,
                text=text,
                position=position,
            )