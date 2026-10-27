from django.urls import path

from . import views


app_name = "surveys"

urlpatterns = [
    path("<int:pk>/", views.take_survey, name="take"),
    path("submissions/<int:pk>/", views.submission_complete, name="complete"),
]
