"""Date and slot parsing helpers.

Pure functions for understanding calendar dates ("12 мая", "завтра",
"в субботу"), past-date detection, and slot-choice parsing for booking
sessions. Used by the message handler to resolve when the client wants
their appointment.

Dependencies: stdlib (datetime, re, types.SimpleNamespace),
django.utils.timezone, and text_utils._repair_mojibake. No models,
no webhooks imports — graph stays cycle-free.
"""

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from django.utils import timezone

from .text_utils import _repair_mojibake


# --- Name tables --------------------------------------------------------

MONTH_NAME_ALIASES = {
    "января": 1,
    "янв": 1,
    "ақпан": 2,
    "акпан": 2,
    "февраля": 2,
    "фев": 2,
    "наурыз": 3,
    "марта": 3,
    "мар": 3,
    "сәуір": 4,
    "сәуiр": 4,
    "сэуір": 4,
    "апреля": 4,
    "апр": 4,
    "мамыр": 5,
    "мая": 5,
    "май": 5,
    "маусым": 6,
    "июня": 6,
    "июн": 6,
    "шілде": 7,
    "шилде": 7,
    "июля": 7,
    "июл": 7,
    "тамыз": 8,
    "августа": 8,
    "авг": 8,
    "қыркүйек": 9,
    "кыркүйек": 9,
    "сентября": 9,
    "сен": 9,
    "сент": 9,
    "қазан": 10,
    "казан": 10,
    "октября": 10,
    "окт": 10,
    "қараша": 11,
    "караша": 11,
    "ноября": 11,
    "ноя": 11,
    "желтоқсан": 12,
    "желтоксан": 12,
    "декабря": 12,
    "дек": 12,
}

WEEKDAY_NAME_ALIASES = {
    0: (
        "понедельник",
        "понедельника",
        "пн",
        "дүйсенбі",
        "дуйсенби",
        "дүйсенбіге",
        "дуйсенбиге",
    ),
    1: (
        "вторник",
        "вторника",
        "вт",
        "сейсенбі",
        "сейсенби",
        "сейсенбіге",
        "сейсенбиге",
    ),
    2: (
        "среда",
        "среду",
        "ср",
        "сәрсенбі",
        "сарсенби",
        "сәрсенбіге",
        "сарсенбиге",
    ),
    3: (
        "четверг",
        "четверга",
        "чт",
        "бейсенбі",
        "бейсенби",
        "бейсенбіге",
        "бейсенбиге",
    ),
    4: (
        "пятница",
        "пятницу",
        "пт",
        "жұма",
        "жума",
        "жұмаға",
        "жумага",
    ),
    5: (
        "суббота",
        "субботу",
        "сб",
        "сенбі",
        "сенби",
        "сенбіге",
        "сенбиге",
    ),
    6: (
        "воскресенье",
        "воскресения",
        "воскресенье",
        "вс",
        "жексенбі",
        "жексенби",
        "жексенбіге",
        "жексенбиге",
    ),
}


# --- Calendar date parsing ----------------------------------------------

def resolve_day_in_current_or_next_month(day: int, *, today):
    for month_offset in range(13):
        month_index = today.month - 1 + month_offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        try:
            parsed = datetime(year, month, day).date()
        except ValueError:
            continue
        if parsed >= today:
            return parsed
    return None


def parse_full_calendar_date(text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None

    year_first_match = re.search(
        r"\b(\d{4})\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.|,)?\s*,?\s*(\d{1,2})\s+([^\W\d_]+)\b",
        normalized,
    )
    if year_first_match:
        year = int(year_first_match.group(1))
        day = int(year_first_match.group(2))
        month = MONTH_NAME_ALIASES.get(year_first_match.group(3))
        if month is None:
            return None
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    day_first_match = re.search(
        r"\b(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})(?:\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.))?\b",
        normalized,
    )
    if day_first_match:
        day = int(day_first_match.group(1))
        month = MONTH_NAME_ALIASES.get(day_first_match.group(2))
        year = int(day_first_match.group(3))
        if month is None:
            return None
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    return None


def parse_relative_weekday_date(text: str, *, today=None):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None
    today = today or timezone.localdate()
    for weekday, aliases in WEEKDAY_NAME_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized):
                days_ahead = (weekday - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                return today + timedelta(days=days_ahead)
    return None


def parse_explicit_calendar_date(text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None

    today = timezone.localdate()
    full_date = parse_full_calendar_date(normalized)
    if full_date is not None:
        return full_date if full_date >= today else None

    weekday_date = parse_relative_weekday_date(normalized, today=today)
    if weekday_date is not None:
        return weekday_date

    dot_match = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", normalized)
    if dot_match:
        day = int(dot_match.group(1))
        month = int(dot_match.group(2))
        year = int(dot_match.group(3)) if dot_match.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            parsed = datetime(year, month, day).date()
        except ValueError:
            return None
        if parsed < today:
            try:
                parsed = datetime(year + 1, month, day).date()
            except ValueError:
                return None
        return parsed

    day_only_match = re.search(
        r"(?:\b(?:на|к|ко)\s+)?\b(\d{1,2})(?:\s*(?:число|числа)|-?го)\b",
        normalized,
    )
    if day_only_match:
        return resolve_day_in_current_or_next_month(
            int(day_only_match.group(1)),
            today=today,
        )

    day_with_part_of_day_match = re.search(
        r"\b(?:на|к|ко)\s+(\d{1,2})(?![:.\d])\s+(?:ближе\s+к\s+)?(?:вечер|утр|дн|кеш)",
        normalized,
    )
    if day_with_part_of_day_match:
        return resolve_day_in_current_or_next_month(
            int(day_with_part_of_day_match.group(1)),
            today=today,
        )

    word_match = re.search(r"\b(\d{1,2})\s+([^\W\d_]+)\b", normalized)
    if not word_match:
        return None

    day = int(word_match.group(1))
    month = MONTH_NAME_ALIASES.get(word_match.group(2))
    if month is None:
        return None

    try:
        parsed = datetime(today.year, month, day).date()
    except ValueError:
        return None
    if parsed < today:
        try:
            parsed = datetime(today.year + 1, month, day).date()
        except ValueError:
            return None
    return parsed


def detect_explicit_past_calendar_date(text: str) -> bool:
    parsed = parse_full_calendar_date(text)
    return parsed is not None and parsed < timezone.localdate()


# --- Target date inference ---------------------------------------------

def infer_target_date_from_messages(*, texts: list[str], last_assistant_text: str):
    normalized_texts = [(text or "").strip().lower() for text in texts if (text or "").strip()]
    day_after_tomorrow_keywords = ("послезавтра", "бүрсігүні", "бурсикуни")
    tomorrow_keywords = ("завтра", "ертен", "ертең")
    today_keywords = ("сегодня", "бүгін", "бугин")

    for text in reversed(normalized_texts):
        if any(keyword in text for keyword in day_after_tomorrow_keywords):
            return timezone.localdate() + timedelta(days=2)
        if any(keyword in text for keyword in tomorrow_keywords):
            return timezone.localdate() + timedelta(days=1)
        if any(keyword in text for keyword in today_keywords):
            return timezone.localdate()

    assistant_text = (last_assistant_text or "").lower()
    if any(keyword in assistant_text for keyword in day_after_tomorrow_keywords):
        return timezone.localdate() + timedelta(days=2)
    if "завтра" in assistant_text or "ертең" in assistant_text or "ертен" in assistant_text:
        return timezone.localdate() + timedelta(days=1)
    return None


# --- Slot choice parsing -----------------------------------------------

def parse_slot_choice(text: str, *, slots: list):
    normalized = (text or "").strip().lower()
    if not normalized or not slots:
        return None

    if normalized.isdigit():
        numeric_value = int(normalized)
        if 1 <= numeric_value <= min(len(slots), 9):
            return slots[numeric_value - 1]
        if len(normalized) == 4:
            hour = int(normalized[:2])
            minute = int(normalized[2:])
            for slot in slots:
                local_start = timezone.localtime(slot.start)
                if local_start.hour == hour and local_start.minute == minute:
                    return slot

    # Preposition-anchored time wins over a bare number elsewhere in the
    # text: "23 только на 18" must choose 18:00, not 23:00. If a "на/к/ко/в"
    # phrase is present but no slot matches, we deliberately do NOT fall
    # back to the bare-number search — that would re-introduce the bug.
    preposition_match = re.search(
        r"\b(?:на|к|ко|в)\s+(\d{1,2})(?::(\d{2}))?\b",
        normalized,
    )
    if preposition_match:
        hour = int(preposition_match.group(1))
        minute = int(preposition_match.group(2) or 0)
        for slot in slots:
            local_start = timezone.localtime(slot.start)
            if local_start.hour == hour and local_start.minute == minute:
                return slot
        return None

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", normalized)
    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    for slot in slots:
        local_start = timezone.localtime(slot.start)
        if local_start.hour == hour and local_start.minute == minute:
            return slot
    return None


def deserialize_session_slot_options(slot_options: list[dict]):
    if not slot_options:
        return []

    lightweight_slots = []
    for option in slot_options:
        start_time_raw = option.get("start_time")
        if not start_time_raw:
            continue
        start_time = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
        end_time_raw = option.get("end_time") or start_time_raw
        end_time = datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
        lightweight_slots.append(
            SimpleNamespace(
                start=start_time,
                end=end_time,
                master_id=option.get("master_id"),
                master_name=option.get("master_name", ""),
            )
        )
    return lightweight_slots


def parse_session_slot_choice(*, text: str, slot_options: list[dict]):
    lightweight_slots = deserialize_session_slot_options(slot_options)
    return parse_slot_choice(text, slots=lightweight_slots)
