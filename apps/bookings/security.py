"""Security helpers for webhook endpoints.

Centralises token/secret verification and per-client rate limiting so that
`webhooks.py` stays focused on message processing logic.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Client, ConversationMessage


RATE_LIMIT_MESSAGE = (
    "Слишком много сообщений за короткое время. Давайте продолжим через минуту."
)


def verify_webhook_token(token: str):
    expected_token = settings.WEBHOOK_SHARED_SECRET
    if not expected_token:
        raise ValidationError("Webhook token is not configured.")
    if token != expected_token:
        raise ValidationError("Invalid webhook token.")


def verify_telegram_secret(secret: str):
    """Legacy global-only verifier — kept as a deprecated alias.

    New code should call ``verify_telegram_request(business_id=..., secret=...)``
    which also accepts per-business secrets. This helper stays so external
    callers (e.g. one-off scripts) don't break; it will be removed together
    with the green-api whitelist cleanup once messenger_webhook is audited.
    """
    expected_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected_secret:
        raise ValidationError("Telegram webhook secret is not configured.")
    if secret != expected_secret:
        raise ValidationError("Invalid Telegram webhook secret.")


def verify_telegram_request(*, business_id: int, secret: str):
    """Принимает Telegram webhook на ``/telegram/<business_id>/<secret>/``.

    Порядок проверок:

    1. Per-business path: ищем активный ``Business`` с одновременно
       совпадающими ``id=business_id`` и ``telegram_webhook_secret=secret``.
       Если есть — webhook принят, возвращаем этот Business.

    2. Global fallback (deprecated): если у Business нет per-business
       secret'а, но переданный совпадает с ``settings.TELEGRAM_WEBHOOK_SECRET``,
       webhook принят. Возвращаем ``None`` чтобы вызывающий код мог понять,
       что сработал fallback (логирование/метрики).

    3. Иначе — ``ValidationError``.

    Cross-check (id + secret в одной записи) обязателен: атакующий,
    знающий secret salon-А, не сможет постить webhook на URL salon-Б —
    lookup ``(id=B, secret=A_secret)`` ничего не вернёт.

    Импорт ``Business`` локальный, чтобы избежать циклов на старте app'ов.
    """
    from .models import Business  # pragma: no cover — import boundary

    if secret:
        per_business = (
            Business.objects.filter(
                pk=business_id,
                is_active=True,
                telegram_webhook_secret=secret,
            )
            .exclude(telegram_webhook_secret="")
            .first()
        )
        if per_business is not None:
            return per_business

    global_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if global_secret and secret == global_secret:
        return None

    raise ValidationError("Invalid Telegram webhook secret.")


def verify_green_api_request(
    *,
    token: str,
    authorization: str = "",
    remote_addr: str,
):
    candidate_token = token.strip()
    if not candidate_token and authorization:
        normalized_authorization = authorization.strip()
        if " " in normalized_authorization:
            _, candidate_token = normalized_authorization.split(" ", 1)
        else:
            candidate_token = normalized_authorization

    if candidate_token != settings.GREEN_API_SHARED_SECRET:
        raise ValidationError("Invalid Green-API signature.")
    if settings.GREEN_API_ALLOWED_IPS and remote_addr not in settings.GREEN_API_ALLOWED_IPS:
        raise ValidationError("Green-API IP is not allowed.")


def validate_green_api_business_id(business_id: int) -> int:
    """Reject Green-API webhooks for business_ids outside the configured whitelist.

    When ``GREEN_API_BUSINESS_IDS`` is empty, the check is a no-op for
    backward compatibility with existing deployments. Set the env var to
    the comma-separated list of business primary keys that legitimately
    receive Green-API messages (e.g. ``GREEN_API_BUSINESS_IDS=2,3``) to
    block attackers who guess unused ids.
    """
    allowed = settings.GREEN_API_BUSINESS_IDS
    if allowed and business_id not in allowed:
        raise ValidationError("Green-API business_id is not allowed.")
    return business_id


def enforce_client_rate_limit(*, business_id: int, client: Client, channel: str):
    window_start = timezone.now() - timedelta(minutes=1)
    recent_messages_count = ConversationMessage.objects.filter(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.USER,
        created_at__gte=window_start,
    ).count()
    if recent_messages_count >= settings.MAX_MESSAGES_PER_MINUTE:
        raise ValidationError(RATE_LIMIT_MESSAGE)
