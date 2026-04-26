import logging
from contextlib import contextmanager
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.db import IntegrityError, transaction
from django.utils import timezone
try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional dependency in local dev
    sentry_sdk = None

from .ai_manager import AIManager
from .audit import create_audit_log
from .models import Booking, Business, OutboundMessage
from .transports import get_transport_for_channel


logger = logging.getLogger(__name__)


@contextmanager
def sentry_task_scope(*, task_name: str, **tags):
    if sentry_sdk is None:
        yield
        return

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("task_name", task_name)
        for key, value in tags.items():
            if value in (None, ""):
                continue
            scope.set_tag(key, value)
        yield


def set_sentry_tags(**tags):
    if sentry_sdk is None:
        return

    for key, value in tags.items():
        if value in (None, ""):
            continue
        sentry_sdk.set_tag(key, value)


def get_alert_cooldown_client():
    from redis import Redis

    return Redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


def claim_outbound_alert_cooldown(*, business_id: int) -> bool:
    cooldown_key = f"alert:outbound-health:{business_id}"
    try:
        return bool(
            get_alert_cooldown_client().set(
                cooldown_key,
                "1",
                ex=settings.OUTBOUND_ALERT_COOLDOWN_SECONDS,
                nx=True,
            )
        )
    except Exception:
        logger.warning(
            "outbound_alert_cooldown_unavailable",
            extra={"business_id": business_id},
        )
        return True


def get_outbound_alert_recipient() -> str:
    return settings.HUMAN_ESCALATION_CHAT_ID or settings.ADMIN_ALERT_EMAIL or "admin"


def build_outbound_health_alert_message(
    *,
    business,
    failed_count: int,
    dead_letter_count: int,
    lookback_minutes: int,
) -> str:
    display_name = business.display_brand_name
    return (
        f"Alert for {display_name}: delivery issues detected in the last "
        f"{lookback_minutes} minutes. Failed: {failed_count}. "
        f"Dead letters: {dead_letter_count}."
    )


def get_outbound_health_counts(*, window_start):
    failed_counts = {
        row["business_id"]: row["total"]
        for row in (
            OutboundMessage.objects.exclude(channel="internal")
            .filter(
                status=OutboundMessage.Status.FAILED,
                updated_at__gte=window_start,
            )
            .values("business_id")
            .annotate(total=Count("id"))
        )
    }
    dead_letter_counts = {
        row["business_id"]: row["total"]
        for row in (
            OutboundMessage.objects.exclude(channel="internal")
            .filter(
                status=OutboundMessage.Status.DEAD_LETTER,
                dead_lettered_at__gte=window_start,
            )
            .values("business_id")
            .annotate(total=Count("id"))
        )
    }
    return failed_counts, dead_letter_counts


def dispatch_outbound_delivery(outbound_message_id: int):
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return send_outbound_message.apply(args=(outbound_message_id,)).get()
    async_result = send_outbound_message.delay(outbound_message_id)
    return {
        "outbound_message_id": outbound_message_id,
        "status": OutboundMessage.Status.QUEUED,
        "delivery_task_id": async_result.id,
    }


def get_client_channel(client) -> str:
    if client.whatsapp_id:
        return "whatsapp"
    if client.telegram_id:
        return "telegram"
    if client.phone:
        return "whatsapp"
    return "unknown"


def get_client_recipient(client, channel: str) -> str:
    if channel == "whatsapp":
        return client.whatsapp_id or str(client.phone)
    if channel == "telegram":
        return client.telegram_id or client.external_id
    return str(client.phone or client.external_id or "")


def get_or_create_outbound_message(
    *,
    booking,
    channel: str,
    recipient: str,
    message_type: str,
    text: str,
):
    lookup = {
        "booking": booking,
        "message_type": message_type,
    }
    existing_message = (
        OutboundMessage.objects.filter(
            **lookup,
            status__in=[
                OutboundMessage.Status.QUEUED,
                OutboundMessage.Status.SUBMITTED,
                OutboundMessage.Status.DELIVERED,
                OutboundMessage.Status.FAILED,
                OutboundMessage.Status.CANCELLED,
                OutboundMessage.Status.DEAD_LETTER,
            ],
        )
        .order_by("-created_at")
        .first()
    )
    if existing_message is not None:
        return existing_message, False

    try:
        with transaction.atomic():
            return (
                OutboundMessage.objects.create(
                    business=booking.business,
                    client=booking.client,
                    booking=booking,
                    channel=channel,
                    recipient=recipient,
                    message_type=message_type,
                    text=text,
                ),
                True,
            )
    except IntegrityError:
        return OutboundMessage.objects.get(**lookup), False


def build_existing_outbound_result(outbound_message: OutboundMessage) -> dict:
    payload = {
        "outbound_message_id": outbound_message.id,
        "status": outbound_message.status,
        "channel": outbound_message.channel,
        "text": outbound_message.text,
        "provider_message_id": outbound_message.provider_message_id,
    }
    if outbound_message.submitted_at:
        payload["submitted_at"] = outbound_message.submitted_at.isoformat()
    if outbound_message.delivered_at:
        payload["delivered_at"] = outbound_message.delivered_at.isoformat()
    return payload


def request_outbound_retry(
    *,
    outbound_message: OutboundMessage,
    actor_type: str = "human",
    actor_id: int | None = None,
    actor_name: str = "",
):
    if outbound_message.status != OutboundMessage.Status.FAILED:
        raise ValidationError("Only failed outbound messages can be retried.")

    create_audit_log(
        business=outbound_message.business,
        client=outbound_message.client,
        booking=outbound_message.booking,
        outbound_message=outbound_message,
        actor_type=actor_type,
        event_type="outbound_retry_requested",
        channel=outbound_message.channel,
        payload={
            "actor_id": actor_id,
            "actor_name": actor_name,
            "previous_status": outbound_message.status,
        },
    )
    return outbound_message, dispatch_outbound_delivery(outbound_message.id)


def request_outbound_resend(
    *,
    outbound_message: OutboundMessage,
    actor_type: str = "human",
    actor_id: int | None = None,
    actor_name: str = "",
):
    eligible_statuses = {
        OutboundMessage.Status.FAILED,
        OutboundMessage.Status.DEAD_LETTER,
        OutboundMessage.Status.CANCELLED,
    }
    if outbound_message.status not in eligible_statuses:
        raise ValidationError(
            "Only failed, dead-letter, or cancelled messages can be resent."
        )

    previous_status = outbound_message.status
    outbound_message.status = OutboundMessage.Status.QUEUED
    outbound_message.attempts = 0
    outbound_message.error_code = ""
    outbound_message.last_error = ""
    outbound_message.provider_message_id = ""
    outbound_message.provider_response = {}
    outbound_message.submitted_at = None
    outbound_message.delivered_at = None
    outbound_message.dead_lettered_at = None
    outbound_message.save(
        update_fields=[
            "status",
            "attempts",
            "error_code",
            "last_error",
            "provider_message_id",
            "provider_response",
            "submitted_at",
            "delivered_at",
            "dead_lettered_at",
            "updated_at",
        ]
    )
    create_audit_log(
        business=outbound_message.business,
        client=outbound_message.client,
        booking=outbound_message.booking,
        outbound_message=outbound_message,
        actor_type=actor_type,
        event_type="outbound_resend_requested",
        channel=outbound_message.channel,
        payload={
            "actor_id": actor_id,
            "actor_name": actor_name,
            "previous_status": previous_status,
        },
    )
    return outbound_message, dispatch_outbound_delivery(outbound_message.id)


def sync_booking_delivery_marker(outbound_message: OutboundMessage):
    if outbound_message.status != OutboundMessage.Status.DELIVERED:
        return

    if not outbound_message.booking_id:
        return

    booking = outbound_message.booking
    delivered_at = outbound_message.delivered_at or outbound_message.submitted_at
    if outbound_message.message_type == "reminder" and booking.reminder_sent_at is None:
        booking.reminder_sent_at = delivered_at
        booking.save(update_fields=["reminder_sent_at", "updated_at"])
    elif (
        outbound_message.message_type == "follow_up"
        and booking.follow_up_sent_at is None
    ):
        booking.follow_up_sent_at = delivered_at
        booking.save(update_fields=["follow_up_sent_at", "updated_at"])


def get_outbound_skip_reason(outbound_message: OutboundMessage) -> str:
    booking = outbound_message.booking
    if booking is None:
        return ""

    now = timezone.now()
    if outbound_message.message_type == "reminder":
        if booking.status != Booking.Status.CONFIRMED:
            return "booking_is_not_confirmed"
        if booking.reminder_sent_at is not None:
            return "reminder_already_delivered"
        if now >= booking.start_time:
            return "booking_start_time_already_passed"

    if outbound_message.message_type == "follow_up":
        if booking.status != Booking.Status.PENDING:
            return "booking_is_not_pending"
        if booking.follow_up_sent_at is not None:
            return "follow_up_already_delivered"
        if booking.client_id and not booking.client.allow_follow_up:
            return "client_opted_out"

    return ""


def cancel_obsolete_outbound(outbound_message: OutboundMessage, *, reason: str):
    outbound_message.status = OutboundMessage.Status.CANCELLED
    outbound_message.error_code = reason
    outbound_message.last_error = f"Outbound message cancelled: {reason}."
    outbound_message.save(
        update_fields=["status", "error_code", "last_error", "updated_at"]
    )
    create_audit_log(
        business=outbound_message.business,
        client=outbound_message.client,
        booking=outbound_message.booking,
        outbound_message=outbound_message,
        actor_type="system",
        event_type="outbound_cancelled",
        channel=outbound_message.channel,
        payload={"reason": reason},
    )
    return {
        "outbound_message_id": outbound_message.id,
        "status": outbound_message.status,
        "channel": outbound_message.channel,
        "error_code": outbound_message.error_code,
    }


def schedule_outbound_retry(outbound_message: OutboundMessage):
    if outbound_message.attempts >= settings.MAX_OUTBOUND_ATTEMPTS:
        return None
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return None

    eta = timezone.now() + timedelta(minutes=5)
    async_result = send_outbound_message.apply_async(
        args=(outbound_message.id,),
        eta=eta,
    )
    create_audit_log(
        business=outbound_message.business,
        client=outbound_message.client,
        booking=outbound_message.booking,
        outbound_message=outbound_message,
        actor_type="system",
        event_type="outbound_retry_scheduled",
        channel=outbound_message.channel,
        payload={
            "retry_task_id": async_result.id,
            "retry_eta": eta.isoformat(),
            "attempts": outbound_message.attempts,
        },
    )
    return {
        "delivery_task_id": async_result.id,
        "retry_eta": eta.isoformat(),
    }


def mark_outbound_as_failed(
    outbound_message: OutboundMessage,
    *,
    error_code: str,
    error_message: str,
):
    max_attempts = settings.MAX_OUTBOUND_ATTEMPTS
    outbound_message.error_code = error_code
    outbound_message.last_error = error_message
    if outbound_message.attempts >= max_attempts:
        outbound_message.status = OutboundMessage.Status.DEAD_LETTER
        outbound_message.dead_lettered_at = timezone.now()
        outbound_message.save(
            update_fields=[
                "status",
                "error_code",
                "last_error",
                "dead_lettered_at",
                "updated_at",
            ]
        )
        create_audit_log(
            business=outbound_message.business,
            client=outbound_message.client,
            booking=outbound_message.booking,
            outbound_message=outbound_message,
            actor_type="system",
            event_type="outbound_dead_letter",
            channel=outbound_message.channel,
            payload={
                "attempts": outbound_message.attempts,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
    else:
        outbound_message.status = OutboundMessage.Status.FAILED
        outbound_message.save(
            update_fields=["status", "error_code", "last_error", "updated_at"]
        )
        create_audit_log(
            business=outbound_message.business,
            client=outbound_message.client,
            booking=outbound_message.booking,
            outbound_message=outbound_message,
            actor_type="system",
            event_type="outbound_failed",
            channel=outbound_message.channel,
            payload={
                "attempts": outbound_message.attempts,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        return schedule_outbound_retry(outbound_message)
    return None


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_outbound_message(self, outbound_message_id: int):
    with sentry_task_scope(
        task_name=self.name,
        outbound_message_id=outbound_message_id,
    ):
        outbound_message = (
            OutboundMessage.objects.select_related("booking", "client")
            .filter(pk=outbound_message_id)
            .first()
        )
        if outbound_message is None:
            return {
                "outbound_message_id": outbound_message_id,
                "status": "not_found",
            }

        set_sentry_tags(
            business_id=outbound_message.business_id,
            client_id=outbound_message.client_id,
            booking_id=outbound_message.booking_id,
            outbound_message_id=outbound_message.id,
            channel=outbound_message.channel,
        )

        if outbound_message.status in {
            OutboundMessage.Status.SUBMITTED,
            OutboundMessage.Status.DELIVERED,
            OutboundMessage.Status.CANCELLED,
            OutboundMessage.Status.DEAD_LETTER,
        }:
            return {
                "outbound_message_id": outbound_message.id,
                "status": outbound_message.status,
                "channel": outbound_message.channel,
                "text": outbound_message.text,
                "provider_message_id": outbound_message.provider_message_id,
            }

        skip_reason = get_outbound_skip_reason(outbound_message)
        if skip_reason:
            return cancel_obsolete_outbound(outbound_message, reason=skip_reason)

        outbound_message.attempts += 1
        outbound_message.save(update_fields=["attempts", "updated_at"])

        try:
            transport = get_transport_for_channel(outbound_message.channel)
            result = transport.send_text(
                recipient=outbound_message.recipient,
                text=outbound_message.text,
                metadata={
                    "outbound_message_id": outbound_message.id,
                    "booking_id": outbound_message.booking_id,
                    "message_type": outbound_message.message_type,
                },
            )
        except Exception as error:
            logger.exception(
                "outbound_transport_failed",
                extra={
                    "business_id": outbound_message.business_id,
                    "client_id": outbound_message.client_id,
                    "booking_id": outbound_message.booking_id,
                    "outbound_message_id": outbound_message.id,
                    "channel": outbound_message.channel,
                },
            )
            retry_context = mark_outbound_as_failed(
                outbound_message,
                error_code="transport_exception",
                error_message=str(error),
            )
            result_payload = {
                "outbound_message_id": outbound_message.id,
                "status": outbound_message.status,
                "error_code": outbound_message.error_code,
                "error_message": outbound_message.last_error,
            }
            if retry_context:
                result_payload.update(retry_context)
            return result_payload

        outbound_message.provider_message_id = result.provider_message_id or ""
        outbound_message.provider_response = result.raw_response
        outbound_message.error_code = result.error_code or ""
        outbound_message.last_error = result.error_message or ""

        if result.accepted:
            outbound_message.status = (
                OutboundMessage.Status.DELIVERED
                if result.delivered
                else OutboundMessage.Status.SUBMITTED
            )
            outbound_message.submitted_at = timezone.now()
            if result.delivered:
                outbound_message.delivered_at = outbound_message.submitted_at
            outbound_message.save(
                update_fields=[
                    "provider_message_id",
                    "provider_response",
                    "error_code",
                    "last_error",
                    "status",
                    "submitted_at",
                    "delivered_at",
                    "updated_at",
                ]
            )
            set_sentry_tags(
                provider_message_id=outbound_message.provider_message_id,
                outbound_status=outbound_message.status,
            )
            logger.info(
                "outbound_message_submitted",
                extra={
                    "business_id": outbound_message.business_id,
                    "client_id": outbound_message.client_id,
                    "booking_id": outbound_message.booking_id,
                    "outbound_message_id": outbound_message.id,
                    "channel": outbound_message.channel,
                    "provider_message_id": outbound_message.provider_message_id,
                    "status": outbound_message.status,
                },
            )
            create_audit_log(
                business=outbound_message.business,
                client=outbound_message.client,
                booking=outbound_message.booking,
                outbound_message=outbound_message,
                actor_type="provider",
                event_type=(
                    "outbound_delivered"
                    if outbound_message.status == OutboundMessage.Status.DELIVERED
                    else "outbound_submitted"
                ),
                channel=outbound_message.channel,
                payload={
                    "provider_message_id": outbound_message.provider_message_id,
                    "status": outbound_message.status,
                    "response": outbound_message.provider_response,
                },
            )
            sync_booking_delivery_marker(outbound_message)
        else:
            retry_context = mark_outbound_as_failed(
                outbound_message,
                error_code=outbound_message.error_code or "provider_rejected",
                error_message=(
                    outbound_message.last_error
                    or "Provider rejected the message."
                ),
            )
            if retry_context:
                return {
                    "outbound_message_id": outbound_message.id,
                    "status": outbound_message.status,
                    "channel": outbound_message.channel,
                    "text": outbound_message.text,
                    "provider_message_id": outbound_message.provider_message_id,
                    "error_code": outbound_message.error_code,
                    **retry_context,
                }

        return {
            "outbound_message_id": outbound_message.id,
            "status": outbound_message.status,
            "channel": outbound_message.channel,
            "text": outbound_message.text,
            "provider_message_id": outbound_message.provider_message_id,
            "error_code": outbound_message.error_code,
        }


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_booking_reminder(self, booking_id: int):
    with sentry_task_scope(task_name=self.name, booking_id=booking_id):
        booking = (
            Booking.objects.select_related("client", "service", "business")
            .filter(pk=booking_id)
            .first()
        )
        if booking is None:
            return {"booking_id": booking_id, "status": "not_found"}

        set_sentry_tags(
            business_id=booking.business_id,
            client_id=booking.client_id,
            booking_id=booking.id,
            channel=get_client_channel(booking.client),
        )

        ai_manager = AIManager(business=booking.business)
        if not ai_manager.should_send_reminder(booking=booking):
            return {"booking_id": booking_id, "status": "skipped"}

        channel = get_client_channel(booking.client)
        outbound_message, created = get_or_create_outbound_message(
            booking=booking,
            channel=channel,
            recipient=get_client_recipient(booking.client, channel),
            message_type="reminder",
            text=ai_manager.build_reminder_message(booking=booking),
        )
        set_sentry_tags(
            outbound_message_id=outbound_message.id,
            message_type=outbound_message.message_type,
        )
        if created:
            logger.info(
                "outbound_message_queued",
                extra={
                    "booking_id": booking.id,
                    "outbound_message_id": outbound_message.id,
                    "message_type": "reminder",
                },
            )
            create_audit_log(
                business=booking.business,
                client=booking.client,
                booking=booking,
                outbound_message=outbound_message,
                actor_type="system",
                event_type="reminder_queued",
                channel=channel,
                payload={"message_type": "reminder"},
            )
        else:
            return build_existing_outbound_result(outbound_message)
        return dispatch_outbound_delivery(outbound_message.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_follow_up_if_pending(self, booking_id: int):
    with sentry_task_scope(task_name=self.name, booking_id=booking_id):
        booking = (
            Booking.objects.select_related("client", "service", "business")
            .filter(pk=booking_id)
            .first()
        )
        if booking is None:
            return {"booking_id": booking_id, "status": "not_found"}

        set_sentry_tags(
            business_id=booking.business_id,
            client_id=booking.client_id,
            booking_id=booking.id,
            channel=get_client_channel(booking.client),
        )

        ai_manager = AIManager(business=booking.business)
        if not ai_manager.should_send_follow_up(booking=booking):
            return {"booking_id": booking_id, "status": "skipped"}

        channel = get_client_channel(booking.client)
        outbound_message, created = get_or_create_outbound_message(
            booking=booking,
            channel=channel,
            recipient=get_client_recipient(booking.client, channel),
            message_type="follow_up",
            text=ai_manager.build_follow_up_message(
                client_name=booking.client.name or str(booking.client.phone),
                service_name=booking.service.name,
            ),
        )
        set_sentry_tags(
            outbound_message_id=outbound_message.id,
            message_type=outbound_message.message_type,
        )
        if created:
            logger.info(
                "outbound_message_queued",
                extra={
                    "booking_id": booking.id,
                    "outbound_message_id": outbound_message.id,
                    "message_type": "follow_up",
                },
            )
            create_audit_log(
                business=booking.business,
                client=booking.client,
                booking=booking,
                outbound_message=outbound_message,
                actor_type="ai",
                event_type="follow_up_queued",
                channel=channel,
                payload={"message_type": "follow_up"},
            )
        else:
            return build_existing_outbound_result(outbound_message)
        return dispatch_outbound_delivery(outbound_message.id)


@shared_task
def process_pending_reminders():
    with sentry_task_scope(task_name="apps.bookings.tasks.process_pending_reminders"):
        now = timezone.now()
        reminder_threshold = now + timedelta(hours=2)

        reminder_ids = list(
            Booking.objects.filter(
                status=Booking.Status.CONFIRMED,
                reminder_sent_at__isnull=True,
                start_time__lte=reminder_threshold,
                start_time__gte=now,
                client__is_active=True,
            )
            .values_list("id", flat=True)
        )
        follow_up_ids = list(
            Booking.objects.filter(
                status=Booking.Status.PENDING,
                follow_up_sent_at__isnull=True,
                created_at__lte=now - timedelta(hours=1),
                client__allow_follow_up=True,
                client__is_active=True,
            )
            .values_list("id", flat=True)
        )
        set_sentry_tags(
            reminders_queued=len(reminder_ids),
            follow_ups_queued=len(follow_up_ids),
        )

        for booking_id in reminder_ids:
            send_booking_reminder.delay(booking_id)

        for booking_id in follow_up_ids:
            send_follow_up_if_pending.delay(booking_id)

        return {
            "processed_at": now.isoformat(),
            "reminders_queued": len(reminder_ids),
            "follow_ups_queued": len(follow_up_ids),
        }


@shared_task
def process_outbound_health_alerts():
    with sentry_task_scope(
        task_name="apps.bookings.tasks.process_outbound_health_alerts"
    ):
        lookback_minutes = settings.OUTBOUND_ALERT_LOOKBACK_MINUTES
        window_start = timezone.now() - timedelta(minutes=lookback_minutes)
        failed_counts, dead_letter_counts = get_outbound_health_counts(
            window_start=window_start
        )

        # TODO: support per-business thresholds once salons have different
        # delivery baselines and traffic profiles.
        failed_threshold = settings.OUTBOUND_ALERT_FAILED_THRESHOLD
        dead_letter_threshold = settings.OUTBOUND_ALERT_DEAD_LETTER_THRESHOLD

        triggered_business_ids = sorted(
            business_id
            for business_id in set(failed_counts) | set(dead_letter_counts)
            if failed_counts.get(business_id, 0) >= failed_threshold
            or dead_letter_counts.get(business_id, 0) >= dead_letter_threshold
        )
        set_sentry_tags(
            triggered_businesses=len(triggered_business_ids),
            lookback_minutes=lookback_minutes,
        )

        if not settings.INTERNAL_ALERT_WEBHOOK_URL:
            logger.info(
                "outbound_health_alerts_skipped_unconfigured",
                extra={"triggered_businesses": len(triggered_business_ids)},
            )
            return {
                "processed_at": timezone.now().isoformat(),
                "alerts_sent": 0,
                "alerts_skipped": len(triggered_business_ids),
                "reason": "internal_alert_transport_not_configured",
            }

        sent_alerts = 0
        skipped_alerts = 0
        transport = get_transport_for_channel("internal")
        recipient = get_outbound_alert_recipient()

        for business in Business.objects.filter(
            pk__in=triggered_business_ids,
            is_active=True,
        ):
            failed_count = failed_counts.get(business.id, 0)
            dead_letter_count = dead_letter_counts.get(business.id, 0)
            set_sentry_tags(
                business_id=business.id,
                failed_count=failed_count,
                dead_letter_count=dead_letter_count,
            )

            if not claim_outbound_alert_cooldown(business_id=business.id):
                skipped_alerts += 1
                logger.info(
                    "outbound_health_alert_suppressed_by_cooldown",
                    extra={
                        "business_id": business.id,
                        "failed_count": failed_count,
                        "dead_letter_count": dead_letter_count,
                    },
                )
                continue

            alert_text = build_outbound_health_alert_message(
                business=business,
                failed_count=failed_count,
                dead_letter_count=dead_letter_count,
                lookback_minutes=lookback_minutes,
            )

            try:
                result = transport.send_text(
                    recipient=recipient,
                    text=alert_text,
                    metadata={
                        "business_id": business.id,
                        "failed_count": failed_count,
                        "dead_letter_count": dead_letter_count,
                        "window_start": window_start.isoformat(),
                    },
                )
            except Exception as error:
                logger.exception(
                    "outbound_health_alert_failed",
                    extra={
                        "business_id": business.id,
                        "failed_count": failed_count,
                        "dead_letter_count": dead_letter_count,
                        "channel": "internal",
                    },
                )
                create_audit_log(
                    business=business,
                    actor_type="system",
                    event_type="outbound_health_alert_failed",
                    channel="internal",
                    payload={
                        "failed_count": failed_count,
                        "dead_letter_count": dead_letter_count,
                        "window_start": window_start.isoformat(),
                        "error": str(error),
                    },
                )
                continue

            if not result.accepted:
                logger.warning(
                    "outbound_health_alert_rejected",
                    extra={
                        "business_id": business.id,
                        "failed_count": failed_count,
                        "dead_letter_count": dead_letter_count,
                        "channel": "internal",
                        "error_code": result.error_code,
                        "status": "failed",
                    },
                )
                create_audit_log(
                    business=business,
                    actor_type="system",
                    event_type="outbound_health_alert_failed",
                    channel="internal",
                    payload={
                        "failed_count": failed_count,
                        "dead_letter_count": dead_letter_count,
                        "window_start": window_start.isoformat(),
                        "provider_message_id": result.provider_message_id,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "response": result.raw_response,
                    },
                )
                continue

            sent_alerts += 1
            logger.warning(
                "outbound_health_alert_sent",
                extra={
                    "business_id": business.id,
                    "failed_count": failed_count,
                    "dead_letter_count": dead_letter_count,
                    "channel": "internal",
                    "provider_message_id": result.provider_message_id,
                    "status": "submitted",
                },
            )
            create_audit_log(
                business=business,
                actor_type="system",
                event_type="outbound_health_alert_sent",
                channel="internal",
                payload={
                    "failed_count": failed_count,
                    "dead_letter_count": dead_letter_count,
                    "window_start": window_start.isoformat(),
                    "provider_message_id": result.provider_message_id,
                    "response": result.raw_response,
                },
            )

        return {
            "processed_at": timezone.now().isoformat(),
            "alerts_sent": sent_alerts,
            "alerts_skipped": skipped_alerts,
            "triggered_businesses": len(triggered_business_ids),
        }


@shared_task(name="apps.bookings.tasks.async_prune_history")
def async_prune_history(*, business_id: int, client_id: int, channel: str):
    from .models import ConversationMessage

    ConversationMessage.prune_history(
        business_id=business_id,
        client_id=client_id,
        channel=channel,
    )
    return {
        "business_id": business_id,
        "client_id": client_id,
        "channel": channel,
        "status": "completed",
    }


@shared_task(name="apps.bookings.tasks.process_ai_interaction")
def process_ai_interaction(*, business_id: int, conversation_messages: list[dict]):
    with sentry_task_scope(
        task_name="apps.bookings.tasks.process_ai_interaction",
        business_id=business_id,
    ):
        from .models import Business

        business = Business.objects.get(pk=business_id, is_active=True)
        ai_manager = AIManager(business=business)
        reply = ai_manager.generate_reply(conversation_messages)
        return {
            "business_id": business_id,
            "reply": reply,
        }


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def notify_human_operator(
    self,
    booking_id: int | None = None,
    reason: str = "",
    attempts: int = 0,
):
    with sentry_task_scope(
        task_name=self.name,
        booking_id=booking_id,
    ):
        escalation_payload = AIManager().escalate_to_human(
            booking_id=booking_id,
            reason=reason,
            attempts=attempts,
        )
        booking = (
            Booking.objects.select_related("business", "client", "service")
            .filter(pk=booking_id)
            .first()
        )
        if booking is None:
            escalation_payload["notification_status"] = OutboundMessage.Status.FAILED
            escalation_payload["error_code"] = "booking_not_found_for_handoff"
            return escalation_payload

        set_sentry_tags(
            business_id=booking.business_id,
            client_id=booking.client_id,
            booking_id=booking.id,
            channel="internal",
        )

        create_audit_log(
            business=booking.business,
            client=booking.client,
            booking=booking,
            actor_type="ai",
            event_type="handoff_requested",
            channel="internal",
            payload={"reason": reason, "attempts": attempts},
        )

        message_text = (
            f"Handoff requested. Reason: {reason}. Attempts: {attempts}. "
            f"Booking ID: {booking_id or 'n/a'}."
        )
        outbound_message = OutboundMessage.objects.create(
            business=booking.business,
            client=booking.client,
            booking=booking,
            channel="internal",
            recipient=(
                settings.HUMAN_ESCALATION_CHAT_ID
                or settings.ADMIN_ALERT_EMAIL
                or "admin"
            ),
            message_type="handoff",
            text=message_text,
        )
        set_sentry_tags(
            outbound_message_id=outbound_message.id,
            message_type=outbound_message.message_type,
        )
        send_result = dispatch_outbound_delivery(outbound_message.id)
        escalation_payload["notification_status"] = send_result["status"]
        escalation_payload["notification_message_id"] = outbound_message.id
        if send_result.get("delivery_task_id"):
            escalation_payload["delivery_task_id"] = send_result["delivery_task_id"]
        return escalation_payload
