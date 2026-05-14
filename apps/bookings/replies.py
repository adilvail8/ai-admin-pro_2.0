"""Reply builders for client-facing messages.

All functions return a localized string for the bot to send. They depend on:
- language helpers (format_*, localize_*)
- ORM models (read-only access to Service/Master/Business/Booking/BookingSession)
- service_matcher helpers (for recommendation and option replies)

No imports from webhooks.py — the dependency graph stays one-way so the
module can be loaded without circular issues.
"""

from datetime import datetime, timedelta
from itertools import islice

from django.utils import timezone

from .language import (
    format_local_date,
    format_local_datetime,
    localize_service_name,
)
from .models import Booking, BookingSession, Business, Master
from .service_matcher import (
    get_service_recommended_masters,
    infer_service_from_messages,
)


# --- Tier 1: pure (no model/helper dependencies) --------------------------
def build_greeting_reply(*, language: str) -> str:
    if language == "kz":
        return "Сәлеметсіз бе! Қандай қызметке жазылайын?"
    return "Здравствуйте! На какую услугу хотите записаться?"


def build_booking_intent_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қандай қызметке және қай күнге жазайын?"
    return "На какую услугу и на какой день записать?"


def build_out_of_scope_reply(*, language: str) -> str:
    if language == "kz":
        return "Бұл тақырыпқа көмектесе алмаймын. Мен салон қызметтері, баға, шеберлер және жазылу бойынша көмектесемін."
    return "С этим не помогу. Я отвечаю только по услугам, ценам, мастерам и записи в салон."


def build_gratitude_reply(*, language: str) -> str:
    if language == "kz":
        return "Оқасы жоқ."
    return "Пожалуйста."


def build_haircut_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қайсысы керек: ерлер шаш қиюы ма, әлде әйелдер шаш қиюы ма?"
    return "Уточните, пожалуйста: мужская или женская стрижка?"


def build_cancellation_handoff_reply(*, language: str) -> str:
    """Reply used when the bot escalates a cancellation to a human operator.

    Triggered either by late-escalation (less than
    ``Business.cancellation_policy_hours`` until start) or by any other
    code path that chooses to hand off rather than auto-cancel.
    """
    if language == "kz":
        return "Түсіндім, жазбаны тоқтату өтінішіңізді әкімшіге бірден жіберемін. Ол сізбен жақын арада байланысады."
    return "Поняла, передам администратору запрос на отмену записи. Он свяжется с вами и подтвердит отмену."


def build_cancellation_no_active_bookings_reply(*, language: str) -> str:
    """Reply when the client asks to cancel but has no active bookings."""
    if language == "kz":
        return (
            "Сізде қазір белсенді жазба көрінбейді. Жазылғыңыз келсе — "
            "қандай қызметке және қашан керек, жаза салыңыз."
        )
    return (
        "У вас сейчас нет активных записей. Если захотите записаться — "
        "напишите, на какую услугу и когда, я подберу время."
    )


def build_cancellation_multiple_bookings_reply(
    *, bookings: list, language: str
) -> str:
    """Numbered list of active bookings + prompt to pick one to cancel."""
    lines = []
    for index, booking in enumerate(bookings, start=1):
        service_name = localize_service_name(booking.service.name, language)
        when_label = format_local_datetime(booking.start_time, language=language)
        lines.append(f"{index}. {service_name} — {when_label}")
    listing = "\n".join(lines)

    if language == "kz":
        return (
            "Сізде бірнеше белсенді жазба бар:\n"
            f"{listing}\n"
            "Қайсысын тоқтатамыз? Нөмірін жазыңыз."
        )
    return (
        "У вас несколько активных записей:\n"
        f"{listing}\n"
        "Какую отменить? Напишите номер."
    )


def build_cancellation_confirmation_prompt(*, booking, language: str) -> str:
    """Yes/no prompt before actually cancelling a booking."""
    service_name = localize_service_name(booking.service.name, language)
    when_label = format_local_datetime(booking.start_time, language=language)
    if language == "kz":
        return (
            f"{service_name} ({when_label}) жазбасын тоқтатуға растайсыз ба? "
            "«Иә» немесе «жоқ» деп жаза салыңыз."
        )
    return (
        f"Точно отменить запись: {service_name} ({when_label})? "
        "Напишите «да» или «нет»."
    )


def build_cancellation_aborted_reply(*, language: str) -> str:
    """Reply when the client rejects the cancellation confirmation prompt."""
    if language == "kz":
        return (
            "Жақсы, жазбаны тоқтатпаймын. Тағы бірдеңе керек болса — жаза салыңыз."
        )
    return (
        "Хорошо, не отменяю запись. Если что-то ещё нужно — напишите."
    )


def build_cancellation_success_reply(*, booking, language: str) -> str:
    """Confirmation that a booking has been cancelled."""
    service_name = localize_service_name(booking.service.name, language)
    when_label = format_local_datetime(booking.start_time, language=language)
    if language == "kz":
        return (
            f"Дайын, {service_name} ({when_label}) жазбасы тоқтатылды. "
            "Ауыстырғыңыз келсе — жаза салыңыз, жаңа уақыт қарап беремін."
        )
    return (
        f"Готово, запись на {service_name} ({when_label}) отменена. "
        "Если захотите перенести — напишите, я подберу новое время."
    )


def build_reschedule_no_active_bookings_reply(*, language: str) -> str:
    """Reply when the client wants to move a booking but has none."""
    if language == "kz":
        return (
            "Сізде қазір белсенді жазба көрінбейді. "
            "Жазылғыңыз келсе — қандай қызмет керек, жаза салыңыз 😊"
        )
    return (
        "У вас сейчас нет активных записей. "
        "Если хотите записаться — напишите какая услуга вас интересует 😊"
    )


def build_reschedule_multiple_bookings_reply(*, bookings: list, language: str) -> str:
    """Numbered list of active bookings + prompt to pick one to reschedule."""
    lines = []
    for index, booking in enumerate(bookings, start=1):
        service_name = localize_service_name(booking.service.name, language)
        when_label = format_local_datetime(booking.start_time, language=language)
        lines.append(f"{index}. {service_name} — {when_label}")
    listing = "\n".join(lines)

    if language == "kz":
        return (
            "Сізде бірнеше жазба бар, қайсысын ауыстырамыз?\n"
            f"{listing}"
        )
    return (
        "У вас несколько записей, какую хотите перенести?\n"
        f"{listing}"
    )


def build_reschedule_late_escalation_reply(*, booking, language: str) -> str:
    """Reply when the requested reschedule is too close to the booking start.

    Mirrors the cancellation late-escalation pattern: bot doesn't move
    the booking itself, hands the request to a human operator who can
    decide on the spot.
    """
    if language == "kz":
        return (
            "Жазбаға дейін уақыт өте аз қалды — өтінішіңізді "
            "әкімшіге жіберемін, ол сізбен байланысып, ауыстыруға "
            "көмектеседі 🙏"
        )
    return (
        "До записи совсем мало времени — передаю запрос администратору, "
        "он свяжется с вами и поможет перенести 🙏"
    )


def build_reschedule_success_reply(*, booking, language: str) -> str:
    """Confirmation that a booking has been moved to the new slot."""
    service_name = localize_service_name(booking.service.name, language)
    local_start = timezone.localtime(booking.start_time)
    date_label = format_local_date(local_start.date(), language=language)
    time_label = f"{local_start:%H:%M}"
    master_name = booking.master.full_name
    if language == "kz":
        return (
            "✅ Дайын! Жазба ауыстырылды:\n"
            f"📅 {date_label}, {time_label}\n"
            f"💅 {service_name} — Шебер {master_name}\n"
            "Бір сағат бұрын еске саламын 😊"
        )
    return (
        "✅ Готово! Запись перенесена:\n"
        f"📅 {date_label} в {time_label}\n"
        f"💅 {service_name} — Мастер {master_name}\n"
        "За час напомню 😊"
    )


def build_reschedule_initiated_reply(*, booking, language: str) -> str:
    """Bot has accepted the reschedule request for a specific booking and is
    asking the client which new date works.

    Hands off into the standard date/slot/confirmation flow (the same
    one used for fresh bookings) — that flow will detect the reschedule
    context flag on the session and call reschedule_appointment instead
    of create_appointment at the final confirm step.
    """
    service_name = localize_service_name(booking.service.name, language)
    when_label = format_local_datetime(booking.start_time, language=language)
    if language == "kz":
        return (
            "Жақсы, ауыстырайық!\n"
            f"Қазіргі жазба: {service_name}, {when_label}.\n"
            "Қай күнге ауыстырамыз?"
        )
    return (
        "Хорошо, давайте перенесём!\n"
        f"Текущая запись: {service_name}, {when_label}.\n"
        "На какую дату удобно перенести?"
    )


def build_price_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қай қызметтің бағасы керек екенін жазыңыз."
    return "Напишите, пожалуйста, какая услуга интересует."


# --- Tier 2: language helpers + models ---------------------------------

def build_master_opinion_reply(*, master: Master, service=None, language: str) -> str:
    service_name = localize_service_name(service.name, language) if service is not None else ""
    specialization = (master.specialization or "").strip().lower()
    if language == "kz":
        if service is not None:
            return f"{master.full_name} осы {service_name} бойынша жұмыс істейді. Қаласаңыз, осы шеберге жазып қоямын."
        return f"{master.full_name} — {specialization}. Қай қызмет керек екенін жаза салыңыз, бірден бағыттаймын."
    if service is not None:
        return f"{master.full_name} у нас работает по услуге «{service_name}». Если хотите, могу записать к нему."
    return f"{master.full_name} — {specialization}. Напишите, на что хотите записаться, и я сразу сориентирую."


def build_unknown_master_reply(*, service, candidate: str, actual_master_name: str | None, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        if actual_master_name:
            return (
                f"{candidate.capitalize()} деген шеберді таппадым. "
                f"{service_name} үшін бұл уақытта қолжетімді шебер: {actual_master_name}."
            )
        return (
            f"{candidate.capitalize()} деген шеберді таппадым. "
            f"{service_name} үшін нақты шеберді тізімнен ұсынамын."
        )
    if actual_master_name:
        return (
            f"Мастера «{candidate.capitalize()}» у нас не вижу. "
            f"Для услуги «{service_name}» на это время доступен мастер {actual_master_name}."
        )
    return (
        f"Мастера «{candidate.capitalize()}» у нас не вижу. "
        f"Для услуги «{service_name}» могу подсказать реальных мастеров из салона."
    )


def build_session_master_mismatch_reply(
    *,
    session: BookingSession,
    mentioned_master: Master,
    language: str,
) -> str:
    service_name = localize_service_name(session.service.name, language)
    current_master_name = session.master.full_name if session.master_id and session.master is not None else ""

    if language == "kz":
        if current_master_name:
            return (
                f"{mentioned_master.full_name} шебері {mentioned_master.specialization.lower()} бағытымен жұмыс істейді. "
                f"Ал {service_name} бойынша қазір {current_master_name} шебері таңдалған."
            )
        return (
            f"{mentioned_master.full_name} шебері {mentioned_master.specialization.lower()} бағытымен жұмыс істейді. "
            f"{service_name} үшін лайықты шеберді ұсынып беремін."
        )

    if current_master_name:
        return (
            f"{mentioned_master.full_name} — мастер по направлению {mentioned_master.specialization.lower()}. "
            f"Для услуги «{service_name}» сейчас у вас выбран мастер {current_master_name}."
        )
    return (
        f"{mentioned_master.full_name} работает как {mentioned_master.specialization.lower()}. "
        f"Для услуги «{service_name}» я лучше подскажу подходящего мастера."
    )


def build_session_master_match_reply(
    *,
    session: BookingSession,
    mentioned_master: Master,
    language: str,
) -> str:
    service_name = localize_service_name(session.service.name, language)
    if language == "kz":
        return (
            f"Иә, {mentioned_master.full_name} шебері {service_name} бойынша қолайлы. "
            "Қаласаңыз, осы шебермен жалғастырамыз."
        )
    return (
        f"Да, {mentioned_master.full_name} подходит для услуги «{service_name}». "
        "Если хотите, продолжим запись с этим мастером."
    )


def build_time_preference_unavailable_reply(*, service, preference: dict, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if preference["kind"] == "exact":
        time_label = f"{preference['hour']:02d}:{preference['minute']:02d}"
        if language == "kz":
            return f"{service_name} үшін {time_label}-ге бос уақыт көріп тұрғам жоқ."
        return f"На {time_label} по услуге «{service_name}» свободного времени не вижу."

    if preference["kind"] == "from":
        time_label = f"{preference['hour']:02d}:{preference['minute']:02d}"
        if language == "kz":
            return f"{service_name} үшін {time_label}-ден кейін бос уақыт көріп тұрғам жоқ."
        return f"После {time_label} по услуге «{service_name}» свободного времени не вижу."

    label = preference.get("label_kz") if language == "kz" else preference.get("label_ru")
    if language == "kz":
        return f"{service_name} үшін {label} бос уақыт көріп тұрғам жоқ."
    return f"На {label} по услуге «{service_name}» свободного времени не вижу."


def build_slot_options_reply(*, service, slots: list, language: str) -> str:
    top_slots = slots[:3]
    if not top_slots:
        if language == "kz":
            return (
                f"Әзірге {localize_service_name(service.name, language)} бойынша бос уақыт табылмады. "
                "Қаласаңыз, басқа күнді қарап беремін."
            )
        return (
            f"Пока не вижу свободных слотов на {localize_service_name(service.name, language)}. "
            "Если хотите, посмотрю другой день."
        )

    same_master = len({slot.master_id for slot in top_slots}) == 1
    slot_lines = []
    for index, slot in enumerate(top_slots, start=1):
        local_start = timezone.localtime(slot.start)
        if same_master:
            slot_lines.append(f"{index}. {local_start:%H:%M}")
        else:
            slot_lines.append(f"{index}. {local_start:%H:%M} — {slot.master_name}")
    slots_text = "\n".join(slot_lines)
    service_name = localize_service_name(service.name, language)
    master_name = top_slots[0].master_name
    actual_date = timezone.localtime(top_slots[0].start).date()
    date_label = format_local_date(actual_date, language=language)

    if language == "kz":
        if same_master:
            return (
                f"{date_label} күні {service_name} үшін {master_name} шеберінде мына уақыттар бос:\n\n"
                f"{slots_text}\n\nҚайсысы ыңғайлы?"
            )
        return (
            f"{date_label} күні {service_name} үшін мына бос уақыттар бар:\n\n"
            f"{slots_text}\n\nҚайсысы ыңғайлы?"
        )

    if same_master:
        return (
        f"На {date_label} есть такие варианты на {service_name} у мастера {master_name}:\n\n"
            f"{slots_text}\n\nКакой вариант вам удобнее?"
        )
    return (
        f"На {date_label} есть такие варианты на {service_name}:\n\n"
        f"{slots_text}\n\nКакой вариант вам удобнее?"
    )

    top_slots = slots[:3]
    if not top_slots:
        if language == "kz":
            return f"Әзірге {localize_service_name(service.name, language)} бойынша бос уақыт табылмады. Қаласаңыз, басқа күнді қарап беремін."
        return f"Пока не вижу свободных слотов на {localize_service_name(service.name, language)}. Если хотите, посмотрю другой день."

    same_master = len({slot.master_id for slot in top_slots}) == 1
    slot_lines = []
    for index, slot in enumerate(top_slots, start=1):
        local_start = timezone.localtime(slot.start)
        if same_master:
            slot_lines.append(f"{index}. {local_start:%H:%M}")
        else:
            slot_lines.append(f"{index}. {local_start:%H:%M} — {slot.master_name}")
    slots_text = "\n".join(slot_lines)
    service_name = localize_service_name(service.name, language)
    master_name = top_slots[0].master_name

    if language == "kz":
        if same_master:
            return (
            f"Ертең {service_name} үшін {master_name} шеберінде мына уақыттар бос:\n\n"
                f"{slots_text}\n\nҚайсысы ыңғайлы?"
            )
        return (
            f"Ертең {service_name} үшін мына бос уақыттар бар:\n\n"
            f"{slots_text}\n\nҚайсысы ыңғайлы?"
        )

    if same_master:
        return (
            f"На завтра для услуги «{service_name}» у мастера {master_name} есть такие варианты:\n\n"
            f"{slots_text}\n\nКакой вариант вам удобнее?"
        )
    return (
            f"На завтра для услуги «{service_name}» есть такие варианты:\n\n"
        f"{slots_text}\n\nКакой вариант вам удобнее?"
    )


def build_booking_confirmation_reply(*, service, slot, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    local_start_label = format_local_datetime(slot.start, language=language)
    if language == "kz":
        return f"{service_name}: {local_start_label}, шебер {slot.master_name}. Растайсыз ба?"
    return f"Записать на «{service_name}» {local_start_label}, мастер {slot.master_name}?"


def build_date_selection_reply(*, service, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        return f"{service_name} қызметіне жазуға болады. Ертеңге қарайық па, әлде басқа күн керек пе?"
    return f"Ок, {service_name}. На завтра смотрим или нужен другой день?"


def build_booking_created_reply(*, service_name: str, local_start: datetime, master_name: str, language: str) -> str:
    local_start_label = format_local_datetime(local_start, language=language)
    if language == "kz":
        return f"Жаздым: {service_name}, {local_start_label}, шебер {master_name}."
    return f"Записала: {service_name}, {local_start_label}, мастер {master_name}."


def build_existing_booking_reply(*, booking: Booking, language: str) -> str:
    local_start_label = format_local_datetime(booking.start_time, language=language)
    service_name = localize_service_name(booking.service.name, language)
    master_name = booking.master.full_name
    if booking.status == Booking.Status.CONFIRMED:
        if language == "kz":
            return f"Иә, жазба нақтыланды: {service_name}, {local_start_label}, шебер {master_name}."
        return f"Да, запись подтверждена: «{service_name}», {local_start_label}, мастер {master_name}."
    if language == "kz":
        return f"Жазба әлі нақтыланбаған: {service_name}, {local_start_label}, шебер {master_name}. Растайсыз ба?"
    return f"Запись еще не подтверждена: «{service_name}», {local_start_label}, мастер {master_name}. Подтвердите?"


def build_service_catalog_reply(*, business: Business, language: str) -> str:
    services = list(
        business.services.filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )
    if not services:
        if language == "kz":
            return "Қазір нақты қызмет тізімі бос тұр."
        return "Сейчас не вижу активный список услуг."

    lines = []
    for service in islice(services, 0, 6):
        service_name = localize_service_name(service.name, language)
        price_value = int(service.price)
        duration_minutes = int((service.duration or timedelta()).total_seconds() // 60)
        if duration_minutes > 0:
            lines.append(f"- {service_name} — {price_value} тг, {duration_minutes} мин")
        else:
            lines.append(f"- {service_name} — {price_value} тг")

    if language == "kz":
        return "Мына қызметтер бар:\n" + "\n".join(lines)
    return "Вот что есть:\n" + "\n".join(lines)


def build_master_list_reply(*, business: Business, language: str) -> str:
    masters = list(
        business.masters.filter(is_active=True).order_by("full_name").values_list(
            "full_name",
            "specialization",
        )
    )
    if not masters:
        if language == "kz":
            return "Қазір белсенді шеберлер тізімі бос."
        return "Сейчас активных мастеров в списке нет."

    master_lines = "; ".join(f"{name} — {specialization}" for name, specialization in masters[:4])
    if language == "kz":
        return f"Қазір жұмыс істейтін шеберлер: {master_lines}."
    return f"Сейчас работают: {master_lines}."


def build_service_price_reply(*, service, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    duration_minutes = int((service.duration or timedelta()).total_seconds() // 60)
    price_value = int(service.price)
    if language == "kz":
        if duration_minutes > 0:
            return f"«{service_name}» бағасы {price_value} тг. Ұзақтығы шамамен {duration_minutes} минут."
        return f"«{service_name}» бағасы {price_value} тг."
    if duration_minutes > 0:
        return f"«{service_name}» стоит {price_value} тг. По времени — около {duration_minutes} минут."
    return f"«{service_name}» стоит {price_value} тг."


def build_working_hours_reply(*, business: Business, language: str) -> str:
    hours_text = (business.working_hours or "").strip()
    if not hours_text:
        if language == "kz":
            return "Жұмыс уақытын қазір нақты айта алмаймын."
        return "Сейчас не вижу точный график работы."

    if "-" in hours_text:
        start_time, end_time = [part.strip() for part in hours_text.split("-", 1)]
        if language == "kz":
            return f"Бүгін {start_time}-ден {end_time}-ге дейін жұмыс істейміз."
        return f"Сегодня работаем с {start_time} до {end_time}."

    if language == "kz":
        return f"Жұмыс уақыты: {hours_text}."
    return f"График работы: {hours_text}."


# --- Tier 3: sibling calls + service_matcher ---------------------------

def build_current_session_master_reply(*, session: BookingSession, language: str) -> str:
    if session.master_id and session.master is not None and session.service_id and session.service is not None:
        service_name = localize_service_name(session.service.name, language)
        if language == "kz":
            return (
                f"Қазір {service_name} бойынша сізге {session.master.full_name} шебері таңдалған. "
                "Қаласаңыз, жазбаны жалғастырамын."
            )
        return (
            f"Сейчас по услуге «{service_name}» у вас выбран мастер {session.master.full_name}. "
            "Если хотите, продолжу запись."
        )

    if session.service_id and session.service is not None:
        return build_service_master_options_reply(
            business=session.business,
            language=language,
            texts=[],
            service=session.service,
        )

    return build_master_list_reply(business=session.business, language=language)


def build_master_recommendation_reply(*, business: Business, language: str, texts: list[str], service=None) -> str:
    service = service or infer_service_from_messages(business=business, texts=texts)
    if service is None:
        return build_master_list_reply(business=business, language=language)

    masters = get_service_recommended_masters(business=business, service=service)
    if not masters:
        return build_master_list_reply(business=business, language=language)

    recommended_master = masters[0]
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        return f"{service_name} үшін шебер: {recommended_master.full_name}. Бағыты — {recommended_master.specialization.lower()}."
    return f"Для услуги «{service_name}» подойдет мастер {recommended_master.full_name}. Направление — {recommended_master.specialization.lower()}."


def build_service_master_options_reply(*, business: Business, language: str, texts: list[str], service=None) -> str:
    service = service or infer_service_from_messages(business=business, texts=texts)
    if service is None:
        return build_master_list_reply(business=business, language=language)

    masters = get_service_recommended_masters(business=business, service=service)
    if not masters:
        return build_master_list_reply(business=business, language=language)

    service_name = localize_service_name(service.name, language)
    if language == "kz":
        if len(masters) == 1:
            master = masters[0]
            return f"{service_name} үшін шебер: {master.full_name}. Бағыты — {master.specialization.lower()}."
        master_lines = "; ".join(f"{master.full_name} — {master.specialization}" for master in masters[:4])
        return f"{service_name} үшін мына шеберлер бар: {master_lines}."

    if len(masters) == 1:
        master = masters[0]
        return f"На услугу «{service_name}» работает мастер {master.full_name}. Направление — {master.specialization.lower()}."

    master_lines = "; ".join(f"{master.full_name} — {master.specialization}" for master in masters[:4])
    return f"На услугу «{service_name}» работают: {master_lines}."
