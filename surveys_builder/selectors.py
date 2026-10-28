"""Запросы аудитории используют существующие UserInfo, курсы и предметы проекта."""
from django.contrib.auth import get_user_model
from django.db.models import Q

from my_study.models import CourseSelection


def group_questions(questions, blocks):
    """Вопросы без блока идут первыми; внутри каждого блока сохраняется порядок."""
    grouped = {None: []}
    blocks = list(blocks)
    grouped.update({block.pk: [] for block in blocks})
    for question in questions:
        grouped.setdefault(question.block_id, []).append(question)
    sections = [{'block': None, 'questions': grouped[None]}] if grouped[None] else []
    sections.extend({'block': block, 'questions': grouped[block.pk]} for block in blocks)
    return sections


def student_queryset():
    return get_user_model().objects.filter(is_staff=False, is_superuser=False).select_related('user_info')


def student_name(user):
    info = getattr(user, 'user_info', None)
    if info:
        name = ' '.join(filter(None, [info.last_name, info.first_name, info.middle_name])).strip()
        if name:
            return name
    return user.get_full_name().strip() or user.get_username()


def resolve_audience(data):
    """Принимает только cleaned_data формы: между группами И, внутри группы ИЛИ."""
    base = student_queryset()
    included = data.get('included_users')
    included_ids = list(included.values_list('pk', flat=True)) if included is not None else []
    excluded = data.get('excluded_users')
    excluded_ids = list(excluded.values_list('pk', flat=True)) if excluded is not None else []
    if data['mode'] == 'individual':
        return base.filter(pk__in=included_ids).exclude(pk__in=excluded_ids).order_by('pk')

    users = base
    for key, lookup in [
        ('grades', 'user_info__next_year_class_digit__in'),
        ('profiles', 'user_info__internal_study_profile__in'),
        ('statuses', 'user_info__status__in'),
        ('steps', 'user_info__selection_step__in'),
        ('form_statuses', 'user_info__form_status__in'),
        ('planned_exams', 'user_info__planned_exams__in'),
    ]:
        if data.get(key):
            users = users.filter(**{lookup: data[key]})
    if data.get('account_active'):
        users = users.filter(is_active=data['account_active'] == 'yes')
    if data.get('registration_complete'):
        complete = Q(registrationpersonaldata__current_step='finish')
        users = users.filter(complete if data['registration_complete'] == 'yes' else ~complete)
    if data.get('region'):
        users = users.filter(user_info__region__iexact=data['region'])
    if data.get('q'):
        # ФИО можно вводить несколькими словами, в любом порядке.
        for word in data['q'].split():
            users = users.filter(Q(username__icontains=word) | Q(email__icontains=word)
                                 | Q(first_name__icontains=word) | Q(last_name__icontains=word)
                                 | Q(user_info__first_name__icontains=word)
                                 | Q(user_info__last_name__icontains=word)
                                 | Q(user_info__middle_name__icontains=word)
                                 | Q(user_info__email__icontains=word))

    # Школа, предмет и курс должны относиться к ОДНОМУ выбранному курсу ученика.
    enrollment = CourseSelection.objects.all()
    has_course_filter = False
    for key, lookup in [('schools', 'course__school__in'), ('courses', 'course__in'), ('subjects', 'course__subject__in')]:
        if data.get(key):
            enrollment = enrollment.filter(**{lookup: data[key]})
            has_course_filter = True
    if has_course_filter:
        users = users.filter(pk__in=enrollment.values('user_id'))
    users = base.filter(Q(pk__in=users.values('pk')) | Q(pk__in=included_ids))
    return users.exclude(pk__in=excluded_ids).distinct().order_by('pk')
