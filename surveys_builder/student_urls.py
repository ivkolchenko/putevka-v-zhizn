from django.urls import include, path
from . import student_views

app_name = 'student_surveys'
urlpatterns = [
    path('api/', include('surveys_builder.student_api_urls')),
    path('', student_views.survey_list, name='list'),
    path('<int:pk>/', student_views.answer, name='answer'),
]
