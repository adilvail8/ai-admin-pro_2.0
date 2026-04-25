from dataclasses import dataclass
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from redis.asyncio import Redis as AsyncRedisClient

from config.celery import app as celery_app
from health_check.base import HealthCheck
from health_check.checks import Database
from health_check.contrib.celery import Ping
from health_check.contrib.redis import Redis
from health_check.exceptions import ServiceWarning
from health_check.views import HealthCheckView


@dataclass
class SettingsHealthCheck(HealthCheck):
    label: str
    configured: bool
    warning_message: str

    async def run(self):
        if not self.configured:
            raise ServiceWarning(self.warning_message)


class OperationalHealthCheckView(HealthCheckView):
    checks = ()

    def get_checks(self):
        yield Database(alias="default")
        if is_celery_eager_mode():
            yield SettingsHealthCheck(
                label="celery_eager_mode",
                configured=False,
                warning_message="Celery is running in eager mode.",
            )
        else:
            yield Redis(
                client_factory=lambda: AsyncRedisClient.from_url(
                    settings.CELERY_BROKER_URL,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
            )
            yield Ping(
                app=celery_app,
                timeout=timedelta(seconds=2),
            )
        yield SettingsHealthCheck(
            label="beat_scheduler",
            configured=bool(settings.CELERY_BEAT_SCHEDULER),
            warning_message="Celery beat scheduler is not configured.",
        )
        yield SettingsHealthCheck(
            label="openai_configured",
            configured=bool(settings.OPENAI_API_KEY),
            warning_message="OpenAI API key is not configured.",
        )
        yield SettingsHealthCheck(
            label="telegram_transport",
            configured=bool(settings.TELEGRAM_BOT_TOKEN),
            warning_message="Telegram bot token is not configured.",
        )
        yield SettingsHealthCheck(
            label="whatsapp_transport",
            configured=bool(
                settings.GREEN_API_URL
                and settings.GREEN_API_INSTANCE_ID
                and settings.GREEN_API_API_TOKEN
            ),
            warning_message="WhatsApp transport is not configured.",
        )
        yield SettingsHealthCheck(
            label="internal_alert_transport",
            configured=bool(settings.INTERNAL_ALERT_WEBHOOK_URL),
            warning_message="Internal alert transport is not configured.",
        )


def is_celery_eager_mode() -> bool:
    value = settings.CELERY_TASK_ALWAYS_EAGER
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def evaluate_health_check(check: HealthCheck) -> tuple[str, str]:
    result = async_to_sync(check.get_result)()
    if result.error is None:
        return "ok", "OK"
    if isinstance(result.error, ServiceWarning):
        return "degraded", str(result.error)
    return "failed", str(result.error)


def check_database_connection() -> bool:
    status, _ = evaluate_health_check(Database(alias="default"))
    return status == "ok"


def check_broker_connection() -> bool:
    if is_celery_eager_mode():
        return True
    status, _ = evaluate_health_check(
        Redis(
            client_factory=lambda: AsyncRedisClient.from_url(
                settings.CELERY_BROKER_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        )
    )
    return status == "ok"


def build_health_snapshot() -> dict:
    database_status, _ = evaluate_health_check(Database(alias="default"))
    broker_status = "ok" if check_broker_connection() else "failed"
    if is_celery_eager_mode():
        celery_status = "degraded"
    else:
        celery_status, _ = evaluate_health_check(
            Ping(
                app=celery_app,
                timeout=timedelta(seconds=2),
            )
        )
    beat_status, _ = evaluate_health_check(
        SettingsHealthCheck(
            label="beat_scheduler",
            configured=bool(settings.CELERY_BEAT_SCHEDULER),
            warning_message="Celery beat scheduler is not configured.",
        )
    )
    openai_status, _ = evaluate_health_check(
        SettingsHealthCheck(
            label="openai_configured",
            configured=bool(settings.OPENAI_API_KEY),
            warning_message="OpenAI API key is not configured.",
        )
    )
    telegram_status, _ = evaluate_health_check(
        SettingsHealthCheck(
            label="telegram_transport",
            configured=bool(settings.TELEGRAM_BOT_TOKEN),
            warning_message="Telegram bot token is not configured.",
        )
    )
    whatsapp_status, _ = evaluate_health_check(
        SettingsHealthCheck(
            label="whatsapp_transport",
            configured=bool(
                settings.GREEN_API_URL
                and settings.GREEN_API_INSTANCE_ID
                and settings.GREEN_API_API_TOKEN
            ),
            warning_message="WhatsApp transport is not configured.",
        )
    )
    internal_alert_status, _ = evaluate_health_check(
        SettingsHealthCheck(
            label="internal_alert_transport",
            configured=bool(settings.INTERNAL_ALERT_WEBHOOK_URL),
            warning_message="Internal alert transport is not configured.",
        )
    )

    checks = {
        "database": database_status,
        "broker": broker_status,
        "celery_eager_mode": "degraded" if is_celery_eager_mode() else "ok",
        "celery_worker": celery_status,
        "beat_scheduler": beat_status,
        "openai_configured": openai_status,
        "telegram_transport": telegram_status,
        "whatsapp_transport": whatsapp_status,
        "internal_alert_transport": internal_alert_status,
    }
    overall_status = (
        "ok"
        if all(
            value != "failed"
            for key, value in checks.items()
            if key not in {"celery_eager_mode", "openai_configured", "telegram_transport", "whatsapp_transport", "internal_alert_transport"}
        )
        else "failed"
    )
    return {
        "status": overall_status,
        "checks": checks,
        "celery": {
            "default_queue": getattr(
                settings,
                "CELERY_TASK_DEFAULT_QUEUE",
                "celery",
            ),
            "routes": {
                task_name: route.get(
                    "queue",
                    getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "celery"),
                )
                for task_name, route in getattr(
                    settings,
                    "CELERY_TASK_ROUTES",
                    {},
                ).items()
            },
        },
    }
