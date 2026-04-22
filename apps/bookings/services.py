from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Booking, Business, Client


DEFAULT_SLOT_STEP = timedelta(minutes=30)


@dataclass(frozen=True)
class TimeSlot:
    start: datetime
    end: datetime
    master_id: int
    master_name: str


def serialize_slot(slot: TimeSlot) -> dict:
    return {
        "start_time": slot.start.isoformat(),
        "end_time": slot.end.isoformat(),
        "master_id": slot.master_id,
        "master_name": slot.master_name,
    }


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def build_day_window(target_date: date):
    tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(
        datetime.combine(target_date, time.min),
        tz,
    )
    return day_start, day_start + timedelta(days=1)


def iter_master_slots(
    *,
    master,
    target_date: date,
    duration: timedelta,
    slot_step: timedelta,
):
    schedule = master.get_daily_schedule(target_date)
    if not schedule:
        return []

    tz = timezone.get_current_timezone()
    work_start = timezone.make_aware(
        datetime.combine(target_date, parse_clock(schedule["start"])),
        tz,
    )
    work_end = timezone.make_aware(
        datetime.combine(target_date, parse_clock(schedule["end"])),
        tz,
    )

    slots = []
    cursor = work_start
    while cursor + duration <= work_end:
        slots.append(
            TimeSlot(
                start=cursor,
                end=cursor + duration,
                master_id=master.id,
                master_name=master.full_name,
            )
        )
        cursor += slot_step

    return slots


def slot_overlaps(slot: TimeSlot, bookings):
    return any(
        booking["start_time"] < slot.end and booking["end_time"] > slot.start
        for booking in bookings
    )


def get_available_slots(
    business_id: int,
    *,
    target_date: date,
    service_id: int,
    master_id: int | None = None,
    slot_step: timedelta = DEFAULT_SLOT_STEP,
):
    business = Business.objects.get(pk=business_id, is_active=True)
    service = business.services.get(pk=service_id, is_active=True)
    masters = business.masters.filter(is_active=True)
    if master_id is not None:
        masters = masters.filter(pk=master_id)

    day_start, day_end = build_day_window(target_date)
    available_slots = []

    for master in masters:
        bookings = list(
            Booking.objects.active()
            .filter(
                business=business,
                master=master,
                start_time__lt=day_end,
                end_time__gt=day_start,
            )
            .values("start_time", "end_time")
        )
        for slot in iter_master_slots(
            master=master,
            target_date=target_date,
            duration=service.duration + service.buffer_time,
            slot_step=slot_step,
        ):
            if not slot_overlaps(slot, bookings):
                available_slots.append(slot)

    return sorted(
        available_slots,
        key=lambda slot: (slot.start, slot.master_id),
    )


@transaction.atomic
def create_appointment(
    business_id: int,
    *,
    master_id: int,
    service_id: int,
    client_id: int,
    start_time: datetime,
    client_data: dict,
    status: str = Booking.Status.PENDING,
):
    if timezone.is_naive(start_time):
        raise ValidationError(
            "start_time must include timezone information."
        )

    business = Business.objects.get(pk=business_id, is_active=True)
    master = business.masters.select_for_update().get(
        pk=master_id,
        is_active=True,
    )
    service = business.services.get(pk=service_id, is_active=True)
    client = business.clients.get(pk=client_id, is_active=True)
    provisional_end_time = start_time + service.duration + service.buffer_time

    conflicting_booking = (
        Booking.objects.active()
        .select_for_update()
        .filter(
            business=business,
            master=master,
            start_time__lt=provisional_end_time,
            end_time__gt=start_time,
        )
        .first()
    )
    if conflicting_booking is not None:
        raise ValidationError(
            "This time slot was just booked. Please choose another slot."
        )

    booking = Booking(
        business=business,
        master=master,
        service=service,
        client=client,
        start_time=start_time,
        status=status,
        client_data=client_data,
    )
    booking.full_clean()
    booking.save()
    return booking


OPENAI_FUNCTION_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_free_slots",
            "description": (
                "Используй эту функцию, когда клиент выразил желание "
                "записаться или спросил 'Когда есть свободное время?'. "
                "Тебе нужно передать ID услуги и желаемую дату. Если дата "
                "не указана, используй текущую дату. Получив список слотов, "
                "предложи клиенту 3 самых удобных варианта: утро, обед, вечер."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "business_id": {
                        "type": "integer",
                        "description": "Business identifier.",
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "Requested date in ISO YYYY-MM-DD format."
                        ),
                    },
                    "service_id": {
                        "type": "integer",
                        "description": "Requested service identifier.",
                    },
                    "master_id": {
                        "type": "integer",
                        "description": "Optional preferred master identifier.",
                    },
                },
                "required": ["date", "service_id", "business_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": (
                "Create a booking after the client confirms the slot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "business_id": {
                        "type": "integer",
                        "description": "Business identifier.",
                    },
                    "master_id": {
                        "type": "integer",
                        "description": "Selected master identifier.",
                    },
                    "service_id": {
                        "type": "integer",
                        "description": "Selected service identifier.",
                    },
                    "client_id": {
                        "type": "integer",
                        "description": "Existing client identifier.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Booking start time in ISO 8601 format "
                            "with timezone."
                        ),
                    },
                    "client_data": {
                        "type": "object",
                        "description": "Client profile and contact data.",
                    },
                },
                "required": [
                    "business_id",
                    "master_id",
                    "service_id",
                    "client_id",
                    "start_time",
                    "client_data",
                ],
                "additionalProperties": False,
            },
        },
    },
]


def execute_ai_function(
    *,
    function_name: str,
    payload: dict,
):
    if function_name in {"get_available_slots", "get_free_slots"}:
        slots = get_available_slots(
            payload["business_id"],
            target_date=date.fromisoformat(payload["date"]),
            service_id=payload["service_id"],
            master_id=payload.get("master_id"),
        )
        return [serialize_slot(slot) for slot in slots]

    if function_name == "create_appointment":
        booking = create_appointment(
            payload["business_id"],
            master_id=payload["master_id"],
            service_id=payload["service_id"],
            client_id=payload["client_id"],
            start_time=datetime.fromisoformat(payload["start_time"]),
            client_data=payload["client_data"],
        )
        return {
            "booking_id": booking.id,
            "status": booking.status,
            "start_time": booking.start_time.isoformat(),
            "end_time": booking.end_time.isoformat(),
        }

    raise ValidationError(f"Unsupported AI function: {function_name}")
