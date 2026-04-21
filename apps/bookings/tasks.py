from django.utils import timezone

from celery import shared_task

from .ai_manager import AIManager
from .models import Booking


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def send_booking_reminder(self, booking_id: int):
    booking = (
        Booking.objects.select_related("client", "service")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        return {"booking_id": booking_id, "status": "not_found"}

    ai_manager = AIManager()
    if not ai_manager.should_send_reminder(booking=booking):
        return {"booking_id": booking_id, "status": "skipped"}

    reminder_message = ai_manager.build_reminder_message(booking=booking)
    booking.reminder_sent_at = timezone.now()
    booking.save(update_fields=["reminder_sent_at", "updated_at"])
    return {
        "booking_id": booking_id,
        "status": "ready_to_send",
        "message": reminder_message,
        "channel": (
            "whatsapp"
            if booking.client.whatsapp_id
            else "telegram" if booking.client.telegram_id else "unknown"
        ),
    }


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True)
def send_follow_up_if_pending(self, booking_id: int):
    booking = (
        Booking.objects.select_related("client", "service")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        return {"booking_id": booking_id, "status": "not_found"}

    ai_manager = AIManager()
    if not ai_manager.should_send_follow_up(booking=booking):
        return {"booking_id": booking_id, "status": "skipped"}

    follow_up_message = ai_manager.build_follow_up_message(
        client_name=booking.client.name or str(booking.client.phone),
        service_name=booking.service.name,
    )
    booking.follow_up_sent_at = timezone.now()
    booking.save(update_fields=["follow_up_sent_at", "updated_at"])
    return {
        "booking_id": booking_id,
        "status": "ready_to_send",
        "message": follow_up_message,
    }


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
