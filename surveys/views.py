from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from surveys_builder.models import SurveysBuilder

from .access import is_survey_assigned
from .forms import SurveyResponseForm
from .models import SurveySubmission
from .services import submit_survey_response


@login_required
@require_http_methods(["GET", "POST"])
def take_survey(request, pk):
    survey = get_object_or_404(SurveysBuilder, pk=pk)
    if not is_survey_assigned(request.user, survey):
        raise PermissionDenied

    form = SurveyResponseForm(
        request.POST if request.method == "POST" else None,
        survey=survey,
    )
    if request.method == "POST":
        submission, _ = submit_survey_response(user=request.user, form=form)
        if submission is not None:
            return redirect("surveys:complete", pk=submission.pk)

    return render(
        request,
        "surveys/take_survey.html",
        {"survey": survey, "form": form, "active": "study"},
    )


@login_required
@require_GET
def submission_complete(request, pk):
    submission = get_object_or_404(SurveySubmission, pk=pk, user=request.user)
    return render(
        request,
        "surveys/submission_complete.html",
        {"submission": submission, "active": "study"},
    )
