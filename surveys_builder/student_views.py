from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import services
from .forms import ResponseForm
from .models import SurveyAssignment, SurveyResponse
from .views import response_sections
from .selectors import assignment_blocks, assignment_questions


@login_required
@require_http_methods(['GET'])
def survey_list(request):
    if not request.user.is_active:
        raise PermissionDenied
    assignments = SurveyAssignment.objects.filter(user=request.user).exclude(survey__status='draft').select_related('survey', 'response').order_by('-assigned_at', '-pk')
    return render(request, 'surveys_builder/student_list.html', {
        'page_obj': Paginator(assignments, 25).get_page(request.GET.get('page')),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def answer(request, pk):
    if not request.user.is_active:
        raise PermissionDenied
    assignment = get_object_or_404(SurveyAssignment.objects.select_related('survey'), pk=pk, user=request.user)
    survey = assignment.survey
    response = SurveyResponse.objects.filter(assignment=assignment).first()
    submitted = response is not None and response.status == 'submitted'
    initial = {f'q_{a.question_id}': a.value for a in response.answers.all()} if response else {}
    questions = list(assignment_questions(assignment))
    form = ResponseForm(questions=questions, initial=initial, blocks=assignment_blocks(assignment))
    errors = []
    if request.method == 'POST':
        action = request.POST.get('action')
        if action not in ('save', 'submit'):
            errors = ['Неизвестное действие.']
        else:
            try:
                services.save_response(pk, request.user, request.POST, final=action == 'submit',
                                       revision=request.POST.get('response_revision'))
                messages.success(request, 'Ответ отправлен.' if action == 'submit' else 'Черновик ответа сохранён.')
                return redirect('student_surveys:answer', pk=pk)
            except services.InvalidForm as error:
                form = error.form
            except ValidationError as error:
                errors = error.messages
    return render(request, 'surveys_builder/student_answer.html', {
        'survey': survey, 'assignment': assignment, 'form': form, 'errors': errors,
        'response_revision': response.revision if response else 0,
        'submitted': submitted, 'read_only': submitted or not survey.accepts_answers,
        'sections': response_sections(response) if response and (submitted or not survey.accepts_answers) else [],
    }, status=400 if errors else 200)
