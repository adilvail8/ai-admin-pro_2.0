import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .ai_manager import AIManager
from .models import Booking, OutboundMessage
from .transports import get_transport_for_channel


logger = logging.getLogger(__name__)


def get_client_channel(client) -> str:
    if client.whatsapp_id:
        return "whatsapp"
    if client.telegram_id:
        return "telegram"
    return "unknown"


def get_client_recipient(client, channel: str) -> str:
    if channel == "whatsapp":
        return client.whatsapp_id or str(client.phone)
    if channel == "telegram":
        return client.telegram_id or client.external_id
    return ""


def get_or_create_outbound_message(
    *,
    booking,
    channel: str,
    recipient: str,
    message_type: str,
    text: str,
):
    existing_message = (
        OutboundMessage.objects.filter(
            booking=booking,
            message_type=message_type,
            status__in=[
                OutboundMessage.Status.QUEUED,
                OutboundMessage.Status.SUBMITTED,
                OutboundMessage.Status.DELIVERED,
            ],
        )
        .order_by("-created_at")
        .first()
    )
    if existing_message is not None:
        return existing_message, False

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


def sync_booking_delivery_marker(outbound_message: OutboundMessage):
    if outbound_message.status not in {
        OutboundMessage.Status.SUBMITTED,
        OutboundMessage.Status.DELIVERED,
    }:
        return

    if not outbound_message.booking_id:
        return

    booking = outbound_message.booking
    if outbound_message.message_type == "reminder" and booking.reminder_sent_at is None:
        booking.reminder_sent_at = outbound_message.submitted_at
        booking.save(update_fields=["reminder_sent_at", "updated_at"])
    elif (
        outbound_message.message_type == "follow_up"
        and booking.follow_up_sent_at is None
    ):
        booking.follow_up_sent_at = outbound_message.submitted_at
        booking.save(update_fields=["follow_up_sent_at", "updated_at"])


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def send_outbound_message(self, outbound_message_id: int):
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

    if outbound_message.status in {
        OutboundMessage.Status.SUBMITTED,
        OutboundMessage.Status.DELIVERED,
    }:
        return {
            "outbound_message_id": outbound_message.id,
            "status": outbound_message.status,
            "channel": outbound_message.channel,
            "text": outbound_message.text,
            "provider_message_id": outbound_message.provider_message_id,
        }

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
                "outbound_message_id": outbound_message.id,
                "channel": outbound_message.channel,
            },
        )
        outbound_message.status = OutboundMessage.Status.FAILED
        outbound_message.error_code = "transport_exception"
        outbound_message.last_error = str(error)
        outbound_message.save(
            update_fields=["status", "error_code", "last_error", "updated_at"]
        )
        return {
            "outbound_message_id": outbound_message.id,
            "status": outbound_message.status,
            "error_code": outbound_message.error_code,
            "error_message": outbound_message.last_error,
        }

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
        sync_booking_delivery_marker(outbound_message)
    else:
        outbound_message.status = OutboundMessage.Status.FAILED
        outbound_message.save(
            update_fields=[
                "provider_message_id",
                "provider_response",
                "error_code",
                "last_error",
                "status",
                "updated_at",
            ]
        )

    return {
        "outbound_message_id": outbound_message.id,
        "status": outbound_message.status,
        "channel": outbound_message.channel,
        "text": outbound_message.text,
        "provider_message_id": outbound_message.provider_message_id,
        "error_code": outbound_message.error_code,
    }


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def send_booking_reminder(self, booking_id: int):
    booking = (
        Booking.objects.select_related("client", "service", "business")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        return {"booking_id": booking_id, "status": "not_found"}

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
    if created:
        logger.info(
            "outbound_message_queued",
            extra={
                "booking_id": booking.id,
                "outbound_message_id": outbound_message.id,
                "message_type": "reminder",
            },
        )
    return send_outbound_message.run(outbound_message.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def send_follow_up_if_pending(self, booking_id: int):
    booking = (
        Booking.objects.select_related("client", "service", "business")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        return {"booking_id": booking_id, "status": "not_found"}

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
    if created:
        logger.info(
            "outbound_message_queued",
            extra={
                "booking_id": booking.id,
                "outbound_message_id": outbound_message.id,
                "message_type": "follow_up",
            },
        )
    return send_outbound_message.run(outbound_message.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def notify_human_operator(
    self,
    booking_id: int | None = None,
    reason: str = "",
    attempts: int = 0,
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
    send_result = send_outbound_message.run(outbound_message.id)
    escalation_payload["notification_status"] = send_result["status"]
    escalation_payload["notification_message_id"] = outbound_message.id
    return escalation_payload
