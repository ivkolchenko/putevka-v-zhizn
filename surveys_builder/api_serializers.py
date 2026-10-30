"""Явный JSON-контракт: личные настройки аудитории выдаются только сотрудникам."""
from . import services
from .forms import AudienceForm
from .selectors import block_items
from django.urls import reverse


def question_data(question, *, staff=True):
    data = {
        'id': question.pk, 'title': question.title, 'description': question.description,
        'block_id': question.block_id, 'block_position': question.block_position,
        'answer_type': question.answer_type, 'required': question.required,
        'position': question.position,
        'show_if_option_id': question.show_if_option_id,
        'parent_question_id': question.show_if_option.question_id if question.show_if_option_id else None,
        'options': [{'id': o.pk, 'text': o.text, 'position': o.position} for o in question.options.all()],
    }
    if staff:
        data['visibility_filters'] = question.visibility_filters
    return data


def section_data(section):
    return {'id': section.pk, 'title': section.title, 'text': section.text,
            'image_url': reverse('surveys_builder:section_image', args=[section.pk]) if section.image else None,
            'image_alt': section.image_alt, 'position': section.position}


def block_data(block, *, staff=True, questions=None):
    data = {'id': block.pk, 'title': block.title, 'description': block.description, 'position': block.position,
            'show_if_option_id': block.show_if_option_id,
            'parent_question_id': block.show_if_option.question_id if block.show_if_option_id else None,
            'items': [{'type': item['type'], 'id': item['id'],
                       **({'section': section_data(item['section'])} if item['type'] == 'section' else {})}
                      for item in block_items(block, questions)]}
    if staff:
        data['visibility_filters'] = block.visibility_filters
    return data


def survey_data(survey, *, detail=False, staff=True, block_ids=None, question_ids=None):
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
        if not staff and question_ids is not None:
            questions = questions.filter(pk__in=question_ids)
        questions = list(questions.prefetch_related('options'))
        data['blocks'] = [block_data(block, staff=staff, questions=[q for q in questions if q.block_id == block.pk])
                          for block in blocks.prefetch_related('text_sections')]
        data['questions'] = [question_data(q, staff=staff) for q in questions]
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
    data['recipient'] = response.assignment.recipient_display
    data['assignment_id'] = response.assignment_id
    data['display_answers'] = {
        str(a.question_id): services.answer_display(a.question, a.value)
        for a in response.answers.select_related('question').prefetch_related('question__options')
    }
    return data
