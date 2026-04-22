from pathlib import Path

import environ


BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    CELERY_TASK_ALWAYS_EAGER=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")


SECRET_KEY = env("DJANGO_SECRET_KEY", default="unsafe-dev-secret")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "phonenumber_field",
    "apps.bookings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = env("TIME_ZONE", default="Asia/Almaty")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ADMIN_URL_PATH = env("ADMIN_URL_PATH", default="secure-admin/")

CELERY_BROKER_URL = env(
    "CELERY_BROKER_URL",
    default="redis://localhost:6379/0",
)
CELERY_RESULT_BACKEND = env(
    "CELERY_RESULT_BACKEND",
    default=CELERY_BROKER_URL,
)
CELERY_TASK_ALWAYS_EAGER = env("CELERY_TASK_ALWAYS_EAGER")

OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_TRANSCRIPTION_MODEL = env(
    "OPENAI_TRANSCRIPTION_MODEL",
    default="whisper-1",
)
PHONENUMBER_DEFAULT_REGION = "KZ"
PHONENUMBER_DB_FORMAT = "E164"
ADMIN_ALERT_EMAIL = env("ADMIN_ALERT_EMAIL", default="")
HUMAN_ESCALATION_CHAT_ID = env("HUMAN_ESCALATION_CHAT_ID", default="")
WEBHOOK_SHARED_SECRET = env("WEBHOOK_SHARED_SECRET", default="")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
GREEN_API_SHARED_SECRET = env("GREEN_API_SHARED_SECRET", default="")
GREEN_API_ALLOWED_IPS = env("GREEN_API_ALLOWED_IPS", default=[])
GREEN_API_URL = env("GREEN_API_URL", default="")
GREEN_API_INSTANCE_ID = env("GREEN_API_INSTANCE_ID", default="")
GREEN_API_API_TOKEN = env("GREEN_API_API_TOKEN", default="")
INTERNAL_ALERT_WEBHOOK_URL = env("INTERNAL_ALERT_WEBHOOK_URL", default="")
INTERNAL_ALERT_WEBHOOK_TOKEN = env(
    "INTERNAL_ALERT_WEBHOOK_TOKEN",
    default="",
)
OUTBOUND_CALLBACK_SECRET = env("OUTBOUND_CALLBACK_SECRET", default="")
MAX_VOICE_FILE_SIZE_BYTES = env.int(
    "MAX_VOICE_FILE_SIZE_BYTES",
    default=10 * 1024 * 1024,
)
MAX_MESSAGES_PER_MINUTE = env.int("MAX_MESSAGES_PER_MINUTE", default=8)
MAX_OUTBOUND_ATTEMPTS = env.int("MAX_OUTBOUND_ATTEMPTS", default=3)
OUTBOUND_TRANSPORT_TIMEOUT_SECONDS = env.int(
    "OUTBOUND_TRANSPORT_TIMEOUT_SECONDS",
    default=10,
)
