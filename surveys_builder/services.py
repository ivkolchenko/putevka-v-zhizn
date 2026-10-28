"""Все изменения выполняются здесь: права, этапы, транзакции и версии записей."""
import hashlib

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Max
from django.shortcuts import get_object_or_404
from django.utils import timezone

from core.models import Notification, UserNotification

from .forms import AudienceForm, QuestionBlockForm, QuestionsBuilderForm, ResponseForm, SurveysBuilderForm
from .models import QuestionBlock, QuestionOptions, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder
from .selectors import resolve_audience, student_name


class InvalidForm(Exception):
    def __init__(self, form):
        self.form = form
        super().__init__('Проверьте поля формы.')


class RevisionConflict(ValidationError):
    """Клиент пытается перезаписать уже изменённые данные."""


def require_staff(actor):
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        raise PermissionDenied('Доступ только сотрудникам.')


def locked_survey(pk, actor):
    require_staff(actor)
    return get_object_or_404(SurveysBuilder.objects.select_for_update(), pk=pk)


def check_revision(obj, expected):
    try:
        matches = int(expected) == obj.revision
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise RevisionConflict('Данные уже изменились. Обновите страницу и повторите действие.')


def require_draft(survey, expected):
    check_revision(survey, expected)
    if not survey.is_draft:
        raise ValidationError('Опубликованный опросник менять нельзя. Создайте его копию.')


def changed(survey):
    survey.revision += 1
    survey.save()


@transaction.atomic
def create_survey(actor, data):
    require_staff(actor)
    form = SurveysBuilderForm(data)
    if not form.is_valid():
        raise InvalidForm(form)
    survey = form.save(commit=False)
    survey.author = actor
    survey.save()
    return survey


@transaction.atomic
def update_survey(pk, actor, data, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    form = SurveysBuilderForm(data, instance=survey)
    if not form.is_valid():
        raise InvalidForm(form)
    form.save(commit=False)
    changed(survey)
    return survey


@transaction.atomic
def save_audience(pk, actor, data, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    form = AudienceForm(data)
    if not form.is_valid():
        raise InvalidForm(form)
    if not resolve_audience(form.cleaned_data).exists():
        form.add_error(None, 'Не найдено получателей. Измените условия или список учеников.')
        raise InvalidForm(form)
    survey.audience_mode = form.cleaned_data['mode']
    survey.audience_filters = form.serialized_filters()
    survey.included_users.set(form.cleaned_data['included_users'])
    survey.excluded_users.set(form.cleaned_data['excluded_users'])
    survey.audience_configured = True
    changed(survey)
    return survey


def audience_for(survey):
    if not survey.audience_configured:
        raise ValidationError('Сначала сохраните получателей на первом этапе.')
    form = AudienceForm(AudienceForm.saved_data(survey))
    if not form.is_valid():
        raise ValidationError('Состав справочников изменился. Проверьте и заново сохраните получателей.')
    return resolve_audience(form.cleaned_data)


def normalize_question_positions(survey):
    questions = list(survey.questions.select_related('block'))
    questions.sort(key=lambda q: (q.block.position if q.block_id else 0,
                                 q.block_id or 0, q.position, q.pk))
    for position, question in enumerate(questions, 1):
        question.position = position
    QuestionsBuilder.objects.bulk_update(questions, ['position'])
    return {q.pk: q.position for q in questions}


@transaction.atomic
def save_block(pk, actor, data, revision, block_pk=None):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    if block_pk is None:
        if survey.blocks.count() >= 50:
            raise ValidationError('Допускается не более 50 блоков.')
        block = QuestionBlock(survey=survey, position=(survey.blocks.aggregate(last=Max('position'))['last'] or 0) + 1)
    else:
        block = get_object_or_404(survey.blocks, pk=block_pk)
    form = QuestionBlockForm(data, instance=block)
    if not form.is_valid():
        raise InvalidForm(form)
    block = form.save()
    changed(survey)
    return block


def write_block_order(survey, blocks):
    for position, block in enumerate(blocks, 1):
        block.position = position
    QuestionBlock.objects.bulk_update(blocks, ['position'])
    normalize_question_positions(survey)
    changed(survey)


@transaction.atomic
def block_action(pk, block_pk, actor, action, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    blocks = list(survey.blocks.all())
    index = next(i for i, item in enumerate(blocks) if item.pk == block.pk)
    if action == 'delete':
        last = survey.questions.aggregate(last=Max('position'))['last'] or 0
        block.questions.update(block=None, position=F('position') + last)
        block.delete()
        blocks.pop(index)
    elif action in ('up', 'down'):
        target = index + (-1 if action == 'up' else 1)
        if 0 <= target < len(blocks):
            blocks[index], blocks[target] = blocks[target], blocks[index]
    else:
        raise ValidationError('Неизвестное действие с блоком.')
    write_block_order(survey, blocks)
    return survey


@transaction.atomic
def reorder_blocks(pk, actor, block_ids, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    blocks = {block.pk: block for block in survey.blocks.all()}
    if len(block_ids) != len(blocks) or set(block_ids) != set(blocks):
        raise ValidationError('Передайте все блоки этого опроса ровно по одному разу.')
    write_block_order(survey, [blocks[pk] for pk in block_ids])
    return survey


@transaction.atomic
def save_question(pk, actor, data, revision, question_pk=None):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    if not survey.audience_configured:
        raise ValidationError('Сначала выберите получателей.')
    if question_pk is None:
        if survey.questions.count() >= 100:
            raise ValidationError('Допускается не более 100 вопросов.')
        question = QuestionsBuilder(survey=survey)
        question.position = (survey.questions.aggregate(last=Max('position'))['last'] or 0) + 1
    else:
        question = get_object_or_404(survey.questions, pk=question_pk)
        if 'block' not in data:
            data = data.copy()
            data['block'] = question.block_id or ''
    original_block = question.block_id
    form = QuestionsBuilderForm(data, instance=question)
    if not form.is_valid():
        raise InvalidForm(form)
    question = form.save(commit=False)
    if question_pk is not None and question.block_id != original_block:
        question.position = (survey.questions.aggregate(last=Max('position'))['last'] or 0) + 1
    question.save()
    form.save_options(question)
    question.position = normalize_question_positions(survey)[question.pk]
    changed(survey)
    return question


@transaction.atomic
def question_action(pk, question_pk, actor, action, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    question = get_object_or_404(survey.questions, pk=question_pk)
    questions = list(survey.questions.filter(block_id=question.block_id))
    index = next(i for i, q in enumerate(questions) if q.pk == question.pk)
    if action == 'delete':
        question.delete()
        questions.pop(index)
    elif action == 'duplicate':
        if survey.questions.count() >= 100:
            raise ValidationError('Допускается не более 100 вопросов.')
        copy = QuestionsBuilder.objects.create(survey=survey, block=question.block, title=question.title, description=question.description,
                                               answer_type=question.answer_type, required=question.required, position=index + 1)
        QuestionOptions.objects.bulk_create([
            QuestionOptions(question=copy, text=o.text, position=o.position) for o in question.options.all()
        ])
        questions.insert(index + 1, copy)
    elif action in ('up', 'down'):
        target = index + (-1 if action == 'up' else 1)
        if 0 <= target < len(questions):
            questions[index], questions[target] = questions[target], questions[index]
    else:
        raise ValidationError('Неизвестное действие с вопросом.')
    for position, item in enumerate(questions, 1):
        item.position = position
    QuestionsBuilder.objects.bulk_update(questions, ['position'])
    normalize_question_positions(survey)
    changed(survey)


@transaction.atomic
def reorder_questions(pk, actor, question_ids, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    questions = {q.pk: q for q in survey.questions.all()}
    if len(question_ids) != len(questions) or set(question_ids) != set(questions):
        raise ValidationError('Передайте все вопросы этого опроса ровно по одному разу.')
    ranks = {None: 0, **{block.pk: i for i, block in enumerate(survey.blocks.all(), 1)}}
    order = [ranks[questions[pk].block_id] for pk in question_ids]
    if order != sorted(order):
        raise ValidationError('Вопросы можно переставлять внутри блока. Для перестановки блоков измените их порядок.')
    ordered = []
    for position, question_id in enumerate(question_ids, 1):
        question = questions[question_id]
        question.position = position
        ordered.append(question)
    QuestionsBuilder.objects.bulk_update(ordered, ['position'])
    changed(survey)
    return survey


def publication_errors(survey):
    errors = []
    if not survey.title.strip():
        errors.append('Укажите название опросника.')
    if survey.deadline and survey.deadline <= timezone.now():
        errors.append('Срок ответа уже истёк.')
    blocks = list(survey.blocks.annotate(question_count=Count('questions')))
    block_ids = {block.pk for block in blocks}
    if len(blocks) > 50:
        errors.append('Допускается не более 50 блоков.')
    for block in blocks:
        if not block.title.strip():
            errors.append('Укажите название каждого блока.')
        if not block.question_count:
            errors.append(f'Блок «{block.title}»: добавьте хотя бы один вопрос или удалите блок.')
    try:
        if not audience_for(survey).exists():
            errors.append('В выборке больше нет получателей.')
    except ValidationError as error:
        errors.extend(error.messages)
    questions = list(survey.questions.prefetch_related('options'))
    if not questions:
        errors.append('Добавьте хотя бы один вопрос.')
    if len(questions) > 100:
        errors.append('Допускается не более 100 вопросов.')
    for index, question in enumerate(questions, 1):
        if question.block_id and question.block_id not in block_ids:
            errors.append(f'Вопрос {index}: блок принадлежит другому опросу.')
        if not question.title.strip() or question.answer_type not in QuestionsBuilder.AnswerType.values:
            errors.append(f'Проверьте название и тип вопроса {index}.')
        if question.has_options:
            texts = [o.text.strip() for o in question.options.all()]
            if not 2 <= len(texts) <= 50 or len(texts) != len(set(texts)) or any(not t or len(t) > 200 for t in texts):
                errors.append(f'Вопрос {index}: нужны от 2 до 50 непустых, разных вариантов.')
    return errors


def audience_fingerprint(ids):
    return hashlib.sha256(','.join(map(str, sorted(ids))).encode()).hexdigest()


def publish_token(survey, actor, ids):
    return signing.dumps({'survey': survey.pk, 'actor': actor.pk, 'revision': survey.revision,
                          'audience': audience_fingerprint(ids)}, salt='surveys-builder.publish')


@transaction.atomic
def publish_survey(pk, actor, token):
    survey = locked_survey(pk, actor)
    # Повтор отправки того же действия не создаёт вторых назначений.
    if survey.status == SurveysBuilder.Status.PUBLISHED:
        return survey
    if not survey.is_draft:
        raise ValidationError('Этот опросник нельзя опубликовать повторно.')
    try:
        confirmation = signing.loads(token, salt='surveys-builder.publish', max_age=3600)
    except (signing.BadSignature, TypeError):
        raise ValidationError('Подтверждение публикации устарело. Откройте предпросмотр заново.')
    if confirmation.get('survey') != survey.pk or confirmation.get('actor') != actor.pk:
        raise ValidationError('Неверное подтверждение публикации.')
    check_revision(survey, confirmation.get('revision'))
    errors = publication_errors(survey)
    if errors:
        raise ValidationError(errors)
    users = list(audience_for(survey))
    if confirmation.get('audience') != audience_fingerprint([u.pk for u in users]):
        raise ValidationError('Получатели изменились после предпросмотра. Проверьте список заново.')
    assignments = []
    for user in users:
        info = getattr(user, 'user_info', None)
        assignments.append(SurveyAssignment(survey=survey, user=user, recipient_snapshot={
            'name': student_name(user), 'username': user.get_username(),
            'grade': info.next_year_class_digit if info else None,
            'profile': info.internal_study_profile if info else None,
            'status': info.status if info else None,
        }))
    SurveyAssignment.objects.bulk_create(assignments)
    notification = Notification.objects.create(
        message=f'Вам назначен опрос «{survey.title}». Откройте раздел «Опросы» в личном кабинете.',
        sender=actor,
    )
    UserNotification.objects.bulk_create([
        UserNotification(notification=notification, recipient=user) for user in users
    ])
    survey.status = SurveysBuilder.Status.PUBLISHED
    survey.published_at = timezone.now()
    changed(survey)
    return survey


@transaction.atomic
def change_status(pk, actor, action, revision):
    survey = locked_survey(pk, actor)
    check_revision(survey, revision)
    if action == 'close' and survey.status == SurveysBuilder.Status.PUBLISHED:
        survey.status = SurveysBuilder.Status.CLOSED
        survey.closed_at = timezone.now()
    elif action == 'archive' and survey.status in (SurveysBuilder.Status.PUBLISHED, SurveysBuilder.Status.CLOSED):
        survey.status = SurveysBuilder.Status.ARCHIVED
        survey.closed_at = survey.closed_at or timezone.now()
    else:
        raise ValidationError('Недопустимый переход состояния опросника.')
    changed(survey)
    return survey


@transaction.atomic
def delete_survey(pk, actor, revision, *, allow_published=False):
    survey = locked_survey(pk, actor)
    if allow_published:
        check_revision(survey, revision)
        if survey.assignments.filter(response__isnull=False).exists():
            raise ValidationError('Нельзя удалить опрос с ответами или черновиками. Завершите приём или переместите его в архив.')
        survey.assignments.all().delete()
    else:
        require_draft(survey, revision)
    survey.delete()


@transaction.atomic
def copy_survey(pk, actor):
    source = locked_survey(pk, actor)
    copy = SurveysBuilder.objects.create(
        author=actor, title=source.title[:192] + ' (копия)', description=source.description,
        audience_mode=source.audience_mode, audience_filters=source.audience_filters,
        audience_configured=source.audience_configured,
    )
    copy.included_users.set(source.included_users.all())
    copy.excluded_users.set(source.excluded_users.all())
    blocks = {block.pk: QuestionBlock.objects.create(survey=copy, title=block.title,
        description=block.description, position=block.position) for block in source.blocks.all()}
    for question in source.questions.prefetch_related('options'):
        duplicate = QuestionsBuilder.objects.create(survey=copy, block=blocks.get(question.block_id), title=question.title, description=question.description,
                                                     answer_type=question.answer_type, required=question.required, position=question.position)
        QuestionOptions.objects.bulk_create([
            QuestionOptions(question=duplicate, text=o.text, position=o.position) for o in question.options.all()
        ])
    return copy


@transaction.atomic
def save_response(assignment_pk, actor, data, *, final, revision):
    return write_response(assignment_pk, actor, data, final=final, revision=revision)


@transaction.atomic
def save_response_page(assignment_pk, actor, data, *, final, revision, question_ids, require_page):
    return write_response(
        assignment_pk,
        actor,
        data,
        final=final,
        revision=revision,
        page_question_ids=question_ids,
        require_page=require_page,
    )


def write_response(assignment_pk, actor, data, *, final, revision, page_question_ids=None, require_page=False):
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied
    # Во всех операциях сначала блокируется опрос, затем назначение/ответ.
    assignment = get_object_or_404(SurveyAssignment, pk=assignment_pk, user=actor)
    survey = get_object_or_404(SurveysBuilder.objects.select_for_update(), pk=assignment.survey_id)
    assignment = get_object_or_404(SurveyAssignment.objects.select_for_update(), pk=assignment.pk)
    if not survey.accepts_answers:
        raise ValidationError('Приём ответов завершён или опрос недоступен.')
    response = SurveyResponse.objects.filter(assignment=assignment).first()
    if response and response.status == SurveyResponse.Status.SUBMITTED:
        raise ValidationError('Ответ уже отправлен и не может быть изменён.')
    try:
        expected = int(revision)
    except (TypeError, ValueError):
        raise ValidationError('Обновите страницу перед сохранением ответа.')
    if expected != (response.revision if response else 0):
        raise RevisionConflict('Ответ изменён в другой вкладке. Обновите страницу.')
    questions = list(survey.questions.select_related('block').prefetch_related('options'))
    if page_question_ids is None:
        page_questions = questions
    else:
        selected_ids = set(page_question_ids)
        page_questions = [question for question in questions if question.pk in selected_ids]
        if len(page_questions) != len(selected_ids):
            raise ValidationError('Страница опроса изменилась. Обновите её.')
    form = ResponseForm(data, questions=page_questions, final=final or require_page)
    if not form.is_valid():
        raise InvalidForm(form)
    values = form.answer_values()
    if page_question_ids is not None and final:
        saved_values = {
            answer.question_id: answer.value
            for answer in SurveyAnswer.objects.filter(response=response)
        } if response else {}
        saved_values.update(values)
        complete_form = ResponseForm(
            {f'q_{question_id}': value for question_id, value in saved_values.items()},
            questions=questions,
            final=True,
        )
        if not complete_form.is_valid():
            raise ValidationError('Заполните обязательные вопросы на предыдущих страницах опроса.')
        values = complete_form.answer_values()
        page_questions = questions
    if response is None:
        response = SurveyResponse.objects.create(assignment=assignment)
    else:
        response.revision += 1
    for question in page_questions:
        SurveyAnswer.objects.update_or_create(response=response, question=question,
                                             defaults={'value': values[question.pk]})
    if final:
        response.status = SurveyResponse.Status.SUBMITTED
        response.submitted_at = timezone.now()
    response.save()
    return response


def answer_display(question, value):
    if value is None or value == '' or value == []:
        return '—'
    if question.has_options:
        labels = {o.pk: o.text for o in question.options.all()}
        ids = value if isinstance(value, list) else [value]
        return '; '.join(labels.get(pk, '[вариант удалён]') for pk in ids)
    return str(value)
