"""Все изменения выполняются здесь: права, этапы, транзакции и версии записей."""
import hashlib
import json

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404
from django.utils import timezone

from core.models import Notification, UserNotification

from .forms import AdditionalAudienceForm, AudienceForm, BlockTextSectionForm, BlockVisibilityForm, QuestionBlockForm, QuestionsBuilderForm, ResponseForm, SurveysBuilderForm
from .models import BlockTextSection, QuestionBlock, QuestionOptions, QuestionsBuilder, SurveyAnswer, SurveyAssignment, SurveyResponse, SurveysBuilder
from .selectors import assignment_questions, block_items, filter_students, resolve_audience, student_name, student_queryset


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
    if survey.is_block_template:
        raise ValidationError('Черновик блока не назначается участникам.')
    form = AudienceForm(data)
    if not form.is_valid():
        raise InvalidForm(form)
    if not resolve_audience(form.cleaned_data).exists():
        form.add_error(None, 'Не найдено получателей. Измените условия или список учеников.')
        raise InvalidForm(form)
    survey.audience_mode = form.cleaned_data['mode']
    survey.audience_filters = form.serialized_filters()
    survey.included_users.set(form.cleaned_data['included_users'])
    survey.excluded_users.clear()
    survey.deadline = form.cleaned_data['deadline']
    survey.audience_configured = True
    changed(survey)
    return survey


def audience_for(survey):
    if not survey.audience_configured:
        raise ValidationError('Сохраните получателей на этапе «Получатели и срок».')
    form = AudienceForm(AudienceForm.saved_data(survey))
    if not form.is_valid():
        raise ValidationError('Состав справочников изменился. Проверьте и заново сохраните получателей.')
    return resolve_audience(form.cleaned_data)


def normalize_question_positions(survey):
    questions = list(survey.questions.select_related('block'))
    questions.sort(key=lambda q: (q.block.position if q.block_id else 0,
                                 q.block_id or 0, q.block_position, q.pk))
    for position, question in enumerate(questions, 1):
        question.position = position
    QuestionsBuilder.objects.bulk_update(questions, ['position'])
    return {q.pk: q.position for q in questions}


def visibility_form(data):
    raw = data.get('visibility_filters', data)
    if not isinstance(raw, dict) and not hasattr(raw, 'getlist'):
        raise ValidationError('Фильтры видимости должны быть объектом.')
    if 'visibility_filters' in data:
        unknown = set(raw) - set(BlockVisibilityForm.base_fields) - {'_active_filters'}
        if unknown:
            raise ValidationError('Недопустимые фильтры видимости блока.')
    values = raw.copy()
    if '_active_filters' in values:
        values['active_filters'] = values.pop('_active_filters')
    form = BlockVisibilityForm(values)
    if not form.is_valid():
        raise InvalidForm(form)
    return form


@transaction.atomic
def save_block(pk, actor, data, revision, block_pk=None):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    if block_pk is None:
        if survey.blocks.count() >= (1 if survey.is_block_template else 50):
            raise ValidationError('Лимит блоков достигнут.')
        block = QuestionBlock(survey=survey, position=(survey.blocks.aggregate(last=Max('position'))['last'] or 0) + 1)
    else:
        block = get_object_or_404(survey.blocks, pk=block_pk)
    form = QuestionBlockForm(data, instance=block)
    if not form.is_valid():
        raise InvalidForm(form)
    filters = visibility_form(data)
    block = form.save(commit=False)
    block.visibility_filters = filters.serialized_filters()
    block.save()
    if survey.is_block_template:
        survey.title, survey.description = block.title, block.description
    changed(survey)
    return block


def write_items(block, items):
    questions, sections = [], []
    for position, item in enumerate(items, 1):
        if item['type'] == 'question':
            question = item['question']
            question.block_position = position
            questions.append(question)
        else:
            section = item['section']
            section.position = position
            sections.append(section)
    QuestionsBuilder.objects.bulk_update(questions, ['block_position'])
    BlockTextSection.objects.bulk_update(sections, ['position'])


@transaction.atomic
def save_text_section(pk, block_pk, actor, data, files, revision, section_pk=None):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    if section_pk is None and block.text_sections.count() >= 100:
        raise ValidationError('В блоке допускается не более 100 текстовых секций.')
    section = get_object_or_404(block.text_sections, pk=section_pk) if section_pk else BlockTextSection(block=block, position=len(block_items(block)) + 1)
    form = BlockTextSectionForm(data, files, instance=section)
    if not form.is_valid():
        raise InvalidForm(form)
    section = form.save()
    changed(survey)
    return section


@transaction.atomic
def text_section_action(pk, block_pk, section_pk, actor, action, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    block = get_object_or_404(survey.blocks, pk=block_pk)
    section = get_object_or_404(block.text_sections, pk=section_pk)
    items = block_items(block)
    index = next(i for i, item in enumerate(items) if item['type'] == 'section' and item['id'] == section.pk)
    if action == 'delete':
        section.delete()
        items.pop(index)
    elif action in ('up', 'down'):
        target = index + (-1 if action == 'up' else 1)
        if 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
    else:
        raise ValidationError('Неизвестное действие с секцией.')
    write_items(block, items)
    normalize_question_positions(survey)
    changed(survey)
    return survey


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
        if survey.is_block_template:
            raise ValidationError('Удалите черновик целиком через библиотеку блоков.')
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
    if question_pk is None:
        if survey.questions.count() >= 100:
            raise ValidationError('Допускается не более 100 вопросов.')
        if not data.get('block'):
            raise ValidationError('Сначала создайте блок и выберите его для вопроса.')
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
    if question_pk is None or question.block_id != original_block:
        question.block_position = len(block_items(question.block)) + 1
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
    items = block_items(question.block)
    index = next(i for i, item in enumerate(items) if item['type'] == 'question' and item['id'] == question.pk)
    if action == 'delete':
        question.delete()
        items.pop(index)
    elif action == 'duplicate':
        if survey.questions.count() >= 100:
            raise ValidationError('Допускается не более 100 вопросов.')
        copy = QuestionsBuilder.objects.create(survey=survey, block=question.block, title=question.title,
            description=question.description, answer_type=question.answer_type, required=question.required)
        QuestionOptions.objects.bulk_create([QuestionOptions(question=copy, text=o.text, position=o.position) for o in question.options.all()])
        items.insert(index + 1, {'type': 'question', 'question': copy, 'id': copy.pk})
    elif action in ('up', 'down'):
        target = index + (-1 if action == 'up' else 1)
        if 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
    else:
        raise ValidationError('Неизвестное действие с вопросом.')
    write_items(question.block, items)
    normalize_question_positions(survey)
    changed(survey)


@transaction.atomic
def reorder_questions(pk, actor, question_ids, revision):
    survey = locked_survey(pk, actor)
    require_draft(survey, revision)
    questions = {q.pk: q for q in survey.questions.all()}
    if len(question_ids) != len(questions) or set(question_ids) != set(questions):
        raise ValidationError('Передайте все вопросы этого опроса ровно по одному разу.')
    ranks = {block.pk: i for i, block in enumerate(survey.blocks.all())}
    order = [ranks[questions[pk].block_id] for pk in question_ids]
    if order != sorted(order):
        raise ValidationError('Вопросы переставляются внутри блоков; отдельно меняется порядок блоков.')
    for block in survey.blocks.all():
        ordered = iter(questions[pk] for pk in question_ids if questions[pk].block_id == block.pk)
        items = block_items(block)
        for item in items:
            if item['type'] == 'question':
                item['question'] = next(ordered)
        write_items(block, items)
    normalize_question_positions(survey)
    changed(survey)
    return survey


def block_visibility(survey, users):
    users = list(users)
    result = {user.pk: [] for user in users}
    base = student_queryset().filter(pk__in=result)
    for block in survey.blocks.all():
        try:
            form = visibility_form({'visibility_filters': block.visibility_filters})
        except InvalidForm:
            raise ValidationError(f'Проверьте фильтры видимости блока «{block.title}».')
        for user_id in filter_students(base, form.cleaned_data).values_list('pk', flat=True):
            result[user_id].append(block.pk)
    return result


def visibility_fingerprint(survey, ids):
    mapping = block_visibility(survey, student_queryset().filter(pk__in=ids))
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()


def publication_errors(survey):
    errors = []
    if survey.is_block_template:
        errors.append('Черновик блока используется через вставку в опрос и не публикуется самостоятельно.')
    if not survey.title.strip():
        errors.append('Укажите название опросника.')
    if survey.deadline and survey.deadline <= timezone.now():
        errors.append('Срок ответа уже истёк.')
    blocks = list(survey.blocks.annotate(question_count=Count('questions')))
    block_ids = {block.pk for block in blocks}
    if not blocks:
        errors.append('Добавьте хотя бы один блок.')
    if len(blocks) > 50:
        errors.append('Допускается не более 50 блоков.')
    for block in blocks:
        if not block.title.strip():
            errors.append('Укажите название каждого блока.')
        if not block.question_count and not block.text_sections.exists():
            errors.append(f'Блок «{block.title}»: добавьте вопрос, текст или картинку.')
        try:
            visibility_form({'visibility_filters': block.visibility_filters})
        except (InvalidForm, ValidationError):
            errors.append(f'Проверьте фильтры блока «{block.title}».')
    try:
        users = list(audience_for(survey))
        if not users:
            errors.append('В выборке больше нет получателей.')
        elif blocks:
            mapping = block_visibility(survey, users)
            missing = sum(not ids for ids in mapping.values())
            if missing:
                errors.append(f'Для {missing} получателей нет видимых блоков. Проверьте фильтры блоков или назначения.')
    except ValidationError as error:
        errors.extend(error.messages)
    questions = list(survey.questions.prefetch_related('options'))
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
                          'audience': audience_fingerprint(ids), 'visibility': visibility_fingerprint(survey, ids)}, salt='surveys-builder.publish')


def require_additional_audience(survey):
    if survey.is_block_template or not survey.accepts_answers:
        raise ValidationError('Добавить участников можно только в опубликованный опрос с открытым приёмом ответов.')


def additional_audience(survey, data):
    require_additional_audience(survey)
    form = AdditionalAudienceForm(data, survey=survey)
    if not form.is_valid():
        raise InvalidForm(form)
    users = list(resolve_audience(form.cleaned_data).exclude(survey_assignments__survey=survey))
    if not users:
        form.add_error(None, 'Новых участников по этим условиям нет. Измените фильтры или выбор участников.')
        raise InvalidForm(form)
    mapping = block_visibility(survey, users)
    missing = sum(not ids for ids in mapping.values())
    if missing:
        form.add_error(None, f'Для {missing} новых участников нет видимых блоков. Выберите других участников.')
        raise InvalidForm(form)
    return form, users, mapping


def _assign_users(survey, actor, users, mapping):
    assignments = []
    for user in users:
        info = getattr(user, 'user_info', None)
        assignments.append(SurveyAssignment(survey=survey, user=user, visible_block_ids=mapping[user.pk], recipient_snapshot={
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


@transaction.atomic
def add_recipients(pk, actor, data, revision):
    survey = locked_survey(pk, actor)
    check_revision(survey, revision)
    _, users, mapping = additional_audience(survey, data)
    _assign_users(survey, actor, users, mapping)
    changed(survey)
    return survey, len(users)


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
    if confirmation.get('visibility') != visibility_fingerprint(survey, [user.pk for user in users]):
        raise ValidationError('Видимость блоков изменилась после предпросмотра. Проверьте опрос заново.')
    mapping = block_visibility(survey, users)
    _assign_users(survey, actor, users, mapping)
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
    elif action == 'archive' and not survey.is_block_template and survey.status in (
        SurveysBuilder.Status.DRAFT, SurveysBuilder.Status.PUBLISHED, SurveysBuilder.Status.CLOSED
    ):
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
        # Сначала удаляем ответы, защищающие назначения и вопросы от каскада.
        SurveyResponse.objects.filter(assignment__survey=survey).delete()
        survey.assignments.all().delete()
    else:
        require_draft(survey, revision)
    survey.delete()


def clone_block(source, target, position):
    block = QuestionBlock.objects.create(survey=target, title=source.title, description=source.description,
        position=position, visibility_filters=source.visibility_filters)
    for question in source.questions.prefetch_related('options'):
        copy = QuestionsBuilder.objects.create(survey=target, block=block, title=question.title,
            description=question.description, answer_type=question.answer_type, required=question.required,
            position=question.position, block_position=question.block_position)
        QuestionOptions.objects.bulk_create([QuestionOptions(question=copy, text=o.text, position=o.position) for o in question.options.all()])
    BlockTextSection.objects.bulk_create([BlockTextSection(block=block, title=section.title, text=section.text,
        image=section.image.name, image_alt=section.image_alt, position=section.position) for section in source.text_sections.all()])
    return block


@transaction.atomic
def copy_survey(pk, actor):
    source = locked_survey(pk, actor)
    copy = SurveysBuilder.objects.create(author=actor, title=source.title[:192] + ' (копия)', description=source.description,
        audience_mode=source.audience_mode, audience_filters=source.audience_filters,
        audience_configured=False, is_block_template=source.is_block_template)
    copy.included_users.set(source.included_users.all())
    for block in source.blocks.all():
        clone_block(block, copy, block.position)
    return copy


@transaction.atomic
def create_block_template(actor, data, *, source_pk=None, block_pk=None, revision=None):
    require_staff(actor)
    if source_pk is not None:
        source_survey = locked_survey(source_pk, actor)
        check_revision(source_survey, revision)
        source = get_object_or_404(source_survey.blocks, pk=block_pk)
        template = SurveysBuilder.objects.create(author=actor, title=source.title, description=source.description, is_block_template=True)
        clone_block(source, template, 1)
        return template
    form = QuestionBlockForm(data)
    if not form.is_valid():
        raise InvalidForm(form)
    template = SurveysBuilder.objects.create(author=actor, title=form.cleaned_data['title'],
        description=form.cleaned_data['description'], is_block_template=True)
    save_block(template.pk, actor, data, template.revision)
    template.refresh_from_db()
    return template


@transaction.atomic
def import_block_template(pk, actor, template_pk, revision):
    target = locked_survey(pk, actor)
    require_draft(target, revision)
    if target.is_block_template or target.blocks.count() >= 50:
        raise ValidationError('Нельзя добавить блок в этот черновик или превышен лимит блоков.')
    source = locked_survey(template_pk, actor)
    if not source.is_block_template:
        raise ValidationError('Выберите черновик из библиотеки блоков.')
    if target.questions.count() + source.questions.count() > 100:
        raise ValidationError('Допускается не более 100 вопросов в опросе.')
    block = clone_block(source.blocks.get(), target, (target.blocks.aggregate(last=Max('position'))['last'] or 0) + 1)
    normalize_question_positions(target)
    changed(target)
    return block


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
    questions = list(assignment_questions(assignment))
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
