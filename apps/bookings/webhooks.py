import logging
import re
from itertools import islice
from types import SimpleNamespace
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .ai_manager import AIManager, AI_RETRY_MESSAGE, HUMAN_HANDOFF_MESSAGE, VOICE_FALLBACK_MESSAGE
from .client_identity import ClientIdentityResolver
from .conversation_context import build_conversation_context, detect_client_language
from .conversation_threads import get_or_create_conversation_thread, is_bot_active
from .models import Booking, BookingSession, Business, Client, ConversationMessage, InboundEvent, Master
from .language import (
    LOCALIZED_RUNTIME_MESSAGES,
    MONTH_NAMES,
    SERVICE_NAME_LOCALIZATIONS,
    format_local_date,
    format_local_datetime,
    get_localized_runtime_message,
    localize_service_name,
)
from .security import (
    RATE_LIMIT_MESSAGE,
    enforce_client_rate_limit,
    verify_green_api_request,
    verify_telegram_request,
    verify_telegram_secret,
    verify_webhook_token,
)
from .replies import (
    build_booking_confirmation_reply,
    build_booking_created_reply,
    build_booking_intent_clarification_reply,
    build_cancellation_aborted_reply,
    build_cancellation_confirmation_prompt,
    build_cancellation_handoff_reply,
    build_cancellation_multiple_bookings_reply,
    build_cancellation_no_active_bookings_reply,
    build_cancellation_success_reply,
    build_current_session_master_reply,
    build_date_selection_reply,
    build_existing_booking_reply,
    build_gratitude_reply,
    build_greeting_reply,
    build_haircut_clarification_reply,
    build_master_list_reply,
    build_master_opinion_reply,
    build_master_recommendation_reply,
    build_out_of_scope_reply,
    build_price_clarification_reply,
    build_reschedule_initiated_reply,
    build_reschedule_late_escalation_reply,
    build_reschedule_multiple_bookings_reply,
    build_reschedule_no_active_bookings_reply,
    build_reschedule_success_reply,
    build_service_catalog_reply,
    build_service_master_options_reply,
    build_service_price_reply,
    build_session_master_match_reply,
    build_session_master_mismatch_reply,
    build_slot_options_reply,
    build_time_preference_unavailable_reply,
    build_unknown_master_reply,
    build_working_hours_reply,
)
from .service_matcher import (
    detect_generic_haircut_request,
    get_gendered_haircut_services,
    get_service_recommended_masters,
    infer_service_from_messages,
    is_haircut_service,
)
from .intent import (
    detect_cancellation_request,
    detect_explicit_booking_intent,
    detect_gratitude_message,
    detect_greeting_message,
    detect_hours_request,
    detect_master_list_request,
    detect_master_opinion_request,
    detect_master_recommendation_request,
    detect_non_booking_service_question,
    detect_out_of_scope_followup_pressure,
    detect_out_of_scope_request,
    detect_price_request,
    detect_reschedule_request,
    detect_service_catalog_request,
    has_affirmative_signal,
    is_affirmative_message,
    is_price_clarification_prompt,
)
from .master_matcher import (
    LATIN_TO_CYRILLIC_MAP,
    MASTER_REFERENCE_STOPWORDS,
    build_master_name_variants,
    extract_unmatched_master_candidate,
    find_mentioned_master,
    transliterate_name_variant_to_cyrillic,
)
from .date_parser import (
    MONTH_NAME_ALIASES,
    WEEKDAY_NAME_ALIASES,
    deserialize_session_slot_options,
    detect_explicit_past_calendar_date,
    infer_target_date_from_messages,
    parse_explicit_calendar_date,
    parse_full_calendar_date,
    parse_relative_weekday_date,
    parse_session_slot_choice,
    parse_slot_choice,
    resolve_day_in_current_or_next_month,
)
from .text_utils import _repair_mojibake
from .booking_utils import (
    extract_slot_time_preference,
    filter_slots_by_time_preference,
    find_next_available_slots,
    should_limit_post_booking_context,
)
from .session_state import (
    clear_booking_session,
    get_or_create_booking_session,
    set_session_selected_slot,
    set_session_service,
    set_session_slot_options,
)
from .services import (
    cancel_booking_for_client,
    create_appointment,
    get_available_slots,
    reschedule_appointment,
)
from .tasks import async_prune_history, notify_human_operator

logger = logging.getLogger(__name__)
POST_BOOKING_CONTEXT_MESSAGE_LIMIT = 6

OPT_OUT_KEYWORDS = {"stop", "стоп", "отписаться", "не пиши", "не писать"}
HUMAN_HANDOFF_DELAY_MESSAGE = (
    "Не получилось сразу передать запрос администратору. "
    "Напишите еще раз через пару минут."
)


def get_business(*, business_id: int) -> Business:
    return Business.objects.get(pk=business_id, is_active=True)


def get_or_create_client(
    *,
    business_id: int,
    channel: str,
    external_id: str = "",
    phone: str = "",
    name: str = "",
):
    business = get_business(business_id=business_id)
    return ClientIdentityResolver().resolve_or_create(
        business=business,
        channel=channel,
        external_id=external_id,
        phone=phone,
        name=name,
    )


def register_inbound_event(
    *,
    business: Business,
    channel: str,
    provider_event_id: str,
    payload: dict,
) -> tuple[InboundEvent, bool]:
    try:
        with transaction.atomic():
            event = InboundEvent.objects.create(
                business=business,
                channel=channel,
                provider_event_id=provider_event_id,
                payload=payload,
            )
        return event, True
    except IntegrityError:
        event = InboundEvent.objects.get(
            business=business,
            channel=channel,
            provider_event_id=provider_event_id,
        )
        return event, False


def mark_inbound_event_processed(event: InboundEvent):
    event.status = InboundEvent.Status.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at"])


def mark_inbound_event_failed(event: InboundEvent):
    event.status = InboundEvent.Status.FAILED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at"])


def get_latest_active_booking(*, business_id: int, client: Client):
    return (
        Booking.objects.filter(
            business_id=business_id,
            client=client,
            status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
        )
        .order_by("-created_at")
        .first()
    )


def get_client_active_bookings(*, business_id: int, client: Client) -> list[Booking]:
    """Return all active bookings of a client that are still in the future.

    Used by the cancellation flow — we only let the client cancel bookings
    that haven't started yet. Ordered by start_time ascending so the
    numbered list shown to the client reads chronologically.

    NEEDS_ATTENTION is treated as active here: the booking is still a real
    future appointment, just flagged for the operator. The client must be
    able to self-cancel it through the bot (UX product decision).
    """
    return list(
        Booking.objects.filter(
            business_id=business_id,
            client=client,
            status__in=[
                Booking.Status.PENDING,
                Booking.Status.CONFIRMED,
                Booking.Status.NEEDS_ATTENTION,
            ],
            start_time__gt=timezone.now(),
        )
        .select_related("service", "master")
        .order_by("start_time")
    )


def store_message(*, business_id: int, client: Client, channel: str, role: str, content: str):
    message = ConversationMessage.objects.create(
        business_id=business_id,
        client=client,
        channel=channel,
        role=role,
        content=content,
    )
    message_count = ConversationMessage.objects.filter(
        business_id=business_id,
        client_id=client.id,
        channel=channel,
    ).count()
    if message_count >= 20 and message_count % 10 == 0:
        if settings.CELERY_TASK_ALWAYS_EAGER:
            async_prune_history.apply(
                kwargs={
                    "business_id": business_id,
                    "client_id": client.id,
                    "channel": channel,
                }
            ).get()
        else:
            async_prune_history.delay(
                business_id=business_id,
                client_id=client.id,
                channel=channel,
            )
    return message


def process_opt_out(*, client: Client, text: str):
    if (text or "").strip().lower() in OPT_OUT_KEYWORDS:
        client.allow_follow_up = False
        client.save(update_fields=["allow_follow_up", "updated_at"])
        return True
    return False


def request_human_handoff(*, booking, reason: str, attempts: int, language: str = "ru"):
    if booking is None:
        return {
            "reply": get_localized_runtime_message("human_handoff_delay", language),
            "escalated": False,
        }

    handoff_result = (
        notify_human_operator.apply(
            kwargs={
                "booking_id": booking.id,
                "reason": reason,
                "attempts": attempts,
            }
        ).get()
        if settings.CELERY_TASK_ALWAYS_EAGER
        else {
            "notification_status": "queued",
            "delivery_task_id": notify_human_operator.delay(
                booking_id=booking.id,
                reason=reason,
                attempts=attempts,
            ).id,
        }
    )
    if handoff_result.get("notification_status") in {
        "queued",
        "submitted",
        "delivered",
    }:
        return {
            "reply": get_localized_runtime_message("human_handoff", language),
            "escalated": True,
        }
    return {
        "reply": get_localized_runtime_message("human_handoff_delay", language),
        "escalated": False,
    }


def is_unspecified_other_day_request(text: str) -> bool:
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return False
    return (
        any(phrase in normalized for phrase in ("другой день", "другой", "басқа күн", "баска кун"))
        and parse_explicit_calendar_date(normalized) is None
    )


def is_brief_post_booking_follow_up(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    brief_affirmative_variants = {
        "\u0434\u0430",
        "\u0430\u0433\u0430",
        "\u043e\u043a",
        "okay",
        "yes",
        "\u0443\u0433\u0443",
        "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0430\u044e",
        "\u0438\u04d9",
        "\u0438\u044f",
        "\u0445\u0430",
        "\u0436\u0430\u0440\u0430\u0439\u0434\u044b",
    }
    if normalized in brief_affirmative_variants:
        return True
    repaired = _repair_mojibake(normalized)
    return repaired in brief_affirmative_variants


def last_assistant_message_targets_existing_booking(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    booking_markers = (
        "\u0437\u0430\u043f\u0438\u0441\u044c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430",
        "\u0437\u0430\u043f\u0438\u0441\u044c \u0435\u0449\u0435 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430",
        "\u0432\u0430\u0448 \u043c\u0430\u0441\u0442\u0435\u0440",
        "\u0432\u0430\u0448\u0430 \u0437\u0430\u043f\u0438\u0441\u044c",
        "\u0434\u0430, \u0437\u0430\u043f\u0438\u0441\u044c",
    )
    return any(marker in normalized for marker in booking_markers)


def _parse_cancel_choice(text: str) -> int | None:
    """Extract a 1-based booking number from a free-form CHOOSING reply."""
    match = re.match(r"^\s*(\d+)\b", text or "")
    return int(match.group(1)) if match else None


def _route_single_booking_cancel(
    *,
    booking: Booking,
    session: BookingSession,
    business: Business,
    client: Client,
    channel: str,
    language: str,
) -> dict:
    """Decide between auto-cancel-with-confirmation and late escalation.

    Compares the time-until-start against ``business.cancellation_policy_hours``.
    Above the threshold the bot asks for yes/no confirmation; below it the
    request is handed off to a human operator unchanged.
    """
    hours_until = (booking.start_time - timezone.now()).total_seconds() / 3600
    if hours_until < business.cancellation_policy_hours:
        handoff_response = request_human_handoff(
            booking=booking,
            reason="Late cancellation request",
            attempts=client.ai_failure_count,
            language=language,
        )
        reply = build_cancellation_handoff_reply(language=language)
        store_message(
            business_id=business.id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        session.reset()
        session.save()
        return {"reply": reply, "escalated": handoff_response["escalated"]}

    session.state = BookingSession.State.CANCEL_CONFIRMING
    session.context = {"cancellation_booking_id": booking.id}
    session.touch_expiration()
    session.save()
    reply = build_cancellation_confirmation_prompt(booking=booking, language=language)
    store_message(
        business_id=business.id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.ASSISTANT,
        content=reply,
    )
    return {"reply": reply, "escalated": False}


def _route_single_booking_reschedule(
    *,
    booking: Booking,
    session: BookingSession,
    business: Business,
    client: Client,
    channel: str,
    language: str,
) -> dict:
    """Decide between auto-reschedule-with-date-prompt and late escalation.

    Mirrors ``_route_single_booking_cancel`` — same policy threshold
    ``business.cancellation_policy_hours``. Above the threshold the bot
    asks the client to pick a new date (handing off into the existing
    AWAITING_DATE flow with a context marker so AWAITING_CONFIRMATION
    later calls reschedule_appointment instead of create_appointment).
    Below the threshold the request is escalated to a human operator.
    """
    hours_until = (booking.start_time - timezone.now()).total_seconds() / 3600
    if hours_until < business.cancellation_policy_hours:
        handoff_response = request_human_handoff(
            booking=booking,
            reason="Late reschedule request",
            attempts=client.ai_failure_count,
            language=language,
        )
        reply = build_reschedule_late_escalation_reply(booking=booking, language=language)
        store_message(
            business_id=business.id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        session.reset()
        session.save()
        return {"reply": reply, "escalated": handoff_response["escalated"]}

    # Hand off into the existing AWAITING_DATE flow with a context marker.
    # set_session_service resets the session and stamps service + state.
    set_session_service(
        session,
        service=booking.service,
        language=language,
        source="reschedule_flow",
    )
    # Preserve language and source, plus add the reschedule marker.
    session.context = {
        **session.context,
        "reschedule_booking_id": booking.id,
    }
    session.save()

    reply = build_reschedule_initiated_reply(booking=booking, language=language)
    store_message(
        business_id=business.id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.ASSISTANT,
        content=reply,
    )
    return {"reply": reply, "escalated": False}


def process_incoming_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    text: str,
    ai_manager: AIManager | None = None,
    persist_user_message: bool = True,
):
    normalized_text = (text or "").strip()
    if not normalized_text:
        raise ValidationError("Message text is required.")

    business = get_business(business_id=business_id)
    ai_manager = ai_manager or AIManager(business=business, client=client)
    enforce_client_rate_limit(
        business_id=business_id,
        client=client,
        channel=channel,
    )

    if persist_user_message:
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.USER,
            content=normalized_text,
        )

    thread = get_or_create_conversation_thread(
        business=business,
        client=client,
        channel=channel,
    )
    if not is_bot_active(thread):
        return {"reply": "", "escalated": False, "bot_paused": True}


    preferred_language = detect_client_language(
        ai_manager=ai_manager,
        business_id=business_id,
        client=client,
        channel=channel,
        current_text="" if persist_user_message else normalized_text,
    )

    if process_opt_out(client=client, text=normalized_text):
        reply = get_localized_runtime_message("opt_out", preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    last_assistant_scope_text = (
        ConversationMessage.objects.filter(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
        )
        .order_by("-created_at", "-id")
        .values_list("content", flat=True)
        .first()
        or ""
    )
    if detect_out_of_scope_request(normalized_text) or detect_out_of_scope_followup_pressure(
        text=normalized_text,
        last_assistant_text=last_assistant_scope_text,
    ):
        reply = build_out_of_scope_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_service_catalog_request(normalized_text):
        reply = build_service_catalog_reply(
            business=business,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    booking = get_latest_active_booking(business_id=business_id, client=client)
    session = get_or_create_booking_session(
        business=business,
        client=client,
        channel=channel,
    )

    # === Cancellation continuation: pre-empt every other handler while
    # the client is mid-flow choosing/confirming a booking to cancel. ===
    if session.state == BookingSession.State.CANCEL_CHOOSING:
        booking_ids = session.context.get("cancellation_booking_ids", []) if isinstance(session.context, dict) else []
        choice = _parse_cancel_choice(normalized_text)
        if choice is None or not (1 <= choice <= len(booking_ids)):
            active_bookings = list(
                Booking.objects.filter(
                    pk__in=booking_ids,
                    client=client,
                    status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
                )
                .select_related("service", "master")
                .order_by("start_time")
            )
            if not active_bookings:
                session.reset()
                session.save()
                reply = build_cancellation_no_active_bookings_reply(language=preferred_language)
            else:
                reply = build_cancellation_multiple_bookings_reply(
                    bookings=active_bookings,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        chosen_id = booking_ids[choice - 1]
        chosen_booking = (
            Booking.objects.filter(
                pk=chosen_id,
                client=client,
                business=business,
                status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
            )
            .select_related("service", "master")
            .first()
        )
        if chosen_booking is None:
            session.reset()
            session.save()
            reply = build_cancellation_no_active_bookings_reply(language=preferred_language)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        return _route_single_booking_cancel(
            booking=chosen_booking,
            session=session,
            business=business,
            client=client,
            channel=channel,
            language=preferred_language,
        )

    if session.state == BookingSession.State.CANCEL_CONFIRMING:
        booking_id = (
            session.context.get("cancellation_booking_id")
            if isinstance(session.context, dict)
            else None
        )
        target = (
            Booking.objects.filter(
                pk=booking_id,
                client=client,
                business=business,
                status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
            )
            .select_related("service", "master")
            .first()
            if booking_id
            else None
        )
        if target is None:
            session.reset()
            session.save()
            reply = build_cancellation_no_active_bookings_reply(language=preferred_language)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if has_affirmative_signal(normalized_text):
            cancelled = cancel_booking_for_client(
                booking=target,
                client=client,
                business=business,
            )
            session.reset()
            session.save()
            reply = build_cancellation_success_reply(
                booking=cancelled,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        session.reset()
        session.save()
        reply = build_cancellation_aborted_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    # === Reschedule choosing: client picked from a numbered list. Hand the
    # chosen booking to the date-prompt flow. ===
    if session.state == BookingSession.State.RESCHEDULE_CHOOSING:
        booking_ids = (
            session.context.get("reschedule_booking_ids", [])
            if isinstance(session.context, dict)
            else []
        )
        choice = _parse_cancel_choice(normalized_text)
        if choice is None or not (1 <= choice <= len(booking_ids)):
            active_bookings = list(
                Booking.objects.filter(
                    pk__in=booking_ids,
                    client=client,
                    status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
                )
                .select_related("service", "master")
                .order_by("start_time")
            )
            if not active_bookings:
                session.reset()
                session.save()
                reply = build_reschedule_no_active_bookings_reply(language=preferred_language)
            else:
                reply = build_reschedule_multiple_bookings_reply(
                    bookings=active_bookings,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        chosen_id = booking_ids[choice - 1]
        chosen_booking = (
            Booking.objects.filter(
                pk=chosen_id,
                client=client,
                business=business,
                status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
            )
            .select_related("service", "master")
            .first()
        )
        if chosen_booking is None:
            session.reset()
            session.save()
            reply = build_reschedule_no_active_bookings_reply(language=preferred_language)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        return _route_single_booking_reschedule(
            booking=chosen_booking,
            session=session,
            business=business,
            client=client,
            channel=channel,
            language=preferred_language,
        )

    if detect_greeting_message(normalized_text):
        clear_booking_session(session)
        reply = build_greeting_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    post_booking_context_limited = should_limit_post_booking_context(
        session=session,
        booking=booking,
        text=normalized_text,
    )
    conversation_context = build_conversation_context(
        business_id=business_id,
        client=client,
        channel=channel,
        max_messages=POST_BOOKING_CONTEXT_MESSAGE_LIMIT if post_booking_context_limited else None,
    )
    recent_texts = [
        item["content"]
        for item in conversation_context
        if item.get("role") == ConversationMessage.Role.USER
    ]
    recent_assistant_texts = [
        item["content"]
        for item in conversation_context
        if item.get("role") == ConversationMessage.Role.ASSISTANT
    ]
    last_assistant_text = recent_assistant_texts[-1] if recent_assistant_texts else ""

    if detect_generic_haircut_request(business=business, text=normalized_text) and (
        session.service_id is None or is_haircut_service(session.service)
    ) and not detect_non_booking_service_question(normalized_text):
        clear_booking_session(session)
        reply = build_haircut_clarification_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    explicit_service = infer_service_from_messages(
        business=business,
        texts=[normalized_text],
    )
    explicit_booking_intent = detect_explicit_booking_intent(normalized_text)
    non_booking_service_question = (
        explicit_service is not None
        and detect_non_booking_service_question(normalized_text)
    )
    faq_context_service = (
        explicit_service
        or session.service
        or (booking.service if booking is not None else None)
        or infer_service_from_messages(
            business=business,
            texts=recent_texts,
        )
    )

    if detect_hours_request(normalized_text):
        reply = build_working_hours_reply(
            business=business,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_price_request(normalized_text) or (
        explicit_service is not None and is_price_clarification_prompt(last_assistant_text)
    ):
        if faq_context_service is not None:
            reply = build_service_price_reply(
                service=faq_context_service,
                language=preferred_language,
            )
        else:
            reply = build_price_clarification_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_gratitude_message(normalized_text):
        reply = build_gratitude_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if (
        booking is not None
        and session.state == BookingSession.State.IDLE
        and is_brief_post_booking_follow_up(normalized_text)
        and (
            not last_assistant_text
            or last_assistant_message_targets_existing_booking(last_assistant_text)
        )
    ):
        reply = build_existing_booking_reply(
            booking=booking,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    service_switched = (
        explicit_service is not None
        and session.service_id != explicit_service.id
        and not non_booking_service_question
        and (explicit_booking_intent or session.state != BookingSession.State.IDLE)
    )
    if service_switched:
        set_session_service(
            session,
            service=explicit_service,
            language=preferred_language,
        )

    history_texts_for_current_service = [] if service_switched else recent_texts
    history_last_assistant_text = "" if service_switched else last_assistant_text

    inferred_service = explicit_service or session.service or infer_service_from_messages(
        business=business,
        texts=history_texts_for_current_service,
    )
    current_message_target_date = parse_explicit_calendar_date(normalized_text) or infer_target_date_from_messages(
        texts=[normalized_text],
        last_assistant_text="",
    )
    if detect_explicit_past_calendar_date(normalized_text):
        reply = "Эта дата уже прошла. Напишите актуальную дату и время, например: 12 мая в 18:00."
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    inferred_target_date = session.target_date or current_message_target_date or infer_target_date_from_messages(
        texts=history_texts_for_current_service,
        last_assistant_text=history_last_assistant_text,
    )
    assistant_requests_confirmation = (not service_switched) and any(
        keyword in last_assistant_text.lower()
        for keyword in ("подтверж", "подтверд", "раста")
    )
    assistant_offered_tomorrow = (not service_switched) and any(
        keyword in last_assistant_text.lower()
        for keyword in ("завтра", "ертең", "ертен")
    )

    if (
        session.state == BookingSession.State.IDLE
        and booking is None
        and inferred_service is None
        and explicit_booking_intent
        and not detect_cancellation_request(normalized_text)
        and not detect_reschedule_request(normalized_text)
    ):
        reply = build_booking_intent_clarification_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    mentioned_master = find_mentioned_master(business=business, text=normalized_text)

    if mentioned_master is not None and detect_master_opinion_request(normalized_text):
        opinion_service = session.service or (booking.service if booking is not None else None)
        if opinion_service is not None:
            service_masters = get_service_recommended_masters(
                business=business,
                service=opinion_service,
            )
            if mentioned_master.id in {item.id for item in service_masters}:
                reply = build_master_opinion_reply(
                    master=mentioned_master,
                    service=opinion_service,
                    language=preferred_language,
                )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}
        reply = build_master_opinion_reply(
            master=mentioned_master,
            service=None,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if session.service_id and mentioned_master is not None:
        service_masters = get_service_recommended_masters(
            business=business,
            service=session.service,
        )
        service_master_ids = {item.id for item in service_masters}
        if session.master_id and mentioned_master.id == session.master_id:
            reply = build_session_master_match_reply(
                session=session,
                mentioned_master=mentioned_master,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if mentioned_master.id not in service_master_ids:
            reply = build_session_master_mismatch_reply(
                session=session,
                mentioned_master=mentioned_master,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if detect_master_list_request(normalized_text):
        if session.service_id:
            reply = build_current_session_master_reply(
                session=session,
                language=preferred_language,
            )
        elif inferred_service is not None:
            reply = build_service_master_options_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=inferred_service,
            )
        else:
            reply = build_master_list_reply(
                business=business,
                language=preferred_language,
            )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_cancellation_request(normalized_text):
        active_bookings = get_client_active_bookings(
            business_id=business_id, client=client,
        )
        if not active_bookings:
            reply = build_cancellation_no_active_bookings_reply(language=preferred_language)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if len(active_bookings) > 1:
            session.state = BookingSession.State.CANCEL_CHOOSING
            session.context = {
                "cancellation_booking_ids": [b.id for b in active_bookings],
            }
            session.touch_expiration()
            session.save()
            reply = build_cancellation_multiple_bookings_reply(
                bookings=active_bookings,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        return _route_single_booking_cancel(
            booking=active_bookings[0],
            session=session,
            business=business,
            client=client,
            channel=channel,
            language=preferred_language,
        )

    if (
        detect_reschedule_request(normalized_text)
        and session.state == BookingSession.State.IDLE
    ):
        # Guard: only fire from IDLE. Otherwise "другой день" / "басқа күн"
        # said while picking a date for a fresh booking would be mis-routed
        # into reschedule. Mid-flow date phrases stay with the date handler.
        active_bookings = get_client_active_bookings(
            business_id=business_id, client=client,
        )
        if not active_bookings:
            reply = build_reschedule_no_active_bookings_reply(language=preferred_language)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if len(active_bookings) > 1:
            session.state = BookingSession.State.RESCHEDULE_CHOOSING
            session.context = {
                "reschedule_booking_ids": [b.id for b in active_bookings],
            }
            session.touch_expiration()
            session.save()
            reply = build_reschedule_multiple_bookings_reply(
                bookings=active_bookings,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        return _route_single_booking_reschedule(
            booking=active_bookings[0],
            session=session,
            business=business,
            client=client,
            channel=channel,
            language=preferred_language,
        )

    if detect_master_recommendation_request(normalized_text):
        if session.service_id:
            reply = build_service_master_options_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=session.service,
            )
        elif inferred_service is not None:
            reply = build_service_master_options_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=inferred_service,
            )
        else:
            reply = build_master_recommendation_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=inferred_service,
            )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if (
        session.state == BookingSession.State.IDLE
        and inferred_service is not None
        and not non_booking_service_question
    ):
        if current_message_target_date is not None:
            set_session_service(
                session,
                service=inferred_service,
                language=preferred_language,
            )
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=inferred_service,
                target_date=current_message_target_date,
            )
            time_preference = extract_slot_time_preference(normalized_text)
            if time_preference is not None and time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            preferred_slots = filter_slots_by_time_preference(
                slots=slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or slots
            set_session_slot_options(
                session,
                service=inferred_service,
                target_date=resolved_target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if time_preference is not None and not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=inferred_service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=inferred_service, slots=slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=inferred_service,
                    slots=slots_for_reply,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if explicit_service is not None:
            set_session_service(
                session,
                service=inferred_service,
                language=preferred_language,
            )
            reply = build_date_selection_reply(
                service=inferred_service,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if (
            is_affirmative_message(normalized_text)
            and assistant_requests_confirmation
            and inferred_target_date is not None
        ):
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=inferred_service,
                target_date=inferred_target_date,
            )
            prior_slot = None
            for candidate_text in reversed(recent_texts):
                prior_slot = parse_slot_choice(candidate_text, slots=slots[:3])
                if prior_slot is not None:
                    break
            if prior_slot is not None:
                selected_master = business.masters.get(
                    pk=prior_slot.master_id,
                    is_active=True,
                )
                booking_record = create_appointment(
                    business=business,
                    master=selected_master,
                    service=inferred_service,
                    client=client,
                    start_time=prior_slot.start,
                    status=Booking.Status.CONFIRMED,
                    client_data={
                        "name": client.name or "",
                        "phone": str(client.phone or ""),
                        "whatsapp_id": client.whatsapp_id or "",
                        "telegram_id": client.telegram_id or "",
                    },
                )
                local_start = timezone.localtime(booking_record.start_time)
                service_name = localize_service_name(inferred_service.name, preferred_language)
                if preferred_language == "kz":
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                else:
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}

        if is_affirmative_message(normalized_text) and (assistant_offered_tomorrow or inferred_target_date is not None):
            target_date = inferred_target_date or (timezone.localdate() + timedelta(days=1))
            set_session_service(
                session,
                service=inferred_service,
                language=preferred_language,
            )
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=inferred_service,
                target_date=target_date,
            )
            set_session_slot_options(
                session,
                service=inferred_service,
                target_date=resolved_target_date,
                slots=slots[:3],
                language=preferred_language,
            )
            reply = build_slot_options_reply(
                service=inferred_service,
                slots=slots,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if (
        session.state == BookingSession.State.AWAITING_CONFIRMATION
        and session.service_id
        and session.master_id
        and session.selected_start_time is not None
        and session.selected_end_time is not None
        and is_affirmative_message(normalized_text)
    ):
        # Reschedule path — the session was entered via _route_single_booking_reschedule,
        # which stamped reschedule_booking_id into context. Apply the new
        # slot to the existing booking instead of creating a new one.
        reschedule_booking_id = (
            session.context.get("reschedule_booking_id")
            if isinstance(session.context, dict)
            else None
        )
        if reschedule_booking_id:
            target_booking = (
                Booking.objects.filter(
                    pk=reschedule_booking_id,
                    client=client,
                    business=business,
                    status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
                )
                .select_related("service", "master")
                .first()
            )
            if target_booking is None:
                clear_booking_session(session)
                reply = build_reschedule_no_active_bookings_reply(language=preferred_language)
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}
            try:
                updated_booking = reschedule_appointment(
                    booking=target_booking,
                    business=business,
                    start_time=session.selected_start_time,
                    master=session.master,
                )
            except ValidationError as error:
                # Slot taken / past / inactive master — let the client try
                # again. Keep state so they can pick another time.
                logger.info(
                    "reschedule_validation_failed",
                    extra={
                        "business_id": business_id,
                        "client_id": client.id,
                        "booking_id": target_booking.id,
                        "error": "; ".join(error.messages) if hasattr(error, "messages") else str(error),
                    },
                )
                reply = build_reschedule_late_escalation_reply(
                    booking=target_booking, language=preferred_language,
                )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                clear_booking_session(session)
                return {"reply": reply, "escalated": False}

            clear_booking_session(session)
            reply = build_reschedule_success_reply(
                booking=updated_booking, language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        booking_record = create_appointment(
            business=business,
            master=session.master,
            service=session.service,
            client=client,
            start_time=session.selected_start_time,
            status=Booking.Status.CONFIRMED,
            client_data={
                "name": client.name or "",
                "phone": str(client.phone or ""),
                "whatsapp_id": client.whatsapp_id or "",
                "telegram_id": client.telegram_id or "",
            },
        )
        local_start = timezone.localtime(booking_record.start_time)
        service_name = localize_service_name(session.service.name, preferred_language)
        if preferred_language == "kz":
            reply = build_booking_created_reply(
                service_name=service_name,
                local_start=local_start,
                master_name=booking_record.master.full_name,
                language=preferred_language,
            )
        else:
            reply = build_booking_created_reply(
                service_name=service_name,
                local_start=local_start,
                master_name=booking_record.master.full_name,
                language=preferred_language,
            )
        clear_booking_session(session)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if session.state == BookingSession.State.AWAITING_SLOT_CHOICE and session.service_id:
        if current_message_target_date is not None:
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=session.service,
                target_date=current_message_target_date,
            )
            time_preference = extract_slot_time_preference(normalized_text)
            if time_preference is not None and time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            preferred_slots = filter_slots_by_time_preference(
                slots=slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or slots
            set_session_slot_options(
                session,
                service=session.service,
                target_date=resolved_target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if time_preference is not None and not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=session.service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=session.service, slots=slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=session.service,
                    slots=slots_for_reply,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        selected_slot = parse_session_slot_choice(
            text=normalized_text,
            slot_options=session.slot_options,
        )
        unknown_master_candidate = extract_unmatched_master_candidate(
            business=business,
            text=normalized_text,
        )
        if selected_slot is not None and unknown_master_candidate is not None:
            reply = build_unknown_master_reply(
                service=session.service,
                candidate=unknown_master_candidate,
                actual_master_name=selected_slot.master_name,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if selected_slot is not None:
            selected_master = business.masters.get(
                pk=selected_slot.master_id,
                is_active=True,
            )
            booking_record = create_appointment(
                business=business,
                master=selected_master,
                service=session.service,
                client=client,
                start_time=selected_slot.start,
                status=Booking.Status.CONFIRMED,
                client_data={
                    "name": client.name or "",
                    "phone": str(client.phone or ""),
                    "whatsapp_id": client.whatsapp_id or "",
                    "telegram_id": client.telegram_id or "",
                },
            )
            local_start = timezone.localtime(booking_record.start_time)
            service_name = localize_service_name(session.service.name, preferred_language)
            reply = build_booking_created_reply(
                service_name=service_name,
                local_start=local_start,
                master_name=booking_record.master.full_name,
                language=preferred_language,
            )
            clear_booking_session(session)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        time_preference = extract_slot_time_preference(normalized_text)
        if time_preference is not None and session.target_date is not None:
            if time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            all_slots = get_available_slots(
                business,
                target_date=session.target_date,
                service_id=session.service.id,
            )
            preferred_slots = filter_slots_by_time_preference(
                slots=all_slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or all_slots
            set_session_slot_options(
                session,
                service=session.service,
                target_date=session.target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=session.service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if all_slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=session.service, slots=all_slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=session.service,
                    slots=preferred_slots,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if session.state == BookingSession.State.AWAITING_DATE and session.service_id:
        target_date = current_message_target_date
        if target_date is None and extract_slot_time_preference(normalized_text) is not None:
            reply = "Время понял. На какой день записать?"
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if target_date is None and is_unspecified_other_day_request(normalized_text):
            reply = "Какой день удобен? Например: среда, пятница или 12 мая."
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if target_date is None and has_affirmative_signal(normalized_text):
            target_date = timezone.localdate() + timedelta(days=1)

        if target_date is not None:
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=session.service,
                target_date=target_date,
            )
            time_preference = extract_slot_time_preference(normalized_text)
            if time_preference is not None and time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            preferred_slots = filter_slots_by_time_preference(
                slots=slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or slots
            set_session_slot_options(
                session,
                service=session.service,
                target_date=resolved_target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if time_preference is not None and not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=session.service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=session.service, slots=slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=session.service,
                    slots=slots_for_reply,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if (
            explicit_service is not None
            and explicit_service.id == session.service_id
            and not non_booking_service_question
        ):
            reply = build_date_selection_reply(
                service=session.service,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if False and inferred_service is not None and inferred_target_date is not None:
        slots = get_available_slots(
            business,
            target_date=inferred_target_date,
            service_id=inferred_service.id,
        )
        assistant_requests_confirmation = any(
            keyword in last_assistant_text.lower()
            for keyword in ("подтверж", "подтверд", "раста")
        )
        selected_slot = parse_slot_choice(normalized_text, slots=slots[:3])
        if selected_slot is not None:
            reply = build_booking_confirmation_reply(
                service=inferred_service,
                slot=selected_slot,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if (
            is_affirmative_message(normalized_text)
        ):
            prior_user_texts = recent_texts[:-1] if recent_texts else []
            prior_slot = None
            for candidate_text in reversed(prior_user_texts):
                prior_slot = parse_slot_choice(candidate_text, slots=slots[:3])
                if prior_slot is not None:
                    break
            if assistant_requests_confirmation and prior_slot is not None:
                booking_record = create_appointment(
                    business=business,
                    master=business.masters.get(pk=prior_slot.master_id, is_active=True),
                    service=inferred_service,
                    client=client,
                    start_time=prior_slot.start,
                    status=Booking.Status.CONFIRMED,
                    client_data={
                        "name": client.name or "",
                        "phone": str(client.phone or ""),
                        "whatsapp_id": client.whatsapp_id or "",
                        "telegram_id": client.telegram_id or "",
                    },
                )
                local_start = timezone.localtime(booking_record.start_time)
                service_name = localize_service_name(inferred_service.name, preferred_language)
                if preferred_language == "kz":
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                else:
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}

        if (
            is_affirmative_message(normalized_text)
            and any(keyword in last_assistant_text.lower() for keyword in ("завтра", "ертең", "ертен"))
            and not assistant_requests_confirmation
        ):
            reply = build_slot_options_reply(
                service=inferred_service,
                slots=slots,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    requested_human = ai_manager.detect_human_request(normalized_text)
    should_auto_escalate = requested_human or (
        booking is not None
        and ai_manager.should_escalate(
            requested_human=False,
            failed_attempts=client.ai_failure_count,
        )
    )
    if should_auto_escalate:
        handoff_response = request_human_handoff(
            booking=booking,
            reason="Client requested a human operator",
            attempts=client.ai_failure_count,
            language=preferred_language,
        )
        assistant_reply = handoff_response["reply"]
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=assistant_reply,
        )
        return handoff_response

    if session.state == BookingSession.State.AWAITING_DATE and session.service_id:
        reply = build_date_selection_reply(
            service=session.service,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if session.state == BookingSession.State.AWAITING_SLOT_CHOICE and session.service_id:
        current_slots = deserialize_session_slot_options(session.slot_options)
        if current_slots:
            reply = build_slot_options_reply(
                service=session.service,
                slots=current_slots,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if (
        session.state == BookingSession.State.AWAITING_CONFIRMATION
        and session.service_id
        and session.master_id
        and session.selected_start_time is not None
    ):
        confirmation_slot = SimpleNamespace(
            start=session.selected_start_time,
            master_name=session.master.full_name,
        )
        reply = build_booking_confirmation_reply(
            service=session.service,
            slot=confirmation_slot,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    logger.info(
        "ai_fallback_entered",
        extra={
            "business_id": business_id,
            "client_id": client.id,
            "channel": channel,
            "session_state": session.state,
            "session_service_id": session.service_id,
            "session_master_id": session.master_id,
            "session_target_date": session.target_date.isoformat() if session.target_date else "",
            "booking_id": booking.id if booking is not None else None,
            "text": (normalized_text or "")[:120],
        },
    )
    try:
        reply = ai_manager.generate_reply(
            conversation_context
        )
    except Exception:
        logger.exception(
            "ai_reply_failed",
            extra={
                "business_id": business_id,
                "client_id": client.id,
                "channel": channel,
                "session_state": session.state,
                "session_service_id": session.service_id,
                "session_master_id": session.master_id,
                "session_target_date": session.target_date.isoformat() if session.target_date else "",
                "booking_id": booking.id if booking is not None else None,
                "text": (normalized_text or "")[:120],
            },
        )
        client.ai_failure_count += 1
        client.save(update_fields=["ai_failure_count", "updated_at"])
        if booking is not None and ai_manager.should_escalate(
            requested_human=False,
            failed_attempts=client.ai_failure_count,
        ):
            handoff_response = request_human_handoff(
                booking=booking,
                reason="AI failed to answer three times in a row",
                attempts=client.ai_failure_count,
                language=preferred_language,
            )
            assistant_reply = handoff_response["reply"]
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=assistant_reply,
            )
            return handoff_response

        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=get_localized_runtime_message("ai_retry", preferred_language),
        )
        return {
            "reply": get_localized_runtime_message("ai_retry", preferred_language),
            "escalated": False,
        }

    if client.ai_failure_count:
        client.ai_failure_count = 0
        client.save(update_fields=["ai_failure_count", "updated_at"])

    store_message(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.ASSISTANT,
        content=reply,
    )
    return {"reply": reply, "escalated": False}


def handle_text_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    text: str,
    ai_manager: AIManager | None = None,
    persist_user_message: bool = True,
):
    return process_incoming_message(
        business_id=business_id,
        channel=channel,
        client=client,
        text=text,
        ai_manager=ai_manager,
        persist_user_message=persist_user_message,
    )


def handle_audio_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    audio_file,
    ai_manager: AIManager | None = None,
):
    max_audio_bytes = settings.MAX_VOICE_FILE_SIZE_BYTES
    file_size = getattr(audio_file, "size", 0)
    if file_size and file_size > max_audio_bytes:
        raise ValidationError("Audio file is too large.")

    business = get_business(business_id=business_id)
    ai_manager = ai_manager or AIManager(business=business, client=client)
    transcript = ai_manager.handle_voice_message(file_obj=audio_file)
    if transcript == VOICE_FALLBACK_MESSAGE:
        reply = get_localized_runtime_message(
            "voice_fallback",
            detect_client_language(
                ai_manager=ai_manager,
                business_id=business_id,
                client=client,
                channel=channel,
            ),
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "transcript": None, "escalated": False}

    response = handle_text_message(
        business_id=business_id,
        channel=channel,
        client=client,
        text=transcript,
        ai_manager=ai_manager,
        persist_user_message=True,
    )
    response["transcript"] = transcript
    return response


# Final human-style reply overrides. These are intentionally placed at the end
# of the module so they override earlier duplicated builder definitions without
# affecting booking/state logic.


