"""
URL configuration for Putevka project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

import core.views

handler400 = "core.error_views.bad_request"
handler403 = "core.error_views.permission_denied"
handler404 = "core.error_views.page_not_found"
handler500 = "core.error_views.server_error"

urlpatterns = [
                  path('admin/send-notification-to-users/', core.views.send_notification_to_users,
                       name='send_notification_to_users'),
                  path('admin/', admin.site.urls),
                  path('', include('core.urls')),
                  path('form/', include('scholar_form.urls')),
                  path('documents/', include('documents.urls')),
                  path('family-income/', include('family_income.urls')),
                  path('study/surveys/', include('surveys.urls')),
                  path("study/", include("my_study.urls")),
                  path('staff/', include('review_by_tutor.urls')),
                  path("before/", include('subscriber.urls')),
                  path("internal/ai/", include("core.ai_urls")),
                  path("internal/telegram/", include("core.telegram_urls")),
                  path('staff/surveys_builder/', include('surveys_builder.urls')),
                  path('surveys/', include('surveys_builder.student_urls')),


              ] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT) \
              + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
