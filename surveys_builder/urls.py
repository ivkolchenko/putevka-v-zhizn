from django.urls import path
from . import views

app_name = 'surveys_builder'

urlpatterns = [
    path('', views.constructor, name='constructor'),
    path('<int:pk>/', views.survey_detail, name='survey_detail'),
    path('<int:pk>/questions/<int:question_pk>/',
         views.question_edit,
         name='question_edit'),
    path('<int:pk>/questions/<int:question_pk>/delete/',
         views.question_delete, name='question_delete'),
]
