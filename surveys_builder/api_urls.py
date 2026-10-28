from django.urls import path
from . import api

app_name = 'surveys_api'
urlpatterns = [
    path('metadata/', api.metadata, name='metadata'),
    path('lookups/<str:kind>/', api.lookups, name='lookups'),
    path('recipients/', api.recipients, name='recipients'),
    path('audience/preview/', api.audience_preview, name='audience_preview'),
    path('', api.surveys, name='list'),
    path('<int:pk>/', api.survey_detail, name='detail'),
    path('<int:pk>/audience/', api.audience, name='audience'),
    path('<int:pk>/questions/', api.questions, name='questions'),
    path('<int:pk>/questions/reorder/', api.reorder, name='reorder'),
    path('<int:pk>/questions/<int:question_pk>/', api.question_detail, name='question_detail'),
    path('<int:pk>/questions/<int:question_pk>/duplicate/', api.duplicate_question, name='duplicate_question'),
    path('<int:pk>/preview/', api.preview, name='preview'),
    path('<int:pk>/publish/', api.publish, name='publish'),
    path('<int:pk>/copy/', api.copy, name='copy'),
    path('<int:pk>/close/', api.lifecycle, {'action': 'close'}, name='close'),
    path('<int:pk>/archive/', api.lifecycle, {'action': 'archive'}, name='archive'),
    path('<int:pk>/results/', api.results, name='results'),
    path('<int:pk>/results/export/', api.export, name='export'),
    path('<int:pk>/responses/<int:response_pk>/', api.response_detail, name='response_detail'),
]
