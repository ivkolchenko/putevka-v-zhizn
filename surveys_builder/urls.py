from django.urls import include, path
from . import views

app_name = 'surveys_builder'
urlpatterns = [
    path('api/', include('surveys_builder.api_urls')),
    path('', views.constructor, name='constructor'),
    path('<int:pk>/', views.survey_detail, name='survey_detail'),
    path('<int:pk>/edit/', views.survey_edit, name='survey_edit'),
    path('<int:pk>/audience/', views.audience, name='audience'),
    path('<int:pk>/preview/', views.preview, name='preview'),
    path('<int:pk>/publish/', views.publish, name='publish'),
    path('<int:pk>/copy/', views.survey_copy, name='survey_copy'),
    path('<int:pk>/delete/', views.survey_delete, name='survey_delete'),
    path('<int:pk>/status/<str:action>/', views.lifecycle, name='lifecycle'),
    path('<int:pk>/results/', views.results, name='results'),
    path('<int:pk>/results/export/', views.results_csv, name='results_csv'),
    path('<int:pk>/results/<int:response_pk>/', views.response_detail, name='response_detail'),
    path('<int:pk>/blocks/create/', views.block_edit, name='block_create'),
    path('<int:pk>/blocks/<int:block_pk>/', views.block_edit, name='block_edit'),
    path('<int:pk>/blocks/<int:block_pk>/delete/', views.block_delete, name='block_delete'),
    path('<int:pk>/blocks/<int:block_pk>/<str:action>/', views.block_action, name='block_action'),
    path('<int:pk>/questions/<int:question_pk>/', views.question_edit, name='question_edit'),
    path('<int:pk>/questions/<int:question_pk>/delete/', views.question_delete, name='question_delete'),
    path('<int:pk>/questions/<int:question_pk>/<str:action>/', views.question_action, name='question_action'),
]
