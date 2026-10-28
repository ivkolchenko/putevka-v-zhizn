from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from my_study.models import Course, School, Subject
from scholar_form.models import UserInfo

from .models import QuestionBlock, QuestionsBuilder, QuestionOptions, SurveysBuilder
from .selectors import group_questions, student_name, student_queryset


class SurveysBuilderForm(forms.ModelForm):
    class Meta:
        model = SurveysBuilder
        fields = ['title', 'description', 'deadline']
        widgets = {'deadline': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'})}

    def clean_deadline(self):
        deadline = self.cleaned_data['deadline']
        if deadline and deadline <= timezone.now():
            raise ValidationError('Срок ответа должен быть в будущем.')
        return deadline


class SurveyListFilterForm(forms.Form):
    q = forms.CharField(label='Поиск опроса', required=False, max_length=200,
                        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Название или описание'}))
    status = forms.ChoiceField(label='Статус', required=False,
        choices=[('', 'Все статусы'), *SurveysBuilder.Status.choices],
        widget=forms.Select(attrs={'class': 'form-select'}))


class QuestionBlockForm(forms.ModelForm):
    class Meta:
        model = QuestionBlock
        fields = ['title', 'description']


class QuestionsBuilderForm(forms.ModelForm):
    options_text = forms.CharField(
        label='Варианты ответа', required=False, max_length=10000,
        widget=forms.Textarea(attrs={'rows': 5}),
        help_text='Для вопросов с выбором — от 2 до 50 вариантов, каждый с новой строки. Для остальных типов поле не используется.',
    )

    class Meta:
        model = QuestionsBuilder
        fields = ['block', 'title', 'description', 'answer_type', 'required', 'options_text']

    def __init__(self, *args, survey=None, **kwargs):
        super().__init__(*args, **kwargs)
        if survey is None and self.instance.survey_id:
            survey = self.instance.survey
        self.fields['block'].queryset = survey.blocks.all() if survey else QuestionBlock.objects.none()
        self.fields['block'].empty_label = 'Без блока'
        if self.instance.pk:
            self.fields['options_text'].initial = '\n'.join(o.text for o in self.instance.options.all())

    def clean(self):
        data = super().clean()
        labels = []
        if data.get('answer_type') in (QuestionsBuilder.AnswerType.SINGLE_CHOICE, QuestionsBuilder.AnswerType.MULTIPLE_CHOICE):
            labels = [s.strip() for s in data.get('options_text', '').splitlines() if s.strip()]
            if not 2 <= len(labels) <= 50:
                self.add_error('options_text', 'Добавьте от 2 до 50 вариантов.')
            if any(len(label) > 200 for label in labels):
                self.add_error('options_text', 'Вариант должен быть не длиннее 200 символов.')
            if len(labels) != len(set(labels)):
                self.add_error('options_text', 'Варианты не должны повторяться.')
        data['option_labels'] = labels
        return data

    def save_options(self, question):
        # Вызывается сервисом только для черновика, внутри общей транзакции.
        existing = {option.text: option for option in question.options.all()}
        keep = []
        for position, text in enumerate(self.cleaned_data['option_labels'], 1):
            option = existing.get(text)
            if option:
                option.position = position
                option.save(update_fields=['position'])
            else:
                option = QuestionOptions.objects.create(question=question, text=text, position=position)
            keep.append(option.pk)
        question.options.exclude(pk__in=keep).delete()


class StudentMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f'{student_name(obj)} ({obj.get_username()})'


class AudienceForm(forms.Form):
    mode = forms.ChoiceField(label='Выбор получателей', choices=SurveysBuilder.AudienceMode.choices)
    grades = forms.MultipleChoiceField(label=UserInfo._meta.get_field('next_year_class_digit').verbose_name, required=False,
                                      choices=[(str(n), str(n)) for n in range(1, 12)])
    profiles = forms.MultipleChoiceField(label='Учебные профили', choices=UserInfo.InternalStudyProfile.choices, required=False)
    statuses = forms.MultipleChoiceField(label='Статус в программе (включая выпускников)', choices=UserInfo.STATUSES, required=False)
    subjects = forms.ModelMultipleChoiceField(label='Предметы изучаемых курсов', queryset=Subject.objects.all(), required=False)
    planned_exams = forms.ModelMultipleChoiceField(label='Планируемые экзамены', queryset=Subject.objects.all(), required=False)
    schools = forms.ModelMultipleChoiceField(label='Учебные организации / школы курсов', queryset=School.objects.all(), required=False)
    courses = forms.ModelMultipleChoiceField(label='Курсы', queryset=Course.objects.select_related('school', 'subject'), required=False)
    steps = forms.MultipleChoiceField(label='Этапы отбора', choices=UserInfo.SelectionStep.choices, required=False)
    form_statuses = forms.MultipleChoiceField(label='Статусы анкеты', choices=UserInfo.FormStatus.choices, required=False)
    account_active = forms.ChoiceField(label='Активность учётной записи', required=False, initial='yes',
                                     choices=[('', 'Любая'), ('yes', 'Активная'), ('no', 'Отключённая')],
                                     help_text='Это доступ к входу на сайт, а не статус участия в программе.')
    registration_complete = forms.ChoiceField(label='Регистрация завершена', required=False,
                                              choices=[('', 'Не учитывать'), ('yes', 'Да'), ('no', 'Нет')])
    region = forms.CharField(label='Регион (точное название)', required=False, max_length=1000)
    q = forms.CharField(label='Поиск по имени, логину или email', required=False, max_length=200)
    included_users = StudentMultipleChoiceField(label='Конкретные ученики / дополнительно включить', queryset=student_queryset(), required=False)
    excluded_users = StudentMultipleChoiceField(label='Исключить учеников', queryset=student_queryset(), required=False)

    FILTER_FIELDS = ('grades', 'profiles', 'statuses', 'subjects', 'planned_exams', 'schools', 'courses',
                     'steps', 'form_statuses', 'account_active', 'registration_complete', 'region', 'q')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field, (forms.MultipleChoiceField, forms.ModelMultipleChoiceField)):
                field.widget = forms.CheckboxSelectMultiple(choices=field.choices)
        # В индивидуальном режиме даже устаревшие ID фильтров не мешают выбору учеников.
        if self.is_bound and self.data.get('mode') == SurveysBuilder.AudienceMode.INDIVIDUAL:
            self.data = self.data.copy()
            for name in self.FILTER_FIELDS:
                self.data.pop(name, None)

    @classmethod
    def empty_data(cls):
        """Явно пустая настройка, включая ограничение активности аккаунта."""
        return {'mode': SurveysBuilder.AudienceMode.FILTERS, 'account_active': ''}

    @classmethod
    def saved_data(cls, survey):
        return {**survey.audience_filters, 'mode': survey.audience_mode,
                'included_users': list(survey.included_users.values_list('pk', flat=True)),
                'excluded_users': list(survey.excluded_users.values_list('pk', flat=True))}

    def clean(self):
        data = super().clean()
        if data.get('mode') == 'individual' and not data.get('included_users'):
            self.add_error('included_users', 'Выберите хотя бы одного ученика.')
        return data

    def serialized_filters(self):
        result = {}
        for key in self.FILTER_FIELDS:
            value = self.cleaned_data[key]
            result[key] = list(value.values_list('pk', flat=True)) if hasattr(value, 'values_list') else value
        return result


class ResponseForm(forms.Form):
    """Одна форма для предпросмотра, черновика и окончательной отправки."""
    def __init__(self, *args, questions, final=True, blocks=None, **kwargs):
        self.questions = list(questions)
        self.blocks = blocks
        super().__init__(*args, **kwargs)
        for question in self.questions:
            attrs = {'label': question.title, 'help_text': question.description,
                     'required': question.required and final}
            kind = question.answer_type
            if kind == QuestionsBuilder.AnswerType.SHORT_TEXT:
                field = forms.CharField(max_length=1000, **attrs)
            elif kind == QuestionsBuilder.AnswerType.LONG_TEXT:
                field = forms.CharField(max_length=10000, widget=forms.Textarea(attrs={'rows': 4}), **attrs)
            elif question.has_options:
                choices = [(option.pk, option.text) for option in question.options.all()]
                if kind == QuestionsBuilder.AnswerType.SINGLE_CHOICE:
                    if not question.required:
                        choices.insert(0, ('', 'Без ответа'))
                    field = forms.TypedChoiceField(choices=choices, coerce=int, empty_value=None, widget=forms.RadioSelect, **attrs)
                else:
                    field = forms.TypedMultipleChoiceField(choices=choices, coerce=int, widget=forms.CheckboxSelectMultiple, **attrs)
            elif kind == QuestionsBuilder.AnswerType.NUMBER:
                field = forms.DecimalField(max_digits=18, decimal_places=6, widget=forms.NumberInput(attrs={'step': 'any'}), **attrs)
            elif kind == QuestionsBuilder.AnswerType.DATE:
                field = forms.DateField(widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}), **attrs)
            else:
                field = forms.TypedChoiceField(choices=([('', 'Без ответа')] if not question.required else []) + [(n, n) for n in range(1, 6)], coerce=int, empty_value=None, widget=forms.RadioSelect, **attrs)
            if isinstance(field.widget, (forms.TextInput, forms.Textarea)):
                field.widget.attrs['class'] = 'form-control'
            self.fields[f'q_{question.pk}'] = field

    @property
    def sections(self):
        blocks = self.blocks
        if blocks is None:
            blocks = sorted({q.block_id: q.block for q in self.questions if q.block_id}.values(),
                            key=lambda b: (b.position, b.pk))
        return [{'block': section['block'],
                 'fields': [self[f'q_{q.pk}'] for q in section['questions']]}
                for section in group_questions(self.questions, blocks)]

    def clean(self):
        data = super().clean()
        if any(key.startswith('q_') and key not in self.fields for key in self.data):
            raise ValidationError('Ответ содержит вопрос из другого опросника.')
        return data

    def answer_values(self):
        from datetime import date
        from decimal import Decimal
        values = {}
        for question in self.questions:
            value = self.cleaned_data[f'q_{question.pk}']
            if isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, list):
                value = list(dict.fromkeys(value))
            values[question.pk] = value
        return values
