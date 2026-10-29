from django.apps import AppConfig


class SurveysBuilderConfig(AppConfig):
    name = 'surveys_builder'

    def ready(self):
        from . import signals
