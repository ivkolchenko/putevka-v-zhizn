"""Запросы аудитории используют существующие UserInfo, курсы и предметы проекта."""
from django.contrib.auth import get_user_model

from my_study.models import CourseSelection


def block_items(block, questions=None):
    questions = list(block.questions.all()) if questions is None else list(questions)
    items = [{'type': 'question', 'question': q, 'id': q.pk, 'position': q.block_position} for q in questions]
    items += [{'type': 'section', 'section': section, 'id': section.pk, 'position': section.position}
              for section in block.text_sections.all()]
    return sorted(items, key=lambda item: (item['position'], item['type'], item['id']))


def group_questions(questions, blocks):
    grouped = {}
    for question in questions:
        grouped.setdefault(question.block_id, []).append(question)
    return [{'block': block, 'has_visibility_filters': any(value for key, value in block.visibility_filters.items() if not key.startswith('_')), 'questions': grouped.get(block.pk, []),
             'items': block_items(block, grouped.get(block.pk, []))} for block in blocks]


def student_queryset():
    return get_user_model().objects.filter(is_active=True, is_staff=False, is_superuser=False,
        registrationpersonaldata__current_step='finish').select_related('user_info')


def assignment_blocks(assignment):
    return assignment.survey.blocks.filter(pk__in=assignment.visible_block_ids).select_related('show_if_option__question').prefetch_related('text_sections')


def assignment_questions(assignment):
    questions = assignment.survey.questions.filter(block_id__in=assignment.visible_block_ids)
    if assignment.visible_question_ids is not None:
        questions = questions.filter(pk__in=assignment.visible_question_ids)
    questions = questions.select_related('block', 'show_if_option__question').prefetch_related('options')
    available = conditional_question_ids(list(questions), blocks=list(assignment_blocks(assignment)))
    return questions.filter(pk__in=available)


def conditional_visibility(questions, blocks=None, values=None):
    available_questions = {q.pk: q for q in questions}
    available_blocks = {block.pk: block for block in blocks} if blocks is not None else None
    active = {}

    def matches(kind, pk, visiting):
        key = (kind, pk)
        if key in active:
            return active[key]
        if key in visiting:
            return False
        obj = (available_questions if kind == 'question' else available_blocks).get(pk)
        if obj is None:
            return False
        branch = visiting | {key}
        visible = True
        if kind == 'question' and available_blocks is not None:
            visible = matches('block', obj.block_id, branch)
        if visible and obj.show_if_option_id:
            parent_id = obj.show_if_option.question_id
            visible = matches('question', parent_id, branch)
            if visible and values is not None:
                name = f'q_{parent_id}'
                value = values.getlist(name) if hasattr(values, 'getlist') else values.get(name)
                selected = value if isinstance(value, list) else [value]
                visible = str(obj.show_if_option_id) in [str(item) for item in selected]
        active[key] = visible
        return visible

    block_ids = {pk for pk in available_blocks if matches('block', pk, set())} if available_blocks is not None else set()
    question_ids = {pk for pk in available_questions if matches('question', pk, set())}
    return block_ids, question_ids


def conditional_question_ids(questions, values=None, blocks=None):
    return conditional_visibility(questions, blocks, values)[1]


def conditional_block_ids(blocks, questions, values=None):
    return conditional_visibility(questions, blocks, values)[0]


def student_name(user):
    info = getattr(user, 'user_info', None)
    if info:
        name = ' '.join(filter(None, [info.last_name, info.first_name, info.middle_name])).strip()
        if name:
            return name
    return user.get_full_name().strip() or user.get_username()


def filter_students(users, data):
    for key, lookup in [('grades', 'user_info__next_year_class_digit__in'),
                        ('profiles', 'user_info__internal_study_profile__in'),
                        ('statuses', 'user_info__status__in'), ('steps', 'user_info__selection_step__in'),
                        ('planned_exams', 'user_info__planned_exams__in')]:
        if data.get(key):
            users = users.filter(**{lookup: data[key]})
    enrollment = CourseSelection.objects.all()
    has_course_filter = False
    for key, lookup in [('schools', 'course__school__in'), ('courses', 'course__in'), ('subjects', 'course__subject__in')]:
        if data.get(key):
            enrollment = enrollment.filter(**{lookup: data[key]})
            has_course_filter = True
    if has_course_filter:
        users = users.filter(pk__in=enrollment.values('user_id'))
    return users.distinct().order_by('pk')


def resolve_audience(data):
    users = student_queryset()
    if data['mode'] == 'individual':
        return users.filter(pk__in=data['included_users'].values('pk')).order_by('pk')
    return filter_students(users, data)
