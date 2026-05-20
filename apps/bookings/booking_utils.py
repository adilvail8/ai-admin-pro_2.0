"""Slot and post-booking utilities.

Helpers used by the message handler when picking time slots for a new
booking, filtering by preference (morning / evening / specific time),
walking forward day-by-day to find availability, and deciding whether
to limit the AI conversation context after a booking is confirmed.

Dependencies:
- ``services.get_available_slots`` for the actual slot grid query.
- ``date_parser.deserialize_session_slot_options`` for parsing slot
  payloads stored in the session.
- ``text_utils._repair_mojibake`` for tolerating CP1251-corrupted
  client text inside preference parsing.
- ``models.Business`` / ``models.BookingSession`` (state enum + FKs).

No imports from webhooks/replies — graph stays one-way.
"""

import re
from datetime import date, timedelta

from django.utils import timezone

from .date_parser import deserialize_session_slot_options
from .models import Business, BookingSession
from .services import get_available_slots
from .text_utils import _repair_mojibake


def should_limit_post_booking_context(*, session: BookingSession, booking, text: str) -> bool:
    if booking is None or session.state != BookingSession.State.IDLE:
        return False

    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    if len(normalized) <= 80:
        return True

    return any(
        keyword in normalized
        for keyword in (
            "запис",
            "подтверж",
            "ок",
            "okay",
            "спасибо",
            "рахмет",
            "она",
            "он",
            "мастер",
        )
    )


def extract_slot_time_preference(text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None

    time_text = re.sub(
        r"\b(?:на|к|ко)\s+\d{1,2}(?!\s*[:.])(?:\s*(?:число|числа)|-?го)?\b",
        " ",
        normalized,
    )
    time_text = re.sub(
        r"\b\d{4}\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.)?\s*,?\s*\d{1,2}\s+[^\W\d_]+\b",
        " ",
        time_text,
    )
    time_text = re.sub(
        r"\b\d{1,2}\s+[^\W\d_]+\s+\d{4}(?:\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.))?\b",
        " ",
        time_text,
    )

    meridiem_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(вечера|вечером|вечере|кешке|кеш|утра|дня|днем|днём)\b",
        time_text,
    )
    if meridiem_match:
        hour = int(meridiem_match.group(1))
        minute = int(meridiem_match.group(2) or 0)
        period = meridiem_match.group(3)
        if period in {"вечера", "вечером", "вечере", "кешке", "кеш"} and hour < 12:
            hour += 12
        return {
            "kind": "exact",
            "hour": hour,
            "minute": minute,
        }

    # "Поздно вечером" — узкое окно 19-21. ДОЛЖНО проверяться ДО общего
    # вечера, иначе "поздно вечером" поймает вечер 17-21 и потеряет
    # семантику "ближе к закрытию".
    if any(
        keyword in normalized
        for keyword in (
            "поздно вечером", "поздним вечером", "под вечер",
            "ближе к закрытию", "кеш кешке",
        )
    ):
        return {
            "kind": "range",
            "start": (19, 0),
            "end": (21, 0),
            "label_ru": "поздно вечером",
            "label_kz": "кеш кешке",
        }

    if any(
        keyword in normalized
        for keyword in (
            "вечером", "вечер", "к вечеру", "вечерком", "ближе к вечеру",
            "кешке", "кеш",
        )
    ):
        return {
            "kind": "range",
            "start": (17, 0),
            "end": (21, 0),
            "label_ru": "вечером",
            "label_kz": "кешке",
        }

    if any(keyword in normalized for keyword in ("попозже", "позже", "кейінірек", "кейинирек")):
        return {
            "kind": "later",
            "label_ru": "попозже",
            "label_kz": "кейінірек",
        }

    if any(
        keyword in normalized
        for keyword in (
            "утром", "с утра", "к утру", "поутру", "пораньше",
            "танертен", "таңертең", "ертемен", "таңмен",
        )
    ):
        return {
            "kind": "range",
            "start": (8, 0),
            "end": (12, 0),
            "label_ru": "утром",
            "label_kz": "таңертең",
        }

    if any(
        keyword in normalized
        for keyword in ("днем", "днём", "после обеда", "тустен кейин", "түстен кейін")
    ):
        return {
            "kind": "range",
            "start": (12, 0),
            "end": (17, 0),
            "label_ru": "днем",
            "label_kz": "түстен кейін",
        }

    # "До обеда" — 10-12. Проверяется ПОСЛЕ блока "день" (где сидит
    # "после обеда"), но ДО блока "обед": иначе "до обеда" заматчится
    # на голое "обед".
    if any(
        keyword in normalized
        for keyword in ("до обеда", "перед обедом", "түскі асқа дейін")
    ):
        return {
            "kind": "range",
            "start": (10, 0),
            "end": (12, 0),
            "label_ru": "до обеда",
            "label_kz": "түскі асқа дейін",
        }

    # "Обед" / "полдень" — узкое окно 12-14. \b-граница защищает от
    # "необеденное" / случайных вхождений. "до обеда" / "после обеда" /
    # "перед обедом" уже отработали в блоках выше — сюда они не дойдут.
    if re.search(r"\b(обед\w*|полдень|полудн\w*|түс\w*)", normalized):
        return {
            "kind": "range",
            "start": (12, 0),
            "end": (14, 0),
            "label_ru": "в обед",
            "label_kz": "түс мезгілінде",
        }

    after_match = re.search(r"(?:после|кейін|кейин)\s*(\d{1,2})(?::?(\d{2}))?", time_text)
    if after_match:
        return {
            "kind": "from",
            "hour": int(after_match.group(1)),
            "minute": int(after_match.group(2) or 0),
        }

    compact_time_match = re.search(r"\b(\d{2})(\d{2})\b", time_text)
    if compact_time_match:
        hour = int(compact_time_match.group(1))
        minute = int(compact_time_match.group(2))
        if not (8 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return {
            "kind": "exact",
            "hour": hour,
            "minute": minute,
        }

    separated_time_matches = list(re.finditer(r"\b(\d{1,2})[.:](\d{2})\b", time_text))
    for separated_time_match in reversed(separated_time_matches):
        hour = int(separated_time_match.group(1))
        minute = int(separated_time_match.group(2))
        if 8 <= hour <= 23 and 0 <= minute <= 59:
            return {
                "kind": "exact",
                "hour": hour,
                "minute": minute,
            }

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", time_text)
    if not time_match:
        return None

    raw_match = time_match.group(0)
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)

    if time_text.strip().isdigit() and len(time_text.strip()) == 1:
        return None
    if ":" not in raw_match and time_match.group(2) is None and hour < 8:
        return None

    return {
        "kind": "exact",
        "hour": hour,
        "minute": minute,
    }


def filter_slots_by_time_preference(*, slots: list, preference: dict):
    if not slots or preference is None:
        return slots

    filtered = []
    for slot in slots:
        local_start = timezone.localtime(slot.start)
        slot_tuple = (local_start.hour, local_start.minute)
        if preference["kind"] == "range":
            if preference["start"] <= slot_tuple < preference["end"]:
                filtered.append(slot)
        elif preference["kind"] == "from":
            if slot_tuple >= (preference["hour"], preference["minute"]):
                filtered.append(slot)
        elif preference["kind"] == "exact":
            if slot_tuple == (preference["hour"], preference["minute"]):
                filtered.append(slot)
        elif preference["kind"] == "later":
            existing_slots = deserialize_session_slot_options(
                preference.get("slot_options", [])
            )
            if existing_slots:
                latest_existing = max(
                    (timezone.localtime(item.start).hour, timezone.localtime(item.start).minute)
                    for item in existing_slots
                )
                if slot_tuple > latest_existing:
                    filtered.append(slot)
    return filtered


def find_next_available_slots(*, business: Business, service, target_date, max_days: int = 14):
    fallback_date = target_date
    for offset in range(max_days):
        candidate_date = target_date + timedelta(days=offset)
        slots = get_available_slots(
            business,
            target_date=candidate_date,
            service_id=service.id,
        )
        if slots:
            return candidate_date, slots
    return fallback_date, []
