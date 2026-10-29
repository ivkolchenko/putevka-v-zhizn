"""JSON API поверх общих сервисов. Session auth и стандартная CSRF-защита Django."""
import json
from functools import wraps

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404

from my_study.models import Course, School, Subject

from . import services
from .api_serializers import (block_data, question_data, section_data, recipient_data, response_data, submitted_response_data,
                              survey_data)
from .forms import AudienceForm, BlockVisibilityForm
from .models import QuestionsBuilder, SurveyAssignment, SurveyResponse, SurveysBuilder
from .selectors import assignment_questions, resolve_audience


class ApiError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def error_response(code, messages, status, fields=None):
    return JsonResponse({'error': {'code': code, 'messages': messages, 'fields': fields or {}}}, status=status)


def endpoint(*methods, staff=True):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                if not request.user.is_authenticated:
                    raise ApiError('unauthenticated', 'Войдите в аккаунт.', 401)
                if not request.user.is_active or (staff and not request.user.is_staff):
                    raise PermissionDenied('Недостаточно прав.')
                if request.method not in methods:
                    response = error_response('method_not_allowed', ['Недопустимый HTTP-метод.'], 405)
                    response['Allow'] = ', '.join(methods)
                else:
                    response = view(request, *args, **kwargs)
            except ApiError as error:
                response = error_response(error.code, [error.message], error.status)
            except services.InvalidForm as error:
                fields = error.form.errors.get_json_data()
                response = error_response('validation_error', ['Проверьте поля запроса.'], 400, fields)
            except services.RevisionConflict as error:
                response = error_response('revision_conflict', error.messages, 409)
            except ValidationError as error:
                response = error_response('validation_error', error.messages, 400)
            except PermissionDenied:
                response = error_response('forbidden', ['Недостаточно прав.'], 403)
            except Http404:
                response = error_response('not_found', ['Объект не найден.'], 404)
            response['Cache-Control'] = 'no-store'
            return response
        return wrapped
    return decorate


def payload(request, allowed):
    if request.content_type != 'application/json':
        raise ApiError('unsupported_media_type', 'Используйте Content-Type: application/json.', 415)
    try:
        def invalid_constant(value):
            raise ValueError(value)
        data = json.loads(request.body, parse_constant=invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApiError('invalid_json', 'Некорректный JSON.')
    if not isinstance(data, dict):
        raise ApiError('invalid_json', 'Ожидается JSON-объект.')
    unknown = set(data) - set(allowed)
    if unknown:
        raise ApiError('unknown_fields', 'Неизвестные поля: ' + ', '.join(sorted(unknown)))
    return data


def revision(data):
    value = data.get('revision')
    if type(value) is not int or value < 0:
        raise ApiError('invalid_revision', 'Передайте целое неотрицательное поле revision.')
    return value


def text_fields(data, names, *, nullable=()):
    for name in names:
        if name in data and not isinstance(data[name], str) and not (name in nullable and data[name] is None):
            raise ApiError('invalid_type', f'{name}: ожидается строка.')


def pagination(request, queryset, serialize):
    try:
        number = int(request.GET.get('page', 1))
        size = int(request.GET.get('page_size', 50))
        if number < 1 or not 1 <= size <= 100:
            raise ValueError
    except ValueError:
        raise ApiError('invalid_pagination', 'page >= 1; page_size от 1 до 100.')
    paginator = Paginator(queryset, size)
    try:
        page = paginator.page(number)
    except EmptyPage:
        raise Http404
    return {'count': paginator.count, 'page': page.number, 'page_size': size,
            'pages': paginator.num_pages, 'results': [serialize(item) for item in page]}


def get_survey(pk):
    return get_object_or_404(SurveysBuilder, pk=pk)


def audience_input(data):
    form = AudienceForm()
    unknown = set(data) - set(form.fields)
    if unknown:
        raise ApiError('unknown_fields', 'Неизвестные поля аудитории: ' + ', '.join(sorted(unknown)))
    for name, value in data.items():
        field = form.fields[name]
        if isinstance(field, (forms.MultipleChoiceField, forms.ModelMultipleChoiceField)):
            if not isinstance(value, list) or any(type(v) not in (int, str) for v in value):
                raise ApiError('invalid_type', f'{name}: ожидается массив строк или ID.')
        elif name == 'deadline' and (value is None or isinstance(value, str)):
            continue
        elif not isinstance(value, str):
            raise ApiError('invalid_type', f'{name}: ожидается строка.')
    return {'mode': 'filters', **data}


@endpoint('GET')
def metadata(request):
    form = AudienceForm()
    choices = {name: [{'value': value, 'label': str(label)} for value, label in field.choices]
               for name, field in form.fields.items()
               if isinstance(field, forms.ChoiceField) and not isinstance(field, forms.ModelChoiceField)}
    return JsonResponse({
        'csrf_token': get_token(request), 'audience_choices': choices,
        'audience_filter_fields': list(AudienceForm.FILTER_FIELDS),
        'block_visibility_filter_fields': list(BlockVisibilityForm.FILTER_FIELDS),
        'recipient_requirements': {'active': True, 'registration_complete': True},
        'answer_types': [{'value': v, 'label': str(label)} for v, label in QuestionsBuilder.AnswerType.choices],
        'survey_statuses': [{'value': v, 'label': str(label)} for v, label in SurveysBuilder.Status.choices],
        'limits': {'blocks': 50, 'questions': 100, 'options_min': 2, 'options_max': 50, 'page_size_max': 100},
    })


@endpoint('GET')
def lookups(request, kind):
    query = request.GET.get('q', '').strip()
    if kind == 'courses':
        queryset = Course.objects.select_related('school', 'subject').filter(
            Q(title__icontains=query) | Q(school__name__icontains=query)).order_by('school__name', 'title', 'pk')
        serialize = lambda c: {'id': c.pk, 'title': c.title, 'school_id': c.school_id,
                               'subject_id': c.subject_id, 'label': str(c)}
    elif kind in ('schools', 'subjects'):
        model = School if kind == 'schools' else Subject
        queryset = model.objects.filter(name__icontains=query).order_by('name', 'pk')
        serialize = lambda item: {'id': item.pk, 'name': item.name}
    else:
        raise Http404
    return JsonResponse(pagination(request, queryset, serialize))


@endpoint('GET')
def recipients(request):
    form = AudienceForm()
    data = {name: request.GET.getlist(name) if isinstance(form.fields.get(name),
            (forms.MultipleChoiceField, forms.ModelMultipleChoiceField)) else request.GET[name]
            for name in request.GET if name not in ('page', 'page_size')}
    form = AudienceForm(audience_input(data))
    if not form.is_valid():
        raise services.InvalidForm(form)
    return JsonResponse(pagination(request, resolve_audience(form.cleaned_data), recipient_data))


@endpoint('POST')
def audience_preview(request):
    form = AudienceForm(audience_input(payload(request, AudienceForm.base_fields)))
    if not form.is_valid():
        raise services.InvalidForm(form)
    return JsonResponse(pagination(request, resolve_audience(form.cleaned_data), recipient_data))


@endpoint('GET', 'POST')
def surveys(request):
    if request.method == 'POST':
        data = payload(request, ('title', 'description'))
        text_fields(data, ('title', 'description'))
        survey = services.create_survey(request.user, data)
        return JsonResponse(survey_data(survey, detail=True), status=201)
    queryset = SurveysBuilder.objects.filter(is_block_template=False)
    status = request.GET.get('status')
    if status:
        if status not in SurveysBuilder.Status.values:
            raise ApiError('invalid_status', 'Неизвестный статус опроса.')
        queryset = queryset.filter(status=status)
    if request.GET.get('q'):
        queryset = queryset.filter(title__icontains=request.GET['q'])
    return JsonResponse(pagination(request, queryset, survey_data))


@endpoint('GET', 'PATCH', 'DELETE')
def survey_detail(request, pk):
    survey = get_survey(pk)
    if request.method == 'DELETE':
        data = payload(request, ('revision',))
        services.delete_survey(pk, request.user, revision(data))
        return HttpResponse(status=204)
    if request.method == 'PATCH':
        data = payload(request, ('title', 'description', 'revision'))
        expected = revision(data)
        text_fields(data, ('title', 'description'))
        values = {name: data.get(name, getattr(survey, name)) for name in ('title', 'description')}
        survey = services.update_survey(pk, request.user, values, expected)
    return JsonResponse(survey_data(survey, detail=True))


@endpoint('GET', 'PUT')
@transaction.atomic
def audience(request, pk):
    survey = get_survey(pk)
    if request.method == 'PUT':
        data = payload(request, (*AudienceForm.base_fields, 'revision'))
        expected = revision(data)
        data.pop('revision')
        survey = services.save_audience(pk, request.user, audience_input(data), expected)
    if survey.is_draft:
        users = services.audience_for(survey) if survey.audience_configured else []
        recipients_page = pagination(request, users, recipient_data)
    else:
        recipients_page = pagination(request, survey.assignments.all(),
            lambda a: {'id': a.user_id, **a.recipient_display})
    return JsonResponse({'revision': survey.revision, 'configured': survey.audience_configured,
                         'selection': AudienceForm.saved_data(survey), 'recipients': recipients_page})


def question_input(data, question=None):
    text_fields(data, ('title', 'description', 'answer_type'))
    if 'required' in data and type(data['required']) is not bool:
        raise ApiError('invalid_type', 'required: ожидается boolean.')
    options = data.get('options', [o.text for o in question.options.all()] if question else [])
    if (not isinstance(options, list) or len(options) > 50
            or any(not isinstance(o, str) or not o.strip() or len(o) > 200 or '\n' in o or '\r' in o for o in options)):
        raise ApiError('invalid_options', 'options: до 50 непустых строк, каждая до 200 символов без переноса строки.')
    values = {name: data.get(name, getattr(question, name) if question else default)
              for name, default in [('title', ''), ('description', ''), ('answer_type', 'short_text'), ('required', False)]}
    values['options_text'] = '\n'.join(options)
    block_id = data.get('block_id', question.block_id if question else None)
    if type(block_id) is not int or block_id < 1:
        raise ApiError('invalid_type', 'block_id: выберите блок этого опроса (положительный целый ID).')
    values['block'] = block_id if block_id is not None else ''
    return values


QUESTION_FIELDS = ('title', 'description', 'answer_type', 'required', 'options', 'block_id', 'revision')


@endpoint('GET', 'POST')
def blocks(request, pk):
    if request.method == 'POST':
        data = payload(request, ('title', 'description', 'visibility_filters', 'revision'))
        text_fields(data, ('title', 'description'))
        block = services.save_block(pk, request.user, data, revision(data))
        return JsonResponse({'revision': block.survey.revision, 'block': block_data(block)}, status=201)
    survey = get_survey(pk)
    return JsonResponse({'revision': survey.revision, 'blocks': [block_data(b) for b in survey.blocks.all()]})


@endpoint('GET', 'PATCH', 'DELETE')
def block_detail(request, pk, block_pk):
    survey = get_survey(pk)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    if request.method == 'GET':
        return JsonResponse({'revision': survey.revision, 'block': block_data(block)})
    data = payload(request, ('revision',) if request.method == 'DELETE' else ('title', 'description', 'visibility_filters', 'revision'))
    expected = revision(data)
    if request.method == 'DELETE':
        survey = services.block_action(pk, block_pk, request.user, 'delete', expected)
        return JsonResponse(survey_data(survey, detail=True))
    text_fields(data, ('title', 'description'))
    values = {name: data.get(name, getattr(block, name)) for name in ('title', 'description', 'visibility_filters')}
    block = services.save_block(pk, request.user, values, expected, block_pk)
    return JsonResponse({'revision': block.survey.revision, 'block': block_data(block)})


@endpoint('PUT')
def block_reorder(request, pk):
    data = payload(request, ('revision', 'block_ids'))
    ids = data.get('block_ids')
    if not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
        raise ApiError('invalid_type', 'block_ids: ожидается массив целых ID блоков.')
    survey = services.reorder_blocks(pk, request.user, ids, revision(data))
    return JsonResponse(survey_data(survey, detail=True))


@endpoint('POST')
def questions(request, pk):
    data = payload(request, QUESTION_FIELDS)
    question = services.save_question(pk, request.user, question_input(data), revision(data))
    return JsonResponse({'revision': question.survey.revision, 'question': question_data(question)}, status=201)


@endpoint('PATCH', 'DELETE')
def question_detail(request, pk, question_pk):
    survey = get_survey(pk)
    question = get_object_or_404(survey.questions, pk=question_pk)
    data = payload(request, ('revision',) if request.method == 'DELETE' else QUESTION_FIELDS)
    expected = revision(data)
    if request.method == 'DELETE':
        services.question_action(pk, question_pk, request.user, 'delete', expected)
        survey.refresh_from_db()
        return JsonResponse({'revision': survey.revision})
    question = services.save_question(pk, request.user, question_input(data, question), expected, question_pk)
    return JsonResponse({'revision': question.survey.revision, 'question': question_data(question)})


@endpoint('PUT')
def reorder(request, pk):
    data = payload(request, ('revision', 'question_ids'))
    ids = data.get('question_ids')
    if not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
        raise ApiError('invalid_type', 'question_ids: ожидается массив целых ID вопросов.')
    survey = services.reorder_questions(pk, request.user, ids, revision(data))
    return JsonResponse(survey_data(survey, detail=True))


@endpoint('POST')
def duplicate_question(request, pk, question_pk):
    data = payload(request, ('revision',))
    services.question_action(pk, question_pk, request.user, 'duplicate', revision(data))
    return JsonResponse(survey_data(get_survey(pk), detail=True), status=201)


@endpoint('GET')
@transaction.atomic
def preview(request, pk):
    survey = services.locked_survey(pk, request.user)
    if not survey.is_draft:
        raise ApiError('invalid_state', 'Предпросмотр публикации доступен только для черновика.', 409)
    errors = services.publication_errors(survey)
    users = services.audience_for(survey) if not errors else []
    ids = list(users.values_list('pk', flat=True)) if not errors else []
    return JsonResponse({'survey': survey_data(survey, detail=True), 'errors': errors,
                         'recipients': pagination(request, users, recipient_data),
                         'publish_token': services.publish_token(survey, request.user, ids) if not errors else None})


@endpoint('POST')
def publish(request, pk):
    data = payload(request, ('token',))
    text_fields(data, ('token',))
    if not data.get('token'):
        raise ApiError('missing_token', 'Передайте token из предпросмотра публикации.')
    return JsonResponse(survey_data(services.publish_survey(pk, request.user, data['token']), detail=True))


@endpoint('POST')
def copy(request, pk):
    payload(request, ())
    return JsonResponse(survey_data(services.copy_survey(pk, request.user), detail=True), status=201)


@endpoint('POST')
def lifecycle(request, pk, action):
    data = payload(request, ('revision',))
    return JsonResponse(survey_data(services.change_status(pk, request.user, action, revision(data)), detail=True))


@endpoint('GET')
def results(request, pk):
    survey = get_survey(pk)
    assignments = survey.assignments.select_related('response')
    counts = assignments.aggregate(total=Count('pk'),
        submitted=Count('pk', filter=Q(response__status=SurveyResponse.Status.SUBMITTED)),
        started=Count('pk', filter=Q(response__status=SurveyResponse.Status.DRAFT)))
    counts['not_started'] = counts['total'] - counts['submitted'] - counts['started']
    def assignment_data(assignment):
        response = getattr(assignment, 'response', None)
        submitted = response and response.status == SurveyResponse.Status.SUBMITTED
        return {'id': assignment.pk, 'user_id': assignment.user_id,
                'recipient': assignment.recipient_display,
                'response_status': response.status if response else 'not_started',
                'response_id': response.pk if submitted else None,
                'submitted_at': response.submitted_at if submitted else None}
    return JsonResponse({'counts': counts, 'recipients': pagination(request, assignments, assignment_data)})


@endpoint('GET')
def response_detail(request, pk, response_pk):
    response = get_object_or_404(SurveyResponse.objects.select_related('assignment'),
        pk=response_pk, assignment__survey_id=pk, status=SurveyResponse.Status.SUBMITTED)
    return JsonResponse(submitted_response_data(response))


@endpoint('GET')
def export(request, pk):
    from .views import results_csv
    return results_csv(request, pk)


def my_assignment(pk, actor):
    return get_object_or_404(SurveyAssignment.objects.select_related('survey', 'response'),
        pk=pk, user=actor, survey__status__in=[SurveysBuilder.Status.PUBLISHED,
                                            SurveysBuilder.Status.CLOSED, SurveysBuilder.Status.ARCHIVED])


@endpoint('GET', staff=False)
def my_surveys(request):
    assignments = SurveyAssignment.objects.filter(user=request.user).exclude(
        survey__status=SurveysBuilder.Status.DRAFT).select_related('survey', 'response').order_by('-assigned_at', '-pk')
    data = pagination(request, assignments, lambda a: {
        'id': a.pk, 'survey': survey_data(a.survey, staff=False),
        'response_status': a.response.status if hasattr(a, 'response') else 'not_started',
    })
    data['csrf_token'] = get_token(request)
    return JsonResponse(data)


@endpoint('GET', staff=False)
def my_survey_detail(request, pk):
    assignment = my_assignment(pk, request.user)
    return JsonResponse({'id': assignment.pk, 'survey': survey_data(assignment.survey, detail=True, staff=False, block_ids=assignment.visible_block_ids),
                         'response': response_data(getattr(assignment, 'response', None))})


@endpoint('PUT', staff=False)
def my_response(request, pk):
    data = payload(request, ('revision', 'answers', 'final'))
    expected = revision(data)
    final = data.get('final', False)
    if type(final) is not bool or not isinstance(data.get('answers'), dict):
        raise ApiError('invalid_type', 'Передайте answers как объект и final как boolean.')
    assignment = my_assignment(pk, request.user)
    questions = {str(q.pk): q for q in assignment_questions(assignment)}
    values = {}
    for key, value in data['answers'].items():
        if key not in questions:
            raise ApiError('invalid_question', 'Ответ содержит вопрос из другого опроса.')
        kind = questions[key].answer_type
        if value is None:
            pass
        elif kind == QuestionsBuilder.AnswerType.MULTIPLE_CHOICE:
            if not isinstance(value, list) or any(type(v) not in (int, str) for v in value):
                raise ApiError('invalid_type', f'{key}: ожидается массив ID вариантов.')
        elif kind == QuestionsBuilder.AnswerType.NUMBER:
            if type(value) not in (int, float, str):
                raise ApiError('invalid_type', f'{key}: ожидается число или его строковое представление.')
        elif kind in (QuestionsBuilder.AnswerType.SINGLE_CHOICE, QuestionsBuilder.AnswerType.SCALE):
            if type(value) not in (int, str):
                raise ApiError('invalid_type', f'{key}: ожидается целое число или строка.')
        elif not isinstance(value, str):
            raise ApiError('invalid_type', f'{key}: ожидается строка.')
        values['q_' + key] = value
    response = services.save_response(pk, request.user, values, final=final, revision=expected)
    return JsonResponse(response_data(response))


@endpoint('GET', 'POST')
def block_templates(request):
    if request.method == 'POST':
        data = payload(request, ('title', 'description', 'visibility_filters'))
        text_fields(data, ('title', 'description'))
        template = services.create_block_template(request.user, data)
        return JsonResponse(survey_data(template, detail=True), status=201)
    templates = SurveysBuilder.objects.filter(is_block_template=True)
    return JsonResponse(pagination(request, templates, lambda survey: survey_data(survey, detail=True)))


@endpoint('POST')
def save_block_template(request, pk, block_pk):
    data = payload(request, ('revision',))
    template = services.create_block_template(request.user, {}, source_pk=pk, block_pk=block_pk, revision=revision(data))
    return JsonResponse(survey_data(template, detail=True), status=201)


@endpoint('POST')
def import_block_template(request, pk):
    data = payload(request, ('revision', 'template_id'))
    template_id = data.get('template_id')
    if type(template_id) is not int or template_id < 1:
        raise ApiError('invalid_type', 'template_id: ожидается положительный целый ID.')
    block = services.import_block_template(pk, request.user, template_id, revision(data))
    return JsonResponse({'revision': block.survey.revision, 'block': block_data(block)}, status=201)


def section_payload(request, section=None):
    if request.content_type == 'multipart/form-data' and request.method == 'POST':
        data = request.POST.dict()
        if set(data) - {'title', 'text', 'image_alt', 'revision', 'remove_image'} or set(request.FILES) - {'image'}:
            raise ApiError('unknown_fields', 'Неизвестные поля текстовой секции.')
        try:
            data['revision'] = int(data.get('revision', ''))
        except (TypeError, ValueError):
            raise ApiError('invalid_revision', 'Передайте целое поле revision.')
    else:
        data = payload(request, ('title', 'text', 'image_alt', 'revision', 'remove_image'))
        if 'remove_image' in data:
            if type(data['remove_image']) is not bool:
                raise ApiError('invalid_type', 'remove_image: ожидается boolean.')
    expected = revision(data)
    text_fields(data, ('title', 'text', 'image_alt'))
    if section:
        for name in ('title', 'text', 'image_alt'):
            data.setdefault(name, getattr(section, name))
    return data, expected


@endpoint('POST')
def text_sections(request, pk, block_pk):
    data, expected = section_payload(request)
    section = services.save_text_section(pk, block_pk, request.user, data, request.FILES, expected)
    return JsonResponse({'revision': section.block.survey.revision, 'section': section_data(section)}, status=201)


@endpoint('GET', 'POST', 'PATCH', 'DELETE')
def text_section_detail(request, pk, block_pk, section_pk):
    block = get_object_or_404(get_survey(pk).blocks, pk=block_pk)
    section = get_object_or_404(block.text_sections, pk=section_pk)
    if request.method == 'GET':
        return JsonResponse({'revision': block.survey.revision, 'section': section_data(section)})
    if request.method == 'DELETE':
        data = payload(request, ('revision',))
        survey = services.text_section_action(pk, block_pk, section_pk, request.user, 'delete', revision(data))
        return JsonResponse({'revision': survey.revision})
    data, expected = section_payload(request, section)
    section = services.save_text_section(pk, block_pk, request.user, data, request.FILES, expected, section_pk)
    return JsonResponse({'revision': section.block.survey.revision, 'section': section_data(section)})


@endpoint('POST')
def text_section_action(request, pk, block_pk, section_pk, action):
    data = payload(request, ('revision',))
    survey = services.text_section_action(pk, block_pk, section_pk, request.user, action, revision(data))
    return JsonResponse(survey_data(survey, detail=True))
