from django.urls import path
from django.views.generic import RedirectView

from documents import views as docviews
from family_income import staff_views as family_income_staff_views
from surveys_builder import views as survey_views
from . import views, service_views

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="staff_users_list", query_string=True), name="staff_index"),
    path("surveys/", survey_views.survey_list, name="staff_surveys_list"),
    path("letters/<int:user_id>/", views.staff_letter_detail, name="staff_letter_detail"),
    path("letters/<int:user_id>/download/", views.staff_letter_download, name="staff_letter_download"),
    path("profiles/<int:user_id>/", views.staff_profile_detail, name="staff_profile_detail"),
    path("video/<int:user_id>/", views.staff_video_detail, name="staff_video_detail"),
    path("documents/<int:user_id>/", views.staff_documents_detail, name="staff_documents_detail"),
    path(
        "family-income/<int:user_id>/",
        family_income_staff_views.staff_family_income,
        name="staff_family_income",
    ),
    path("study/<int:user_id>/", views.staff_study_detail, name="staff_study_detail"),
    path("interview/<int:user_id>/", views.interview_detail, name="interview_detail"),
    path("interview/<int:user_id>/video-stream/", views.interview_video_stream, name="interview_video_stream"),
    path("notes/<int:user_id>/", views.staff_notes_by_user, name="staff_notes"),
    path("users/", views.staff_users_list, name="staff_users_list"),

    path('users/send-notification/', views.staff_send_notification, name='staff_send_notification'),

    path("testing/<int:user_id>/", views.testing_list_for_user, name="staff_testing_list_for_user"),
    path("testing/create/", views.testing_create, name="staff_testing_create"),
    path("testing/<int:pk>/edit/", views.testing_edit, name="staff_testing_edit"),
    path("testing/<int:pk>/result/", views.testing_fill_result, name="staff_testing_fill_result"),

    path("templates/<int:user_id>/", docviews.template_list, name="staff_docs_templates"),
    path("templates/<int:template_id>/user/<int:user_id>/", docviews.template_params, name="staff_docs_generate"),

    path(
        "interview/<int:user_id>/template/download/",
        views.download_interview_template,
        name="download_interview_template",
    ),
    path(
        "interview/<int:user_id>/template/prefilled/",
        views.download_prefilled_interview_template,
        name="download_prefilled_interview_template",
    ),
    path(
        "interview/<int:user_id>/template/uploaded/",
        views.download_uploaded_interview_template,
        name="download_uploaded_interview_template",
    ),

    path("staff/notes/<int:user_id>/favorite/<int:note_id>/", views.staff_note_toggle_favorite,
         name="staff_note_toggle_favorite"),
    path("staff/notes/<int:user_id>/delete/<int:note_id>/", views.staff_note_delete, name="staff_note_delete"),

    path("staff/users/ids/", views.staff_users_ids, name="staff_users_ids"),
    path("users/letters/download/", views.staff_letters_download_zip, name="staff_letters_download_zip"),

    path("testing/template-payload/", views.testing_template_payload, name="staff_testing_template_payload"),

    path(
        "staff/users/export-xlsx/",
        service_views.export_users_xlsx,
        name="staff_users_export_xlsx"
    ),
]
