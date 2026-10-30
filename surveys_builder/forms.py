from django import forms
import uuid
from django.core.exceptions import ValidationError
from django.utils import timezone

from my_study.models import Course, School, Subject
from scholar_form.models import UserInfo

from .models import BlockTextSection, QuestionBlock, QuestionsBuilder, QuestionOptions, SurveysBuilder
from .selectors import conditional_question_ids, group_questions, student_name, student_queryset


class SurveysBuilderForm(forms.ModelForm):
    class Meta:
        model = SurveysBuilder
        fields = ['title', 'description']
        widgets = {'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Например, «Обратная связь об учёбе»'}),
                   'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3})}
        labels = {'description': 'Описание (необязательно)'}



class SurveyListFilterForm(forms.Form):
    q = forms.CharField(label='Поиск опроса', required=False, max_length=200,
                        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Название или описание'}))
    status = forms.ChoiceField(label='Статус', required=False,
        choices=[('', 'Все статусы'), *SurveysBuilder.Status.choices],
        widget=forms.Select(attrs={'class': 'form-select'}))


class ConditionalOptionField(forms.ModelChoiceField):
    def label_from_instance(self, option):
        return f'{option.question.title} — {option.text}'



class QuestionBlockForm(forms.ModelForm):
    show_if_option = ConditionalOptionField(
        label='Показывать при выборе варианта', queryset=QuestionOptions.objects.none(), required=False,
        empty_label='Всегда (с учётом фильтров видимости)',
        help_text='Выберите вариант вопроса из предыдущего блока. Если вариант снят, блок скрывается, а его ответы очищаются.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.survey_id:
            self.fields['show_if_option'].queryset = QuestionOptions.objects.filter(
                question__survey_id=self.instance.survey_id, question__block__position__lt=self.instance.position,
                question__answer_type__in=['single_choice', 'multiple_choice'],
            ).select_related('question__block').order_by('question__position', 'position', 'pk')

    class Meta:
        model = QuestionBlock
        fields = ['title', 'description', 'show_if_option']
        widgets = {'title': forms.TextInput(attrs={'placeholder': 'Например, «О себе» или «Об учёбе»'}),
                   'description': forms.Textarea(attrs={'rows': 3})}
        labels = {'description': 'Описание (необязательно)'}


class QuestionsBuilderForm(forms.ModelForm):
    show_if_option = ConditionalOptionField(
        label='Показывать при выборе варианта', queryset=QuestionOptions.objects.none(), required=False,
        empty_label='Всегда (с учётом фильтров видимости)',
        help_text='Для подвопроса выберите вариант родительского вопроса из этого блока. При снятии варианта ответ подвопроса очищается.',
    )
    options_text = forms.CharField(
        label='Варианты ответа', required=False, max_length=10049,
        widget=forms.Textarea(attrs={'rows': 5}),
        help_text='Для вопросов с выбором — от 2 до 50 вариантов, каждый с новой строки. Для остальных типов поле не используется.',
    )

    class Meta:
        model = QuestionsBuilder
        fields = ['block', 'title', 'description', 'answer_type', 'required', 'options_text', 'show_if_option']

    def __init__(self, *args, survey=None, **kwargs):
        super().__init__(*args, **kwargs)
        if survey is None and self.instance.survey_id:
            survey = self.instance.survey
        self.fields['block'].queryset = survey.blocks.all() if survey else QuestionBlock.objects.none()
        self.fields['block'].empty_label = 'Выберите блок'
        block_id = self.data.get('block') if self.is_bound else self.initial.get('block', self.instance.block_id)
        try:
            block_id = int(block_id)
        except (TypeError, ValueError):
            block_id = None
        if survey and block_id:
            self.fields['show_if_option'].queryset = QuestionOptions.objects.filter(
                question__survey=survey, question__block_id=block_id,
                question__answer_type__in=['single_choice', 'multiple_choice'],
            ).exclude(question_id=self.instance.pk).select_related('question').order_by('question__position', 'position', 'pk')
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
        removed = question.options.exclude(pk__in=keep)
        if removed.filter(dependent_questions__isnull=False).exists() or removed.filter(dependent_blocks__isnull=False).exists():
            raise ValidationError('Этот вариант открывает подвопрос или блок. Сначала измените условия или удалите зависимое содержимое.')
        removed.delete()


class StudentMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f'{student_name(obj)} ({obj.get_username()})'


class FilterForm(forms.Form):
    FILTER_FIELDS = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field, (forms.MultipleChoiceField, forms.ModelMultipleChoiceField)):
                field.widget = forms.CheckboxSelectMultiple(choices=field.choices)

    def serialized_filters(self):
        result = {}
        for key in self.FILTER_FIELDS:
            value = self.cleaned_data[key]
            result[key] = list(value.values_list('pk', flat=True)) if hasattr(value, 'values_list') else value
        active = self.data.getlist('active_filters') if hasattr(self.data, 'getlist') else self.data.get('active_filters', [])
        result['_active_filters'] = [key for key in self.FILTER_FIELDS if result[key] or key in active]
        return result

    @property
    def filter_groups(self):
        active = self.data.getlist('active_filters') if hasattr(self.data, 'getlist') else self.data.get('active_filters', [])
        if not self.is_bound:
            active = self.initial.get('active_filters', self.initial.get('_active_filters', []))
        groups = []
        for name in self.FILTER_FIELDS:
            field = self.fields[name]
            groups.append({
                'name': name,
                'field': self[name],
                'active': bool(self[name].value()) or name in active or name in self.initial.get('_active_filters', []),
                'empty': isinstance(field, forms.ModelMultipleChoiceField) and not field.queryset.exists(),
            })
        return groups


class BlockVisibilityForm(FilterForm):
    grades = forms.MultipleChoiceField(label='Классы', choices=[('10', '10 класс'), ('11', '11 класс')], required=False)
    profiles = forms.MultipleChoiceField(label='Учебные профили', choices=UserInfo.InternalStudyProfile.choices, required=False)
    statuses = forms.MultipleChoiceField(label='Статус участия', choices=UserInfo.STATUSES, required=False)
    subjects = forms.ModelMultipleChoiceField(label='Предметы изучаемых курсов', queryset=Subject.objects.all(), required=False)
    planned_exams = forms.ModelMultipleChoiceField(label='Планируемые экзамены', queryset=Subject.objects.all(), required=False)
    schools = forms.ModelMultipleChoiceField(label='Учебные организации / школы курсов', queryset=School.objects.all(), required=False)
    courses = forms.ModelMultipleChoiceField(label='Курсы', queryset=Course.objects.select_related('school', 'subject'), required=False)
    steps = forms.MultipleChoiceField(label='Этапы отбора', choices=UserInfo.SelectionStep.choices, required=False)
    FILTER_FIELDS = ('grades', 'profiles', 'statuses', 'subjects', 'planned_exams', 'schools', 'courses', 'steps')
    active_filters = forms.MultipleChoiceField(required=False, choices=[(key, key) for key in FILTER_FIELDS], widget=forms.MultipleHiddenInput)


class AudienceForm(BlockVisibilityForm):
    mode = forms.ChoiceField(label='Способ назначения', choices=SurveysBuilder.AudienceMode.choices, widget=forms.RadioSelect)
    included_users = StudentMultipleChoiceField(label='Участники', queryset=student_queryset(), required=False)
    deadline = forms.DateTimeField(label='Принимать ответы до (необязательно)', required=False,
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound and self.data.get('mode') == 'individual':
            self.data = self.data.copy()
            for name in (*self.FILTER_FIELDS, 'active_filters'):
                self.data.pop(name, None)

    @classmethod
    def empty_data(cls):
        return {'mode': 'filters'}

    @classmethod
    def saved_data(cls, survey):
        return {**{key: value for key, value in survey.audience_filters.items() if key in cls.FILTER_FIELDS},
                'active_filters': survey.audience_filters.get('_active_filters', []),
                'mode': survey.audience_mode, 'deadline': survey.deadline,
                'included_users': list(survey.included_users.values_list('pk', flat=True)) if survey.audience_mode == 'individual' else []}

    def clean_deadline(self):
        deadline = self.cleaned_data['deadline']
        if deadline and deadline <= timezone.now():
            raise ValidationError('Срок ответа должен быть в будущем.')
        return deadline

    def clean(self):
        data = super().clean()
        if data.get('mode') == 'individual' and not data.get('included_users'):
            self.add_error('included_users', 'Выберите хотя бы одного участника.')
        if data.get('mode') == 'filters' and data.get('included_users'):
            self.add_error('included_users', 'Выберите один способ: фильтры или конкретные участники.')
        return data


class AdditionalAudienceForm(AudienceForm):
    deadline = None

    def __init__(self, *args, survey, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['mode'].initial = 'individual'
        self.fields['included_users'].queryset = student_queryset().exclude(survey_assignments__survey=survey)


class BlockTextSectionForm(forms.ModelForm):
    remove_image = forms.BooleanField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = BlockTextSection
        fields = ['title', 'text', 'image', 'image_alt', 'remove_image']
        widgets = {'image': forms.FileInput(attrs={'accept': 'image/png,image/jpeg,image/gif,image/webp'})}

    def clean_image(self):
        image = self.cleaned_data.get('image')
        if image and hasattr(image, 'image'):
            if image.size > 5 * 1024 * 1024 or image.image.width > 6000 or image.image.height > 6000:
                raise ValidationError('Картинка должна быть до 5 МБ и до 6000 пикселей по каждой стороне.')
            if image.image.format not in ('JPEG', 'PNG', 'GIF', 'WEBP'):
                raise ValidationError('Поддерживаются PNG, JPEG, GIF и WebP.')
            extension = 'jpg' if image.image.format == 'JPEG' else image.image.format.lower()
            image.name = f'{uuid.uuid4().hex}.{extension}'
        return image

    def clean(self):
        data = super().clean()
        if data.get('remove_image'):
            if self.files.get('image'):
                self.add_error('remove_image', 'Выберите замену картинки или удаление, не одновременно.')
            data['image'] = False
        if not data.get('text', '').strip() and not data.get('image'):
            raise ValidationError('Добавьте текст или картинку.')
        return data


class ResponseForm(forms.Form):
    """Одна форма для предпросмотра, черновика и окончательной отправки."""
    def __init__(self, *args, questions, final=True, blocks=None, active_question_ids=None, **kwargs):
        self.questions = list(questions)
        self.blocks = blocks
        super().__init__(*args, **kwargs)
        self.active_question_ids = conditional_question_ids(self.questions, self.data if self.is_bound else self.initial)
        if active_question_ids is not None:
            self.active_question_ids.intersection_update(active_question_ids)
        for question in self.questions:
            attrs = {'label': question.title, 'help_text': question.description,
                     'required': question.required and final and question.pk in self.active_question_ids}
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
            if question.pk not in self.active_question_ids:
                field.disabled = True
                self.initial[f'q_{question.pk}'] = None
            self.fields[f'q_{question.pk}'] = field

    @property
    def sections(self):
        blocks = self.blocks
        if blocks is None:
            blocks = sorted({q.block_id: q.block for q in self.questions if q.block_id}.values(),
                            key=lambda b: (b.position, b.pk))
        return [{'block': section['block'], 'fields': [self[f'q_{q.pk}'] for q in section['questions']],
                 'items': [{'type': item['type'], 'section': item.get('section'),
                            'question': item.get('question'),
                            'hidden': item['type'] == 'question' and item['question'].pk not in self.active_question_ids,
                            'field': self[f"q_{item['question'].pk}"] if item['type'] == 'question' else None}
                           for item in section['items']]}
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
            value = self.cleaned_data[f'q_{question.pk}'] if question.pk in self.active_question_ids else None
            if isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, list):
                value = list(dict.fromkeys(value))
            values[question.pk] = value
        return values
