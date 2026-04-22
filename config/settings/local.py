import os


os.environ.setdefault(
    "DJANGO_SECRET_KEY",
    "unsafe-dev-local-secret-key-for-development-only",
)

from .base import *  # noqa: F403,F401


DEBUG = True
CELERY_TASK_ALWAYS_EAGER = True
