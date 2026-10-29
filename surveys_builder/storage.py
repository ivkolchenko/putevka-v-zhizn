from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateSurveyImageStorage(FileSystemStorage):
    """Картинки блоков не находятся в публичном MEDIA_ROOT."""

    @property
    def base_location(self):
        media = Path(settings.MEDIA_ROOT)
        return str(getattr(settings, 'SURVEY_PRIVATE_MEDIA_ROOT', media.parent / ('.' + media.name + '-survey-private')))

    @property
    def location(self):
        return str(Path(self.base_location).resolve())

    def url(self, name):
        raise ValueError('Картинки блоков выдаются только через защищённый маршрут.')
