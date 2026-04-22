import logging

from celery import shared_task
from django.utils import timezone

from .ai_manager import AIManager
from .models import Booking, OutboundMessage


logger = logging.getLogger(__name__)


def get_client_channel(client) -> str:
    if client.whatsapp_id:
        return "whatsapp"
    if client.telegram_id:
        return "telegram"
    return "unknown"


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

    outbound_message.attempts += 1
    outbound_message.save(update_fields=["attempts", "updated_at"])

    outbound_message.status = OutboundMessage.Status.SENT
    outbound_message.sent_at = timezone.now()
    outbound_message.last_error = ""
    outbound_message.save(
        update_fields=["status", "sent_at", "last_error", "updated_at"]
    )

    if outbound_message.booking_id:
        booking = outbound_message.booking
        if outbound_message.message_type == "reminder":
            booking.reminder_sent_at = outbound_message.sent_at
            booking.save(update_fields=["reminder_sent_at", "updated_at"])
        elif outbound_message.message_type == "follow_up":
            booking.follow_up_sent_at = outbound_message.sent_at
            booking.save(update_fields=["follow_up_sent_at", "updated_at"])

    return {
        "outbound_message_id": outbound_message.id,
        "status": outbound_message.status,
        "channel": outbound_message.channel,
        "text": outbound_message.text,
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

    outbound_message = OutboundMessage.objects.create(
        business=booking.business,
        client=booking.client,
        booking=booking,
        channel=get_client_channel(booking.client),
        message_type="reminder",
        text=ai_manager.build_reminder_message(booking=booking),
    )
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

    outbound_message = OutboundMessage.objects.create(
        business=booking.business,
        client=booking.client,
        booking=booking,
        channel=get_client_channel(booking.client),
        message_type="follow_up",
        text=ai_manager.build_follow_up_message(
            client_name=booking.client.name or str(booking.client.phone),
            service_name=booking.service.name,
        ),
    )
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
    escalation_payload["notification_status"] = "ready_to_send"
    return escalation_payload
