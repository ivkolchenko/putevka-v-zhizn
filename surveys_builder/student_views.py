from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import services
from .forms import ResponseForm
from .models import SurveyAssignment, SurveyResponse, SurveysBuilder
from .selectors import assignment_blocks, assignment_questions, conditional_block_ids, group_questions
from .views import response_sections


def answer_page_url(assignment_pk, page_number):
    url = reverse('student_surveys:answer', args=[assignment_pk])
    return url if page_number == 1 else f'{url}?page={page_number}'


@login_required
@require_http_methods(['GET'])
def survey_list(request):
    if not request.user.is_active:
        raise PermissionDenied
    assignments = SurveyAssignment.objects.filter(user=request.user).exclude(survey__status='draft').select_related('survey', 'response').order_by('-assigned_at', '-pk')
    return render(request, 'surveys_builder/student/student_list.html', {
        'page_obj': Paginator(assignments, 25).get_page(request.GET.get('page')),
        'active': 'surveys',
    })


@login_required
@require_http_methods(['GET', 'POST'])
def answer(request, pk):
    if not request.user.is_active:
        raise PermissionDenied
    assignment = get_object_or_404(
        SurveyAssignment.objects.select_related('survey'),
        pk=pk,
        user=request.user,
        survey__status__in=[
            SurveysBuilder.Status.PUBLISHED,
            SurveysBuilder.Status.CLOSED,
            SurveysBuilder.Status.ARCHIVED,
        ],
    )
    survey = assignment.survey
    response = SurveyResponse.objects.filter(assignment=assignment).first()
    submitted = response is not None and response.status == 'submitted'
    initial = {f'q_{a.question_id}': a.value for a in response.answers.all()} if response else {}
    questions = list(assignment_questions(assignment))
    blocks = list(assignment_blocks(assignment))
    active_blocks = conditional_block_ids(blocks, questions, initial)
    pages = group_questions(questions, [b for b in blocks if b.pk in active_blocks]) or [{'block': None, 'questions': [], 'items': []}]
    try:
        page_number = int(request.GET.get('page', '1'))
    except (TypeError, ValueError):
        raise Http404
    if not 1 <= page_number <= len(pages):
        raise Http404
    current_page = pages[page_number - 1]
    current_questions = current_page['questions']
    form = ResponseForm(
        questions=current_questions,
        initial=initial,
        blocks=[current_page['block']] if current_page['block'] else [],
    )
    errors = []
    if request.method == 'POST':
        action = request.POST.get('action')
        if action not in ('save', 'next', 'submit'):
            errors = ['Неизвестное действие.']
        else:
            try:
                candidate = initial.copy()
                for question in current_questions:
                    name = f'q_{question.pk}'
                    candidate[name] = request.POST.getlist(name) if question.answer_type == 'multiple_choice' else request.POST.get(name)
                candidate_blocks = conditional_block_ids(blocks, questions, candidate)
                current_block_id = current_page['block'].pk if current_page['block'] else None
                current_index = next((i for i, b in enumerate(blocks) if b.pk == current_block_id), -1)
                has_next = any(b.pk in candidate_blocks for b in blocks[current_index + 1:])
                if action == 'submit' and has_next and page_number != len(pages):
                    raise ValidationError('Отправить ответ можно только на последней странице опроса.')
                saved = services.save_response_page(
                    pk,
                    request.user,
                    request.POST,
                    final=action == 'submit' and not has_next,
                    revision=request.POST.get('response_revision'),
                    question_ids=[question.pk for question in current_questions],
                    require_page=action != 'save',
                )
                messages.success(request, {
                    'save': 'Черновик ответа сохранён.',
                    'next': 'Ответы страницы сохранены.',
                    'submit': 'Ответ отправлен.',
                }['next' if action == 'submit' and has_next else action])
                updated = {f'q_{a.question_id}': a.value for a in saved.answers.all()}
                updated_blocks = conditional_block_ids(blocks, questions, updated)
                visible = [b.pk for b in blocks if b.pk in updated_blocks]
                current_number = visible.index(current_block_id) + 1 if current_block_id in visible else 1
                destination = current_number + 1 if action in ('next', 'submit') and has_next else current_number
                return redirect(answer_page_url(pk, destination))
            except services.InvalidForm as error:
                form = error.form
                form.blocks = [current_page['block']] if current_page['block'] else []
            except ValidationError as error:
                form = ResponseForm(
                    request.POST,
                    questions=current_questions,
                    final=action != 'save',
                    blocks=[current_page['block']] if current_page['block'] else [],
                )
                errors = error.messages
    all_response_sections = (response_sections(response) if response else pages) if (submitted or not survey.accepts_answers) else []
    return render(request, 'surveys_builder/student/student_answer.html', {
        'survey': survey, 'assignment': assignment, 'form': form, 'errors': errors,
        'response_revision': response.revision if response else 0,
        'submitted': submitted, 'read_only': submitted or not survey.accepts_answers,
        'sections': all_response_sections[page_number - 1:page_number],
        'page_number': page_number,
        'page_count': len(pages),
        'navigation': {
            'current_block': current_page['block'].pk if current_page['block'] else None,
            'blocks': [{'id': b.pk, 'question': b.show_if_option.question_id if b.show_if_option_id else None,
                        'option': b.show_if_option_id} for b in blocks],
            'questions': [{'id': q.pk, 'block': q.block_id,
                           'question': q.show_if_option.question_id if q.show_if_option_id else None,
                           'option': q.show_if_option_id} for q in questions],
            'answers': initial,
        },
        'page_url': answer_page_url(pk, page_number),
        'previous_page_url': answer_page_url(pk, page_number - 1) if page_number > 1 else None,
        'next_page_url': answer_page_url(pk, page_number + 1) if page_number < len(pages) else None,
        'active': 'surveys',
    }, status=400 if errors else 200)
