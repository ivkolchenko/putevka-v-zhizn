from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required

from surveys_builder.forms import SurveysBuilderForm
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Max
from surveys_builder.models import SurveysBuilder
from .forms import SurveysBuilderForm, QuestionsBuilderForm
from .models import SurveysBuilder, QuestionsBuilder

@staff_member_required
def constructor(request):
    if request.method == 'POST':
        form = SurveysBuilderForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Опросник создан!")
            return redirect(request.path)
    else:
        form = SurveysBuilderForm()

    surveys = SurveysBuilder.objects.order_by("-id")

    return render(
        request,
        'surveys_builder/constructor.html',
        {'form': form,
         'surveys': surveys,
         }
        )


@staff_member_required
def survey_detail(request, pk):
    survey = get_object_or_404(SurveysBuilder, pk=pk)

    if request.method == 'POST':
        form = QuestionsBuilderForm(request.POST)

        if form.is_valid():
            question = form.save(commit=False)
            question.survey = survey

            last_position = survey.questions.aggregate(
                last=Max('position')
            )['last']

            question.position = (
                last_position + 1
                if last_position is not None
                else 1
            )
            question.save()

            messages.success(request, 'Вопрос добавлен!')
            return redirect(
                'surveys_builder:survey_detail',
                pk=survey.pk,
            )
    else:
        form = QuestionsBuilderForm()

        return render(
            request,
            'surveys_builder/survey_detail.html',
            {
                'survey': survey,
                'form': form,
            }
        )


@staff_member_required
def question_edit(request, pk, question_pk):
    survey = get_object_or_404(SurveysBuilder, pk=pk)

    question = get_object_or_404(
        QuestionsBuilder,
        pk=question_pk,
        survey=survey,
    )

    if request.method == 'POST':
        form = SurveysBuilderForm(
            request.POST,
            instance=question,
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Вопрос обновлён!")

            return redirect(
                'surveys_builder:survey_detail',
                pk=question.pk,
            )
    else:
        form = SurveysBuilderForm(instance=question)

    return render(
        request,
        'surveys_builder/question_edit.html',
        {
            'survey': survey,
            'question': question,
            'form': form,
        },
    )


@staff_member_required
def question_delete(request, pk, question_pk):
    survey = get_object_or_404(SurveysBuilder, pk=pk)

    question = get_object_or_404(
        QuestionsBuilder,
        pk=question_pk,
        survey=survey,
    )

    if request.method == 'POST':
        question.delete()

        messages.success(request, "Вопрос удалён!")

        return redirect(
            'surveys_builder:survey_detail',
            pk=survey.pk,
        )

    return render(
        request,
        'surveys_builder/question_delete.html',
        {
            'survey': survey,
            'question': question,
        }
    )