from datetime import timedelta
from pathlib import Path

import environ
try:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
except ImportError:  # pragma: no cover - optional dependency in local dev
    sentry_sdk = None
    CeleryIntegration = None
    DjangoIntegration = None
try:
    from pythonjsonlogger.json import JsonFormatter
except ImportError:  # pragma: no cover - optional dependency in local dev
    JsonFormatter = None


BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
    CELERY_TASK_ALWAYS_EAGER=(bool, False),
    GREEN_API_ALLOWED_IPS=(list, []),
    DB_PORT=(int, 5432),
    DB_CONN_MAX_AGE=(int, 60),
)
environ.Env.read_env(BASE_DIR / ".env")


SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=True)
CORS_URLS_REGEX = r"^/api/.*$"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "django_celery_beat",
    "drf_spectacular",
    "health_check",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "phonenumber_field",
    "apps.accounts",
    "apps.api",
    "apps.bookings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
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

database_url = env("DATABASE_URL", default="")
if database_url:
    DATABASES = {
        "default": env.db("DATABASE_URL")
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME"),
            "USER": env("DB_USER"),
            "PASSWORD": env("DB_PASSWORD"),
            "HOST": env("DB_HOST", default="127.0.0.1"),
            "PORT": env("DB_PORT"),
            "CONN_MAX_AGE": env("DB_CONN_MAX_AGE"),
        }
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
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_DEFAULT_QUEUE = "messages"
CELERY_TASK_ROUTES = {
    "apps.bookings.tasks.async_prune_history": {"queue": "maintenance"},
    "apps.bookings.tasks.process_pending_reminders": {"queue": "maintenance"},
    "apps.bookings.tasks.process_outbound_health_alerts": {"queue": "maintenance"},
    "apps.bookings.tasks.send_outbound_message": {"queue": "messages"},
    "apps.bookings.tasks.send_booking_reminder": {"queue": "messages"},
    "apps.bookings.tasks.send_follow_up_if_pending": {"queue": "messages"},
    "apps.bookings.tasks.notify_human_operator": {"queue": "messages"},
    "apps.bookings.tasks.process_ai_interaction": {"queue": "ai_processing"},
}

OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-4o-mini")
OPENAI_TRANSCRIPTION_MODEL = env(
    "OPENAI_TRANSCRIPTION_MODEL",
    default="whisper-1",
)
SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default="development")
SENTRY_TRACES_SAMPLE_RATE = env.float(
    "SENTRY_TRACES_SAMPLE_RATE",
    default=0.1,
)
SENTRY_PROFILES_SAMPLE_RATE = env.float(
    "SENTRY_PROFILES_SAMPLE_RATE",
    default=0.1,
)
PHONENUMBER_DEFAULT_REGION = "KZ"
PHONENUMBER_DB_FORMAT = "E164"
ADMIN_ALERT_EMAIL = env("ADMIN_ALERT_EMAIL", default="")
HUMAN_ESCALATION_CHAT_ID = env("HUMAN_ESCALATION_CHAT_ID", default="")
WEBHOOK_SHARED_SECRET = env("WEBHOOK_SHARED_SECRET", default="")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
GREEN_API_SHARED_SECRET = env("GREEN_API_SHARED_SECRET", default="")
GREEN_API_ALLOWED_IPS = env("GREEN_API_ALLOWED_IPS")
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
OUTBOUND_ALERT_LOOKBACK_MINUTES = env.int(
    "OUTBOUND_ALERT_LOOKBACK_MINUTES",
    default=60,
)
OUTBOUND_ALERT_FAILED_THRESHOLD = env.int(
    "OUTBOUND_ALERT_FAILED_THRESHOLD",
    default=5,
)
OUTBOUND_ALERT_DEAD_LETTER_THRESHOLD = env.int(
    "OUTBOUND_ALERT_DEAD_LETTER_THRESHOLD",
    default=1,
)
OUTBOUND_ALERT_COOLDOWN_SECONDS = env.int(
    "OUTBOUND_ALERT_COOLDOWN_SECONDS",
    default=3600,
)
API_USER_RATE = env("API_USER_RATE", default="60/minute")
API_ANON_RATE = env("API_ANON_RATE", default="10/minute")
API_PAGE_SIZE = env.int("API_PAGE_SIZE", default=50)
LOG_AS_JSON = env.bool("LOG_AS_JSON", default=not DEBUG)
LOG_LEVEL = env("LOG_LEVEL", default="INFO")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": (
        "rest_framework.pagination.PageNumberPagination"
    ),
    "PAGE_SIZE": API_PAGE_SIZE,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": API_ANON_RATE,
        "user": API_USER_RATE,
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AI-Admin Pro API",
    "DESCRIPTION": (
        "Tenant-scoped operator and integration API for AI-Admin Pro."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

DEFAULT_LOG_FORMATTER = (
    "json"
    if LOG_AS_JSON and JsonFormatter is not None
    else "standard"
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": (
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            ),
        },
        "json": {
            "()": JsonFormatter,
            "fmt": (
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(business_id)s %(client_id)s %(booking_id)s "
                "%(outbound_message_id)s %(provider_event_id)s "
                "%(provider_message_id)s %(channel)s %(status)s "
                "%(error_code)s"
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": DEFAULT_LOG_FORMATTER,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}

if sentry_sdk and SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
        ],
        send_default_pii=False,
    )
