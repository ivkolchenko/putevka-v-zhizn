from django.urls import path
from . import api

app_name = 'surveys_api'
urlpatterns = [
    path('block-templates/', api.block_templates, name='block_templates'),
    path('metadata/', api.metadata, name='metadata'),
    path('lookups/<str:kind>/', api.lookups, name='lookups'),
    path('recipients/', api.recipients, name='recipients'),
    path('audience/preview/', api.audience_preview, name='audience_preview'),
    path('', api.surveys, name='list'),
    path('<int:pk>/', api.survey_detail, name='detail'),
    path('<int:pk>/audience/', api.audience, name='audience'),
    path('<int:pk>/blocks/', api.blocks, name='blocks'),
    path('<int:pk>/blocks/import/', api.import_block_template, name='import_block_template'),
    path('<int:pk>/blocks/<int:block_pk>/save-template/', api.save_block_template, name='save_block_template'),
    path('<int:pk>/blocks/<int:block_pk>/sections/', api.text_sections, name='text_sections'),
    path('<int:pk>/blocks/<int:block_pk>/sections/<int:section_pk>/', api.text_section_detail, name='text_section_detail'),
    path('<int:pk>/blocks/<int:block_pk>/sections/<int:section_pk>/<str:action>/', api.text_section_action, name='text_section_action'),
    path('<int:pk>/blocks/reorder/', api.block_reorder, name='block_reorder'),
    path('<int:pk>/blocks/<int:block_pk>/', api.block_detail, name='block_detail'),
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
