from django import forms
from .models import SurveysBuilder, QuestionsBuilder

class SurveysBuilderForm(forms.ModelForm):
    class Meta:
        model = SurveysBuilder
        fields = [
            'title',
        'description',
        ]


class QuestionsBuilderForm(forms.ModelForm):
    class Meta:
        model = QuestionsBuilder
        fields = ['title', 'answer_type', 'required']