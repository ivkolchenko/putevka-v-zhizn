from django.conf import settings
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import SurveyAssignment


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL, dispatch_uid='surveys_builder.anonymize_recipient')
def anonymize_recipient(sender, instance, using, **kwargs):
    SurveyAssignment.objects.using(using).filter(user_id=instance.pk).update(recipient_snapshot={})
