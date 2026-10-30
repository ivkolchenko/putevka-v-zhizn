"""Серверные страницы конструктора. Каждое изменение — POST с CSRF."""
import csv
import re
import mimetypes

from django import forms

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import services
from .forms import AdditionalAudienceForm, AudienceForm, BlockTextSectionForm, BlockVisibilityForm, QuestionBlockForm, QuestionsBuilderForm, SurveyListFilterForm, SurveysBuilderForm
from .models import BlockTextSection, SurveyAssignment, SurveyResponse, SurveysBuilder
from .selectors import assignment_blocks, assignment_questions, block_items, group_questions, resolve_audience, student_name


def page(request, template, survey=None, **context):
    route = getattr(getattr(request, 'resolver_match', None), 'url_name', '')
    steps = {'constructor': 1, 'survey_edit': 1, 'survey_detail': 2, 'block_edit': 2,
             'block_create': 2, 'block_content': 2, 'import_block_template': 2,
             'question_edit': 2, 'question_delete': 2, 'text_section_edit': 2,
             'text_section_create': 2, 'audience': 3, 'preview': 4}
    response = render(request, 'surveys_builder/staff/' + template,
                      {'survey': survey, 'workflow_step': steps.get(route), **context})
    response['Cache-Control'] = 'private, no-store'
    return response


def failure(request, error, survey=None):
    return render(request, 'surveys_builder/staff/error.html',
                  {'survey': survey, 'errors': error.messages}, status=400)


def get_survey(pk):
    return get_object_or_404(SurveysBuilder, pk=pk)


@staff_member_required
@require_http_methods(['GET'])
def survey_list(request):
    form = SurveyListFilterForm(request.GET)
    surveys = SurveysBuilder.objects.filter(is_block_template=False).select_related('author').annotate(
        recipient_count=Count('assignments'),
        submitted_count=Count('assignments', filter=Q(assignments__response__status=SurveyResponse.Status.SUBMITTED)),
    ).order_by('-created_at', '-pk')
    total_count = SurveysBuilder.objects.filter(is_block_template=False).count()
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
            return redirect('surveys_builder:block_create', pk=survey.pk)
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
            return redirect('surveys_builder:survey_detail', pk=pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'edit.html', survey, form=form, heading='Название и описание опроса')


@staff_member_required
@require_http_methods(['GET', 'POST'])
def audience(request, pk):
    survey = get_survey(pk)
    initial = AudienceForm.saved_data(survey) if survey.audience_configured else {'mode': 'filters', 'deadline': survey.deadline}
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
                    if clear_field not in (*AudienceForm.FILTER_FIELDS, 'included_users'):
                        raise ValidationError('Неизвестный фильтр.')
                    data.pop(clear_field, None)
                form = AudienceForm(data)
                if form.is_valid():
                    recipients = resolve_audience(form.cleaned_data)
            elif action == 'save':
                services.save_audience(pk, request.user, request.POST, request.POST.get('revision'))
                return redirect('surveys_builder:preview', pk=pk)
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
def additional_recipients(request, pk):
    survey = get_survey(pk)
    try:
        services.require_additional_audience(survey)
    except ValidationError as error:
        return failure(request, error, survey)
    form = AdditionalAudienceForm(survey=survey)
    recipients = None
    if request.method == 'POST':
        try:
            action = request.POST.get('action', 'add')
            if action == 'preview':
                services.check_revision(survey, request.POST.get('revision'))
                form, recipients, _ = services.additional_audience(survey, request.POST)
            elif action == 'add':
                _, count = services.add_recipients(pk, request.user, request.POST, request.POST.get('revision'))
                messages.success(request, f'Опрос назначен новым участникам: {count}. Уведомления появились в их кабинетах.')
                return redirect('surveys_builder:results', pk=pk)
            else:
                raise ValidationError('Неизвестное действие.')
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'additional_recipients.html', survey, form=form,
                assigned_count=survey.assignments.count(), available_count=form.fields['included_users'].queryset.count(),
                recipients=[(student_name(user), user.get_username()) for user in (recipients or [])[:50]],
                recipient_count=len(recipients) if recipients is not None else None)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def survey_detail(request, pk):
    survey = get_survey(pk)
    if survey.is_block_template and request.method == 'GET':
        return redirect('surveys_builder:block_content', pk=pk, block_pk=survey.blocks.get().pk)
    form = QuestionsBuilderForm(survey=survey, initial={'block': request.GET.get('block')})
    if request.method == 'POST':
        return failure(request, ValidationError('Добавляйте вопросы на странице выбранного блока.'), survey)
    questions = list(survey.questions.prefetch_related('options'))
    sections = group_questions(questions, survey.blocks.prefetch_related('text_sections'))
    return page(request, 'survey_detail.html', survey, form=form, questions=questions,
                sections=sections, empty_blocks=[s['block'] for s in sections if not s['items']],
                structure_ready=bool(sections) and all(s['items'] for s in sections), can_add_block=len(sections) < 50)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def block_edit(request, pk, block_pk=None):
    survey = get_survey(pk)
    block = get_object_or_404(survey.blocks, pk=block_pk) if block_pk else None
    form = QuestionBlockForm(instance=block)
    visibility = BlockVisibilityForm(initial=block.visibility_filters if block else {})
    if request.method == 'POST':
        try:
            saved = services.save_block(pk, request.user, request.POST, request.POST.get('revision'), block_pk)
            messages.success(request, 'Настройки блока сохранены.' if block else 'Блок создан. Добавьте вопрос или текст.')
            return redirect('surveys_builder:block_content', pk=pk, block_pk=saved.pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    if request.method == 'POST':
        form = QuestionBlockForm(request.POST, instance=block)
        visibility = BlockVisibilityForm(request.POST)
        form.is_valid()
        visibility.is_valid()
    return page(request, 'block_edit.html', survey, form=form, visibility_form=visibility,
                edited_block=block, show_visibility=bool(visibility.errors) or any(g['active'] for g in visibility.filter_groups),
                heading='Редактировать блок' if block else 'Добавить блок')


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
                detail='Блок, все его вопросы, текстовые секции и картинки в опросе будут удалены.')


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
            saved = services.save_question(pk, request.user, request.POST, request.POST.get('revision'), question_pk)
            messages.success(request, 'Вопрос сохранён.')
            return redirect('surveys_builder:block_content', pk=pk, block_pk=saved.block_id)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'question_edit.html', survey, form=form, edited_block=question.block, heading='Редактирование вопроса')


@staff_member_required
@require_http_methods(['GET', 'POST'])
def question_delete(request, pk, question_pk):
    survey = get_survey(pk)
    question = get_object_or_404(survey.questions, pk=question_pk)
    if request.method == 'POST':
        try:
            services.question_action(pk, question_pk, request.user, 'delete', request.POST.get('revision'))
            return redirect('surveys_builder:block_content', pk=pk, block_pk=question.block_id)
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'confirm.html', survey, heading='Удалить вопрос?', detail=question.title, return_block=question.block)


@staff_member_required
@require_POST
def question_action(request, pk, question_pk, action):
    question = get_object_or_404(get_survey(pk).questions, pk=question_pk)
    try:
        services.question_action(pk, question_pk, request.user, action, request.POST.get('revision'))
    except ValidationError as error:
        return failure(request, error, get_survey(pk))
    return redirect('surveys_builder:block_content', pk=pk, block_pk=question.block_id)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def survey_delete(request, pk):
    survey = get_survey(pk)
    if request.method == 'POST':
        try:
            action = request.POST.get('action', 'delete')
            if action == 'archive':
                services.change_status(pk, request.user, action, request.POST.get('revision'))
                messages.success(request, 'Опрос перемещён в архив. Содержимое и ответы сохранены.')
            elif action == 'delete':
                services.delete_survey(pk, request.user, request.POST.get('revision'), allow_published=True)
                messages.success(request, 'Черновик блока удалён.' if survey.is_block_template else 'Опрос, назначения и все ответы удалены.')
            else:
                raise ValidationError('Неизвестное действие.')
            return redirect('surveys_builder:block_templates' if survey.is_block_template else 'staff_surveys_list')
        except ValidationError as error:
            return failure(request, error, survey)
    counts = survey.assignments.aggregate(
        recipients=Count('pk'), responses=Count('response'),
        submitted=Count('response', filter=Q(response__status=SurveyResponse.Status.SUBMITTED)),
    )
    return page(request, 'survey_delete.html', survey, counts=counts)


@staff_member_required
@require_http_methods(['GET'])
def preview(request, pk):
    survey = get_survey(pk)
    questions = list(survey.questions.select_related('block').prefetch_related('options'))
    errors = services.publication_errors(survey) if survey.is_draft and not survey.is_block_template else []
    recipients = services.audience_for(survey) if survey.is_draft and not survey.is_block_template and not errors else None
    ids = list(recipients.values_list('pk', flat=True)) if recipients is not None else []
    token = services.publish_token(survey, request.user, ids) if recipients is not None and not errors else None
    recipient_page = Paginator(recipients, 50).get_page(request.GET.get('page')) if recipients is not None else None
    recipient_rows = [(student_name(u), u.get_username()) for u in recipient_page] if recipient_page is not None else []
    return page(request, 'preview.html', survey, errors=errors, publish_token=token,
                sections=group_questions(questions, survey.blocks.prefetch_related('text_sections')),
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
    return redirect('surveys_builder:survey_detail', pk=survey.pk)


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
    assignment = response.assignment
    values = {a.question_id: a.value for a in response.answers.all()}
    sections = group_questions(assignment_questions(assignment), assignment_blocks(assignment))
    for section in sections:
        for item in section['items']:
            if item['type'] == 'question':
                item['value'] = services.answer_display(item['question'], values.get(item['question'].pk))
    return sections


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
            snapshot = response.assignment.recipient_display
            row = [response.pk, snapshot.get('name', ''), snapshot.get('username', ''), response.submitted_at.isoformat()]
            row.extend(services.answer_display(q, values.get(q.pk)) for q in questions)
            yield writer.writerow([csv_cell(v) for v in row])

    result = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
    result['Content-Disposition'] = f'attachment; filename="survey-{survey.pk}-results.csv"'
    return result


@staff_member_required
@require_http_methods(['GET', 'POST'])
def block_content(request, pk, block_pk):
    survey = get_survey(pk)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    form = QuestionsBuilderForm(survey=survey, initial={'block': block.pk})
    form.fields['block'].widget = forms.HiddenInput()
    if request.method == 'POST':
        data = request.POST.copy()
        data['block'] = block.pk
        try:
            services.save_question(pk, request.user, data, request.POST.get('revision'))
            messages.success(request, 'Вопрос сохранён. Можно добавить следующий или продолжить создание опроса.')
            return redirect('surveys_builder:block_content', pk=pk, block_pk=block.pk)
        except services.InvalidForm as error:
            form = error.form
            form.fields['block'].widget = forms.HiddenInput()
        except ValidationError as error:
            return failure(request, error, survey)
    blocks = list(survey.blocks.prefetch_related('questions', 'text_sections'))
    block_index = next(i for i, candidate in enumerate(blocks) if candidate.pk == block.pk)
    empty_blocks = [candidate for candidate in blocks if not candidate.questions.all() and not candidate.text_sections.all()]
    return page(request, 'block_content.html', survey, edited_block=block, form=form,
                items=block_items(block, block.questions.prefetch_related('options')),
                block_number=block_index + 1, block_count=len(blocks), empty_blocks=empty_blocks,
                structure_ready=not empty_blocks, can_add_block=len(blocks) < 50,
                next_block=blocks[block_index + 1] if block_index + 1 < len(blocks) else None)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def text_section_edit(request, pk, block_pk, section_pk=None):
    survey = get_survey(pk)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    section = get_object_or_404(block.text_sections, pk=section_pk) if section_pk else None
    form = BlockTextSectionForm(instance=section)
    if request.method == 'POST':
        data, files = request.POST, request.FILES
        if request.POST.get('image_action') == 'remove':
            # Без JavaScript кнопка удаляет фото при сохранении остальных полей.
            data, files = request.POST.copy(), request.FILES.copy()
            data['remove_image'] = True
            files.pop('image', None)
        elif request.FILES.get('image') and request.POST.get('remove_image'):
            # Новое фото заменяет старое даже после ранее выбранного удаления.
            data = request.POST.copy()
            data['remove_image'] = False
        try:
            services.save_text_section(pk, block_pk, request.user, data, files,
                                      request.POST.get('revision'), section_pk)
            messages.success(request, 'Текстовая секция сохранена.')
            return redirect('surveys_builder:block_content', pk=pk, block_pk=block_pk)
        except services.InvalidForm as error:
            form = error.form
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'text_section_edit.html', survey, edited_block=block, text_section=section, form=form)


@staff_member_required
@require_POST
def text_section_action(request, pk, block_pk, section_pk, action):
    try:
        services.text_section_action(pk, block_pk, section_pk, request.user, action, request.POST.get('revision'))
    except ValidationError as error:
        return failure(request, error, get_survey(pk))
    return redirect('surveys_builder:block_content', pk=pk, block_pk=block_pk)


@staff_member_required
@require_http_methods(['GET'])
def block_templates(request):
    templates = SurveysBuilder.objects.filter(is_block_template=True).prefetch_related('blocks')
    return page(request, 'block_templates.html', templates=templates)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def block_template_create(request):
    form = QuestionBlockForm()
    if request.method == 'POST':
        try:
            template = services.create_block_template(request.user, request.POST)
            return redirect('surveys_builder:block_content', pk=template.pk, block_pk=template.blocks.get().pk)
        except services.InvalidForm as error:
            form = error.form
    return page(request, 'block_template_create.html', form=form)


@staff_member_required
@require_POST
def save_block_template(request, pk, block_pk):
    try:
        services.create_block_template(request.user, {}, source_pk=pk, block_pk=block_pk, revision=request.POST.get('revision'))
    except ValidationError as error:
        return failure(request, error, get_survey(pk))
    messages.success(request, 'Копия блока добавлена в шаблоны. Продолжайте создание опроса.')
    return redirect('surveys_builder:block_content', pk=pk, block_pk=block_pk)


@staff_member_required
@require_http_methods(['GET', 'POST'])
def import_block_template(request, pk):
    survey = get_survey(pk)
    if request.method == 'POST':
        try:
            try:
                template_pk = int(request.POST.get('template_id', ''))
            except (ValueError, TypeError):
                raise ValidationError('Выберите черновик блока.')
            block = services.import_block_template(pk, request.user, template_pk, request.POST.get('revision'))
            messages.success(request, 'Шаблон скопирован. Настройте название, описание и видимость блока, затем перейдите к вопросам.')
            return redirect('surveys_builder:block_edit', pk=pk, block_pk=block.pk)
        except ValidationError as error:
            return failure(request, error, survey)
    return page(request, 'import_block_template.html', survey,
                templates=SurveysBuilder.objects.filter(is_block_template=True))


@login_required
@require_http_methods(['GET'])
def section_image(request, section_pk):
    section = get_object_or_404(BlockTextSection.objects.select_related('block__survey'), pk=section_pk)
    if not request.user.is_active or not section.image:
        raise Http404
    if not request.user.is_staff:
        assignment = SurveyAssignment.objects.filter(user=request.user, survey=section.block.survey).exclude(
            survey__status=SurveysBuilder.Status.DRAFT).first()
        if not assignment or section.block_id not in assignment.visible_block_ids:
            raise Http404
    try:
        file = section.image.open('rb')
    except FileNotFoundError:
        raise Http404
    response = FileResponse(file, content_type=mimetypes.guess_type(section.image.name)[0] or 'application/octet-stream')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
