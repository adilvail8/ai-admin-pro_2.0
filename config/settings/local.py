import os


os.environ.setdefault(
    "DJANGO_SECRET_KEY",
    "unsafe-dev-local-secret-key-for-development-only",
)

from .base import *  # noqa: F403,F401


DEBUG = True
CELERY_TASK_ALWAYS_EAGER = env("CELERY_TASK_ALWAYS_EAGER", default=True)  # noqa: F405
