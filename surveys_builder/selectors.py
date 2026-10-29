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
    return assignment.survey.blocks.filter(pk__in=assignment.visible_block_ids).prefetch_related('text_sections')


def assignment_questions(assignment):
    return assignment.survey.questions.filter(block_id__in=assignment.visible_block_ids).select_related('block').prefetch_related('options')


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
