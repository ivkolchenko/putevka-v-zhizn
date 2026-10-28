from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import (
    Q, Count, OuterRef, Subquery, Exists,
    Case, F, When, Value,
    CharField, IntegerField,
)
from django.utils import timezone

from core.models import MotivationLetter
from family_income.models import FamilyIncomeCase, FamilyIncomeDocument
from my_study.models import CourseSelection
from review_by_tutor.models import TestAssignment
from scholar_form.models import ScholarVideo

User = get_user_model()


def build_staff_users_queryset(request):
    filters = get_staff_users_filters(request)
    q = filters['q']
    show_staff = filters['show_staff']
    registration_confirmed = filters['registration_confirmed']
    school, course = filters['school'], filters['course']
    form_status = filters['form_status']
    participant_status = filters['participant_status']
    family_income_status = filters['family_income_status']
    profiles_selected, grades_selected = filters['profiles_selected'], filters['grades_selected']
    curator_need = filters['curator_need']
    step, test_deadline = filters['step'], filters['test_deadline']
    letter_status, favorite_letter = filters['letter_status'], filters['favorite_letter']
    sort = filters['sort']
    sort_fields = [s.strip() for s in sort.split(",") if s.strip()]

    qs = (
        User.objects
        .all()
        .select_related(
            "user_info",
            "telegram_account",
            "motivation_letter",
            "scholar_video",
        )
        .prefetch_related(
            "user_info__planned_exams",
            "test_assignments",
        )
    )

    if show_staff != "1":
        qs = qs.filter(is_staff=False)

    if registration_confirmed == "1":
        qs = qs.filter(registrationpersonaldata__current_step="finish")

    if profiles_selected:
        qs = qs.filter(
            user_info__isnull=False,
            user_info__internal_study_profile__in=profiles_selected
        )

    if form_status:
        if form_status == "no_profile":
            qs = qs.filter(user_info__isnull=True)
        else:
            qs = qs.filter(
                user_info__isnull=False,
                user_info__form_status=form_status
            )

    if participant_status:
        qs = qs.filter(
            user_info__isnull=False,
            user_info__status=participant_status,
        )

    if family_income_status == "not_submitted":
        qs = qs.filter(user_info__status="FINAL STAGE").filter(
            Q(user_info__family_income_case__isnull=True)
            | Q(user_info__family_income_case__last_submitted_at__isnull=True)
        )
    elif family_income_status == FamilyIncomeCase.Status.REVISION:
        qs = qs.filter(
            user_info__status="FINAL STAGE",
            user_info__family_income_case__status=FamilyIncomeCase.Status.REVISION,
            user_info__family_income_case__last_submitted_at__isnull=False,
        )
    elif family_income_status in {
        FamilyIncomeCase.Status.PENDING_REVIEW,
        FamilyIncomeCase.Status.APPROVED,
    }:
        qs = qs.filter(
            user_info__status="FINAL STAGE",
            user_info__family_income_case__status=family_income_status,
        )
    elif family_income_status == "not_required":
        qs = qs.filter(
            Q(user_info__isnull=True) | ~Q(user_info__status="FINAL STAGE")
        )

    for word in q.split():
        qs = qs.filter(
            Q(username__icontains=word) | Q(email__icontains=word) |
            Q(first_name__icontains=word) | Q(last_name__icontains=word) |
            Q(user_info__first_name__icontains=word) | Q(user_info__last_name__icontains=word) |
            Q(user_info__middle_name__icontains=word) | Q(user_info__email__icontains=word) |
            Q(user_info__phone__icontains=word) | Q(user_info__region__icontains=word)
        )

    # Все условия относятся к одной записи выбора курса, без перекрёстных JOIN.
    enrollments = CourseSelection.objects.all()
    if school:
        enrollments = enrollments.filter(course__school_id=school)
    if course:
        enrollments = enrollments.filter(course_id=course)
    if curator_need in ("1", "0"):
        enrollments = enrollments.filter(need_tutor=curator_need == "1")
    if school or course or curator_need in ("1", "0"):
        qs = qs.filter(pk__in=enrollments.values('user_id'))

    if step:
        qs = qs.filter(user_info__selection_step=step)

    if letter_status:
        if letter_status == "no_letter":
            qs = qs.filter(motivation_letter__isnull=True)
        else:
            qs = qs.filter(motivation_letter__status=letter_status)

    if favorite_letter == "1":
        qs = qs.filter(motivation_letter__is_favorite=True)

    if grades_selected:
        grade_q = Q()

        numeric_grades = [int(g) for g in grades_selected if g in {"9", "10", "11"}]
        include_other = "other" in grades_selected

        if numeric_grades:
            grade_q |= Q(user_info__next_year_class_digit__in=numeric_grades)

        if include_other:
            grade_q |= (
                Q(user_info__isnull=False) &
                ~Q(user_info__next_year_class_digit__in=[9, 10, 11])
            )

        qs = qs.filter(grade_q) if numeric_grades or include_other else qs.none()

    letter_status_sq = Subquery(
        MotivationLetter.objects
        .filter(user_id=OuterRef("pk"))
        .values("status")[:1]
    )

    letter_favorite_sq = Subquery(
        MotivationLetter.objects
        .filter(user_id=OuterRef("pk"))
        .values("is_favorite")[:1]
    )

    video_qs = ScholarVideo.objects.filter(user_id=OuterRef("pk"))
    video_has_file = Exists(
        video_qs.filter(
            Q(yandex_disk_path__gt="") |
            (Q(file__isnull=False) & ~Q(file=""))
        )
    )
    video_deadline_sq = Subquery(video_qs.values("deadline_at")[:1])

    now = timezone.now()
    user_tests = (
        TestAssignment.objects
        .filter(user_id=OuterRef("pk"))
        .exclude(status=TestAssignment.Status.CANCELLED)
    )

    has_active_test = Exists(user_tests.filter(completed_at__isnull=True))
    has_overdue_test = Exists(user_tests.filter(due_at__lt=now, completed_at__isnull=True))
    has_due_soon_test = Exists(
        user_tests.filter(
            due_at__gte=now,
            due_at__lte=now + timedelta(days=3),
            completed_at__isnull=True
        )
    )
    has_completed_test = Exists(user_tests.filter(completed_at__isnull=False))

    next_test_due_at_sq = Subquery(
        user_tests
        .filter(completed_at__isnull=True)
        .exclude(due_at__isnull=True)
        .order_by("due_at")
        .values("due_at")[:1]
    )

    test_status_sq = Case(
        When(has_overdue_test=True, then=Value("overdue")),
        When(has_due_soon_test=True, then=Value("due_soon")),
        When(has_active_test=True, then=Value("active")),
        When(has_completed_test=True, then=Value("completed")),
        default=Value("none"),
        output_field=CharField(),
    )

    form_status_order = Case(
        When(user_info__isnull=True, then=Value(0)),
        When(user_info__form_status="draft", then=Value(1)),
        When(user_info__form_status="submitted", then=Value(2)),
        When(user_info__form_status="revision", then=Value(3)),
        When(user_info__form_status="clarification", then=Value(4)),
        When(user_info__form_status="approved", then=Value(5)),
        When(user_info__form_status="rejected", then=Value(6)),
        default=Value(99),
        output_field=IntegerField(),
    )

    test_status_order = Case(
        When(has_overdue_test=True, then=Value(1)),
        When(has_due_soon_test=True, then=Value(2)),
        When(has_active_test=True, then=Value(3)),
        When(has_completed_test=True, then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )

    letter_status_order = Case(
        When(letter_status__isnull=True, then=Value(0)),
        When(letter_status="draft", then=Value(1)),
        When(letter_status="submitted", then=Value(2)),
        When(letter_status="revision", then=Value(3)),
        default=Value(4),
        output_field=IntegerField(),
    )

    video_status_order = Case(
        When(video_has_file=True, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )

    selection_step_order = Case(
        When(user_info__selection_step="form", then=Value(1)),
        When(user_info__selection_step="test", then=Value(2)),
        When(user_info__selection_step="ml", then=Value(3)),
        When(user_info__selection_step="video", then=Value(4)),
        When(user_info__selection_step="interview_prep", then=Value(5)),
        When(user_info__selection_step="after_interview", then=Value(6)),
        default=Value(99),
        output_field=IntegerField(),
    )

    family_income_status_order = Case(
        When(
            user_info__status="FINAL STAGE",
            user_info__family_income_case__status=FamilyIncomeCase.Status.PENDING_REVIEW,
            then=Value(1),
        ),
        When(
            user_info__status="FINAL STAGE",
            user_info__family_income_case__status=FamilyIncomeCase.Status.REVISION,
            user_info__family_income_case__last_submitted_at__isnull=False,
            then=Value(2),
        ),
        When(
            user_info__status="FINAL STAGE",
            user_info__family_income_case__last_submitted_at__isnull=True,
            then=Value(3),
        ),
        When(
            user_info__status="FINAL STAGE",
            user_info__family_income_case__status=FamilyIncomeCase.Status.APPROVED,
            then=Value(4),
        ),
        default=Value(5),
        output_field=IntegerField(),
    )

    family_income_documents = FamilyIncomeDocument.objects.filter(
        case_id=OuterRef("user_info__family_income_case__pk"),
    )
    family_income_unapproved_count = Subquery(
        family_income_documents
        .exclude(review_status=FamilyIncomeDocument.ReviewStatus.APPROVED)
        .order_by()
        .values("case_id")
        .annotate(total=Count("pk"))
        .values("total")[:1],
        output_field=IntegerField(),
    )
    family_income_clarification_count = Subquery(
        family_income_documents
        .filter(review_status=FamilyIncomeDocument.ReviewStatus.CLARIFICATION)
        .order_by()
        .values("case_id")
        .annotate(total=Count("pk"))
        .values("total")[:1],
        output_field=IntegerField(),
    )

    qs = qs.annotate(
        docs_total=Count(
            "documents",
            filter=Q(documents__is_deleted=False)
        ),
        docs_pending=Count(
            "documents",
            filter=Q(documents__is_deleted=False, documents__status="PENDING")
        ),

        letter_status=letter_status_sq,
        letter_is_favorite=letter_favorite_sq,

        video_has_file=video_has_file,
        video_deadline_at=video_deadline_sq,

        has_active_test=has_active_test,
        has_overdue_test=has_overdue_test,
        has_due_soon_test=has_due_soon_test,
        has_completed_test=has_completed_test,
        next_test_due_at=next_test_due_at_sq,
        test_status=test_status_sq,

        form_status_order=form_status_order,
        test_status_order=test_status_order,
        letter_status_order=letter_status_order,
        video_status_order=video_status_order,
        selection_step_order=selection_step_order,
        family_income_case_status=F("user_info__family_income_case__status"),
        family_income_last_submitted_at=F("user_info__family_income_case__last_submitted_at"),
        family_income_approved_at=F("user_info__family_income_case__approved_at"),
        family_income_unapproved_count=family_income_unapproved_count,
        family_income_clarification_count=family_income_clarification_count,
        family_income_status_order=family_income_status_order,
    )

    if test_deadline == "overdue":
        qs = qs.filter(has_overdue_test=True)
    elif test_deadline == "due_soon":
        qs = qs.filter(has_due_soon_test=True)
    elif test_deadline == "no_due":
        qs = qs.filter(has_active_test=True, next_test_due_at__isnull=True)
    elif test_deadline == "has_due":
        qs = qs.filter(next_test_due_at__isnull=False)
    elif test_deadline == "completed":
        qs = qs.filter(has_completed_test=True)
    elif test_deadline == "active":
        qs = qs.filter(has_active_test=True)

    SORT_MAP = {
        "user": ["last_name", "first_name", "username"],
        "-user": ["-last_name", "-first_name", "-username"],

        "date_joined": ["date_joined", "last_name", "first_name", "username"],
        "-date_joined": ["-date_joined", "last_name", "first_name", "username"],

        "form": ["form_status_order", "last_name", "first_name", "username"],
        "-form": ["-form_status_order", "last_name", "first_name", "username"],

        "tests": ["test_status_order", "next_test_due_at", "last_name", "first_name", "username"],
        "-tests": ["-test_status_order", "-next_test_due_at", "last_name", "first_name", "username"],

        "letter": ["letter_status_order", "last_name", "first_name", "username"],
        "-letter": ["-letter_status_order", "last_name", "first_name", "username"],

        "video": ["video_status_order", "video_deadline_at", "last_name", "first_name", "username"],
        "-video": ["-video_status_order", "-video_deadline_at", "last_name", "first_name", "username"],

        "result": ["selection_step_order", "last_name", "first_name", "username"],
        "-result": ["-selection_step_order", "last_name", "first_name", "username"],

        "family_income": [
            "family_income_status_order", "-family_income_last_submitted_at",
            "last_name", "first_name", "username",
        ],
        "-family_income": [
            "-family_income_status_order", "family_income_last_submitted_at",
            "last_name", "first_name", "username",
        ],
    }

    order_by_fields = []
    seen = set()

    for key in sort_fields:
        for field in SORT_MAP.get(key, []):
            if field not in seen:
                order_by_fields.append(field)
                seen.add(field)

    if not order_by_fields:
        order_by_fields = ["-date_joined", "last_name", "first_name", "username"]

    return qs.distinct().order_by(*order_by_fields)


def _filter_id(value):
    value = (value or '').strip()
    if not value:
        return ''
    # Некорректный ID даёт пустую выборку, а не 500 и не расширение аудитории.
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        return '0'
    number = int(value)
    return str(number) if 0 < number <= 9223372036854775807 else '0'


def get_staff_users_filters(request):
    q = (request.GET.get("q") or "").strip()
    show_staff = (request.GET.get("show_staff") or "").strip()
    registration_confirmed = (request.GET.get("registration_confirmed", "1") or "").strip()
    school = _filter_id(request.GET.get("school"))
    course = _filter_id(request.GET.get("course"))
    form_status = (request.GET.get("form_status") or "").strip()
    participant_status = (request.GET.get("participant_status") or "").strip()
    family_income_status = (request.GET.get("family_income_status") or "").strip()
    profiles_selected = [x.strip() for x in request.GET.getlist("profile") if x.strip()]
    grades_selected = [x.strip() for x in request.GET.getlist("grade_group") if x.strip()]
    curator_need = (request.GET.get("curator_need") or "").strip()
    step = (request.GET.get("step") or "").strip()
    test_deadline = (request.GET.get("test_deadline") or "").strip()
    letter_status = (request.GET.get("letter_status") or "").strip()
    favorite_letter = (request.GET.get("favorite_letter") or "").strip()
    sort = (request.GET.get("sort") or "-date_joined").strip()

    return {
        "q": q,
        "school": school,
        "course": course,
        "form_status": form_status,
        "participant_status": participant_status,
        "family_income_status": family_income_status,
        "profiles_selected": profiles_selected,
        "grades_selected": grades_selected,
        "curator_need": curator_need,
        "step": step,
        "test_deadline": test_deadline,
        "letter_status": letter_status,
        "favorite_letter": favorite_letter,
        "sort": sort,
        "show_staff": show_staff,
        "registration_confirmed": registration_confirmed,
    }
