from django.urls import path
from . import api

app_name = 'student_surveys_api'
urlpatterns = [
    path('', api.my_surveys, name='list'),
    path('<int:pk>/', api.my_survey_detail, name='detail'),
    path('<int:pk>/response/', api.my_response, name='response'),
]
