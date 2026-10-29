"""Явный JSON-контракт: личные настройки аудитории выдаются только сотрудникам."""
from . import services
from .forms import AudienceForm
from .selectors import block_items
from django.urls import reverse


def question_data(question):
    return {
        'id': question.pk, 'title': question.title, 'description': question.description,
        'block_id': question.block_id, 'block_position': question.block_position,
        'answer_type': question.answer_type, 'required': question.required,
        'position': question.position,
        'options': [{'id': o.pk, 'text': o.text, 'position': o.position} for o in question.options.all()],
    }


def section_data(section):
    return {'id': section.pk, 'title': section.title, 'text': section.text,
            'image_url': reverse('surveys_builder:section_image', args=[section.pk]) if section.image else None,
            'image_alt': section.image_alt, 'position': section.position}


def block_data(block, *, staff=True):
    data = {'id': block.pk, 'title': block.title, 'description': block.description, 'position': block.position,
            'items': [{'type': item['type'], 'id': item['id'],
                       **({'section': section_data(item['section'])} if item['type'] == 'section' else {})}
                      for item in block_items(block)]}
    if staff:
        data['visibility_filters'] = block.visibility_filters
    return data


def survey_data(survey, *, detail=False, staff=True, block_ids=None):
    data = {
        'id': survey.pk, 'title': survey.title, 'description': survey.description,
        'status': survey.status, 'deadline': survey.deadline,
        'published_at': survey.published_at, 'closed_at': survey.closed_at,
        'accepts_answers': survey.accepts_answers,
    }
    if staff:
        data.update(revision=survey.revision, author_id=survey.author_id,
                    created_at=survey.created_at, updated_at=survey.updated_at,
                    audience_configured=survey.audience_configured, is_block_template=survey.is_block_template)
        if detail:
            data['audience'] = AudienceForm.saved_data(survey)
    if detail:
        blocks = survey.blocks.all() if staff else survey.blocks.filter(pk__in=block_ids or [])
        questions = survey.questions.all() if staff else survey.questions.filter(block_id__in=block_ids or [])
        data['blocks'] = [block_data(block, staff=staff) for block in blocks.prefetch_related('questions', 'text_sections')]
        data['questions'] = [question_data(q) for q in questions.prefetch_related('options')]
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
