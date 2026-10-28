"""Явный JSON-контракт: личные настройки аудитории выдаются только сотрудникам."""
from . import services
from .forms import AudienceForm


def question_data(question):
    return {
        'id': question.pk, 'title': question.title, 'description': question.description,
        'answer_type': question.answer_type, 'required': question.required,
        'position': question.position,
        'options': [{'id': o.pk, 'text': o.text, 'position': o.position} for o in question.options.all()],
    }


def survey_data(survey, *, detail=False, staff=True):
    data = {
        'id': survey.pk, 'title': survey.title, 'description': survey.description,
        'status': survey.status, 'deadline': survey.deadline,
        'published_at': survey.published_at, 'closed_at': survey.closed_at,
        'accepts_answers': survey.accepts_answers,
    }
    if staff:
        data.update(revision=survey.revision, author_id=survey.author_id,
                    created_at=survey.created_at, updated_at=survey.updated_at,
                    audience_configured=survey.audience_configured)
        if detail:
            data['audience'] = AudienceForm.saved_data(survey)
    if detail:
        data['questions'] = [question_data(q) for q in survey.questions.prefetch_related('options')]
    return data


def recipient_data(user):
    from .selectors import student_name
    info = getattr(user, 'user_info', None)
    return {
        'id': user.pk, 'name': student_name(user), 'username': user.get_username(),
        'is_active': user.is_active,
        'grade': info.next_year_class_digit if info else None,
        'profile': info.internal_study_profile if info else None,
        'status': info.status if info else None,
    }


def response_data(response):
    if response is None:
        return {'id': None, 'status': 'not_started', 'revision': 0, 'submitted_at': None, 'answers': {}}
    return {
        'id': response.pk, 'status': response.status, 'revision': response.revision,
        'submitted_at': response.submitted_at,
        'answers': {str(a.question_id): a.value for a in response.answers.all()},
    }


def submitted_response_data(response):
    data = response_data(response)
    data['recipient'] = response.assignment.recipient_snapshot
    data['assignment_id'] = response.assignment_id
    data['display_answers'] = {
        str(a.question_id): services.answer_display(a.question, a.value)
        for a in response.answers.select_related('question').prefetch_related('question__options')
    }
    return data
