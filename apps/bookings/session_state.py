from __future__ import annotations

from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

from .models import BookingSession, Business, Client, Master, Service


def get_or_create_booking_session(*, business: Business, client: Client, channel: str) -> BookingSession:
    with transaction.atomic():
        session, created = BookingSession.objects.select_for_update().get_or_create(
            business=business,
            client=client,
            channel=channel,
        )
        if created:
            session.touch_expiration()
            session.save(update_fields=["expires_at", "updated_at"])
            return session

        if session.is_expired:
            session.reset()
        else:
            session.touch_expiration()
        session.save()
        return session


def clear_booking_session(session: BookingSession) -> BookingSession:
    session.reset()
    session.save()
    return session


def set_session_service(
    session: BookingSession,
    *,
    service: Service,
    language: str | None = None,
    source: str = "deterministic_flow",
) -> BookingSession:
    # reset() wipes context, which would drop reschedule_booking_id if the
    # client re-enters the service step mid-reschedule. Preserve the id
    # across the reset only for the reschedule flow — for a fresh booking
    # any stale marker must be cleared.
    preserved_reschedule_id = (
        session.context.get("reschedule_booking_id")
        if isinstance(session.context, dict)
        else None
    )
    session.reset(keep_expiration=True)
    session.service = service
    session.state = BookingSession.State.AWAITING_DATE
    session.context = {
        "service_name_snapshot": service.name,
        "language": language or "",
        "source": source,
    }
    if preserved_reschedule_id and source == "reschedule_flow":
        session.context["reschedule_booking_id"] = preserved_reschedule_id
    session.touch_expiration()
    session.save()
    return session


def serialize_slot_option(slot) -> dict:
    return {
        "start_time": slot.start.isoformat(),
        "end_time": slot.end.isoformat(),
        "master_id": slot.master_id,
        "master_name": slot.master_name,
    }


def set_session_slot_options(
    session: BookingSession,
    *,
    service: Service,
    target_date: date,
    slots: list,
    language: str | None = None,
) -> BookingSession:
    session.service = service
    session.master = None
    session.target_date = target_date
    session.selected_start_time = None
    session.selected_end_time = None
    session.slot_options = [serialize_slot_option(slot) for slot in slots]
    session.state = BookingSession.State.AWAITING_SLOT_CHOICE
    session.context = {
        **(session.context or {}),
        "language": language or session.context.get("language", ""),
    }
    session.touch_expiration()
    session.save()
    return session


def set_session_selected_slot(
    session: BookingSession,
    *,
    master: Master,
    start_time: datetime,
    end_time: datetime,
) -> BookingSession:
    session.master = master
    session.selected_start_time = start_time
    session.selected_end_time = end_time
    session.state = BookingSession.State.AWAITING_CONFIRMATION
    session.touch_expiration()
    session.save()
    return session
