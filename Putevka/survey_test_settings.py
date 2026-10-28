"""Изолированная SQLite для проверки конструктора без рабочей базы и отправки сообщений."""
from .settings import *  # noqa: F401,F403

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
SECRET_KEY = 'survey-tests-only'
ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'handlers': {'null': {'class': 'logging.NullHandler'}},
    'root': {'handlers': ['null']},
    'loggers': {name: {'handlers': ['null'], 'propagate': False} for name in LOGGING['loggers']},
}
