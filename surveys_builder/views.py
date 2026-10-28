"""Серверные страницы конструктора. Каждое изменение — POST с CSRF."""
import csv
import re

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import services
from .forms import AudienceForm, QuestionBlockForm, QuestionsBuilderForm, ResponseForm, SurveyListFilterForm, SurveysBuilderForm
from .models import SurveyResponse, SurveysBuilder
from .selectors import group_questions, resolve_audience, student_name


def page(request, template, survey=None, **context):
    return render(request, 'surveys_builder/' + template, {'survey': survey, **context})


def failure(request, error, survey=None):
    return render(request, 'surveys_builder/error.html',
                  {'survey': survey, 'errors': error.messages}, status=400)


def get_survey(pk):
    return get_object_or_404(SurveysBuilder, pk=pk)


@staff_member_required
@require_http_methods(['GET'])
def survey_list(request):
    form = SurveyListFilterForm(request.GET)
    surveys = SurveysBuilder.objects.select_related('author').annotate(
        recipient_count=Count('assignments'),
        submitted_count=Count('assignments', filter=Q(assignments__response__status=SurveyResponse.Status.SUBMITTED)),
    ).order_by('-created_at', '-pk')
    total_count = SurveysBuilder.objects.count()
    if form.is_valid():
        if form.cleaned_data['status']:
            surveys = surveys.filter(status=form.cleaned_data['status'])
        for word in form.cleaned_data['q'].split():
            # SQLite LIKE не учитывает регистр кириллицы; REGEXP Django учитывает Unicode.
            pattern = re.escape(word)
            surveys = surveys.filter(Q(title__iregex=pattern) | Q(description__iregex=pattern))
    else:
        surveys = surveys.none()
    query = request.GET.copy()
    query.pop('page', None)
    return page(request, 'survey_list.html', filter_form=form, total_count=total_count,
                pagination_query=query.urlencode(),
                page_obj=Paginator(surveys, 25).get_page(request.GET.get('page')))


@staff_member_required
@require_http_methods(['GET', 'POST'])
def constructor(request):
    form = SurveysBuilderForm()
    if request.method == 'POST':
        try:
            survey = services.create_survey(request.user, request.POST)
            return redirect('surveys_builder:audience', pk=survey.pk)
        except services.InvalidForm as error:
            form = error.form
    return page(request, 'constructor.html', form=form)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def survey_edit(request, pk):
    survey = get_survey(pk)
    form = SurveysBuilderForm(instance=survey)
    if request.method == 'POST':
        try:
            services.update_survey(pk, request.user, request.POST, request.POST.get('revision'))
            return redirect('surveys_builder:audience', pk=pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'edit.html', survey, form=form, heading='Название и срок ответа')


@staff_member_required
@require_http_methods(['GET', 'POST'])
def audience(request, pk):
    survey = get_survey(pk)
    initial = AudienceForm.saved_data(survey) if survey.audience_configured else {'mode': 'filters', 'account_active': 'yes'}
    form = AudienceForm(initial=initial)
    recipients = None
    if request.method == 'POST':
        try:
            action = request.POST.get('action', 'save')
            clear_field = request.POST.get('clear_field')
            if action in ('preview', 'reset') or clear_field:
                services.require_draft(survey, request.POST.get('revision'))
                data = request.POST.copy()
                if action == 'reset':
                    data = AudienceForm.empty_data()
                elif clear_field:
                    if clear_field not in (*AudienceForm.FILTER_FIELDS, 'included_users', 'excluded_users'):
                        raise ValidationError('Неизвестный фильтр.')
                    data.pop(clear_field, None)
                form = AudienceForm(data)
                if form.is_valid():
                    recipients = resolve_audience(form.cleaned_data)
            elif action == 'save':
                services.save_audience(pk, request.user, request.POST, request.POST.get('revision'))
                return redirect('surveys_builder:survey_detail', pk=pk)
            else:
                raise ValidationError('Неизвестное действие.')
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    elif survey.is_draft and survey.audience_configured:
        try:
            recipients = services.audience_for(survey)
        except ValidationError as error:
            form = AudienceForm(AudienceForm.saved_data(survey))
            form.is_valid()
            form.add_error(None, error)
    # POST preview stays on the same form; the complete saved selection is paginated on the confirmation page.
    preview = [(u.pk, student_name(u), u.get_username()) for u in recipients[:50]] if recipients is not None else []
    return page(request, 'audience.html', survey, form=form, recipients=preview,
                recipient_count=recipients.count() if recipients is not None else None)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def survey_detail(request, pk):
    survey = get_survey(pk)
    if request.method == 'GET' and survey.is_draft and not survey.audience_configured:
        return redirect('surveys_builder:audience', pk=pk)
    form = QuestionsBuilderForm(survey=survey, initial={'block': request.GET.get('block')})
    if request.method == 'POST':
        try:
            services.save_question(pk, request.user, request.POST, request.POST.get('revision'))
            return redirect('surveys_builder:survey_detail', pk=pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    questions = list(survey.questions.prefetch_related('options'))
    return page(request, 'survey_detail.html', survey, form=form, questions=questions,
                sections=group_questions(questions, survey.blocks.all()))


@staff_member_required
@require_http_methods(['GET', 'POST'])
def block_edit(request, pk, block_pk=None):
    survey = get_survey(pk)
    block = get_object_or_404(survey.blocks, pk=block_pk) if block_pk else None
    form = QuestionBlockForm(instance=block)
    if request.method == 'POST':
        try:
            services.save_block(pk, request.user, request.POST, request.POST.get('revision'), block_pk)
            return redirect('surveys_builder:survey_detail', pk=pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'edit.html', survey, form=form,
                heading='Редактировать блок' if block else 'Добавить блок вопросов')


@staff_member_required
@require_http_methods(['GET', 'POST'])
def block_delete(request, pk, block_pk):
    survey = get_survey(pk)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    if request.method == 'POST':
        try:
            services.block_action(pk, block_pk, request.user, 'delete', request.POST.get('revision'))
            return redirect('surveys_builder:survey_detail', pk=pk)
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'confirm.html', survey, heading=f'Удалить блок «{block.title}»?',
                detail='Вопросы сохранятся и будут перенесены в раздел «Без блока».')


@staff_member_required
@require_POST
def block_action(request, pk, block_pk, action):
    try:
        services.block_action(pk, block_pk, request.user, action, request.POST.get('revision'))
    except ValidationError as error:
        return failure(request, error, get_survey(pk))
    return redirect('surveys_builder:survey_detail', pk=pk)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def question_edit(request, pk, question_pk):
    survey = get_survey(pk)
    question = get_object_or_404(survey.questions, pk=question_pk)
    form = QuestionsBuilderForm(instance=question)
    if request.method == 'POST':
        try:
            services.save_question(pk, request.user, request.POST, request.POST.get('revision'), question_pk)
            return redirect('surveys_builder:survey_detail', pk=pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'edit.html', survey, form=form, heading='Редактирование вопроса')


@staff_member_required
@require_http_methods(['GET', 'POST'])
def question_delete(request, pk, question_pk):
    survey = get_survey(pk)
    question = get_object_or_404(survey.questions, pk=question_pk)
    if request.method == 'POST':
        try:
            services.question_action(pk, question_pk, request.user, 'delete', request.POST.get('revision'))
            return redirect('surveys_builder:survey_detail', pk=pk)
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'confirm.html', survey, heading='Удалить вопрос?', detail=question.title)


@staff_member_required
@require_POST
def question_action(request, pk, question_pk, action):
    try:
        services.question_action(pk, question_pk, request.user, action, request.POST.get('revision'))
    except ValidationError as error:
        return failure(request, error, get_survey(pk))
    return redirect('surveys_builder:survey_detail', pk=pk)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def survey_delete(request, pk):
    survey = get_survey(pk)
    if request.method == 'POST':
        try:
            services.delete_survey(pk, request.user, request.POST.get('revision'), allow_published=True)
            messages.success(request, 'Опрос и связанные с ним назначения и ответы удалены.')
            return redirect('staff_surveys_list')
        except ValidationError as error:
            return failure(request, error, survey)
    counts = survey.assignments.aggregate(
        recipients=Count('pk'), responses=Count('response'),
        submitted=Count('response', filter=Q(response__status=SurveyResponse.Status.SUBMITTED)),
    )
    return page(request, 'survey_delete.html', survey, counts=counts)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def preview(request, pk):
    survey = get_survey(pk)
    questions = list(survey.questions.select_related('block').prefetch_related('options'))
    form = ResponseForm(request.POST if request.method == 'POST' else None, questions=questions, blocks=survey.blocks.all())
    if request.method == 'POST' and form.is_valid():
        messages.success(request, 'Проверка пройдена. Это предпросмотр: ответы не сохранялись.')
    errors = services.publication_errors(survey) if survey.is_draft else []
    recipients = services.audience_for(survey) if survey.is_draft and not errors else None
    ids = list(recipients.values_list('pk', flat=True)) if recipients is not None else []
    token = services.publish_token(survey, request.user, ids) if survey.is_draft and not errors else None
    recipient_page = Paginator(recipients, 50).get_page(request.GET.get('page')) if recipients is not None else None
    recipient_rows = [(student_name(u), u.get_username()) for u in recipient_page] if recipient_page is not None else []
    return page(request, 'preview.html', survey, form=form, errors=errors, publish_token=token,
                page_obj=recipient_page, recipients=recipient_rows)


@staff_member_required
@require_POST
def publish(request, pk):
    try:
        services.publish_survey(pk, request.user, request.POST.get('token'))
    except ValidationError as error:
        return failure(request, error, get_survey(pk))
    messages.success(request, 'Опросник опубликован и появился в личных кабинетах получателей.')
    return redirect('surveys_builder:results', pk=pk)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def lifecycle(request, pk, action):
    survey = get_survey(pk)
    if action not in ('close', 'archive'):
        return failure(request, ValidationError('Неизвестное действие.'), survey)
    if request.method == 'POST':
        try:
            services.change_status(pk, request.user, action, request.POST.get('revision'))
            return redirect('surveys_builder:results', pk=pk)
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'confirm.html', survey,
                heading='Завершить приём ответов?' if action == 'close' else 'Переместить опросник в архив?',
                detail='Уже отправленные ответы сохранятся. Продолжить сбор можно в новой копии опросника.', lifecycle=True)


@staff_member_required
@require_POST
def survey_copy(request, pk):
    survey = services.copy_survey(pk, request.user)
    return redirect('surveys_builder:audience', pk=survey.pk)


@staff_member_required
def results(request, pk):
    survey = get_survey(pk)
    assignments = survey.assignments.select_related('user', 'response')
    counts = assignments.aggregate(total=Count('pk'),
                                   submitted=Count('pk', filter=Q(response__status='submitted')),
                                   started=Count('pk', filter=Q(response__status='draft')))
    counts['not_started'] = counts['total'] - counts['submitted'] - counts['started']
    return page(request, 'results.html', survey, counts=counts,
                page_obj=Paginator(assignments, 50).get_page(request.GET.get('page')))


def response_rows(response):
    return [(answer.question.title, services.answer_display(answer.question, answer.value))
            for answer in response.answers.select_related('question').prefetch_related('question__options')]


def response_sections(response):
    survey = response.assignment.survey
    values = {a.question_id: a.value for a in response.answers.all()}
    return [{'block': section['block'],
             'rows': [(q.title, services.answer_display(q, values.get(q.pk))) for q in section['questions']]}
            for section in group_questions(survey.questions.prefetch_related('options'), survey.blocks.all())]


@staff_member_required
def response_detail(request, pk, response_pk):
    survey = get_survey(pk)
    response = get_object_or_404(SurveyResponse.objects.select_related('assignment'),
                                 pk=response_pk, assignment__survey=survey, status='submitted')
    return page(request, 'response_detail.html', survey, response=response, sections=response_sections(response))


class CsvBuffer:
    def write(self, value):
        return value


def csv_cell(value):
    text = str(value) if value is not None else ''
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


@staff_member_required
def results_csv(request, pk):
    survey = get_survey(pk)
    questions = list(survey.questions.select_related('block').prefetch_related('options'))
    responses = SurveyResponse.objects.filter(assignment__survey=survey, status='submitted').select_related(
        'assignment').prefetch_related('answers').order_by('pk')

    def rows():
        writer = csv.writer(CsvBuffer(), delimiter=';')
        yield '\ufeff'
        headers = [f'[{q.block.title}] {q.title}' if q.block_id else q.title for q in questions]
        yield writer.writerow([csv_cell(v) for v in ['ID ответа', 'Ученик', 'Логин', 'Отправлен'] + headers])
        for response in responses.iterator(chunk_size=100):
            values = {answer.question_id: answer.value for answer in response.answers.all()}
            snapshot = response.assignment.recipient_snapshot
            row = [response.pk, snapshot.get('name', ''), snapshot.get('username', ''), response.submitted_at.isoformat()]
            row.extend(services.answer_display(q, values.get(q.pk)) for q in questions)
            yield writer.writerow([csv_cell(v) for v in row])

    result = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
    result['Content-Disposition'] = f'attachment; filename="survey-{survey.pk}-results.csv"'
    return result
