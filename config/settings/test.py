import os


os.environ.setdefault(
    "DJANGO_SECRET_KEY",
    "test-secret-key-for-jwt-signing-with-safe-length-1234567890",
)

from .base import *  # noqa: F403,F401


DEBUG = False
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",  # noqa: F405
    }
}
CELERY_TASK_ALWAYS_EAGER = True
