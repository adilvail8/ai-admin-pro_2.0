import logging
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .ai_manager import AIManager, AI_RETRY_MESSAGE, HUMAN_HANDOFF_MESSAGE, VOICE_FALLBACK_MESSAGE
from .client_identity import ClientIdentityResolver
from .models import Booking, Business, Client, ConversationMessage, InboundEvent
from .tasks import async_prune_history, notify_human_operator


logger = logging.getLogger(__name__)

OPT_OUT_KEYWORDS = {"stop", "стоп", "отписаться", "не пиши", "не писать"}
RATE_LIMIT_MESSAGE = (
    "Слишком много сообщений за короткое время. Давайте продолжим через минуту."
)
HUMAN_HANDOFF_DELAY_MESSAGE = (
    "Я зафиксировала ваш запрос, но сейчас не смогла сразу передать его администратору. "
    "Пожалуйста, напишите еще раз через пару минут."
)


def verify_webhook_token(token: str):
    expected_token = settings.WEBHOOK_SHARED_SECRET
    if not expected_token:
        raise ValidationError("Webhook token is not configured.")
    if token != expected_token:
        raise ValidationError("Invalid webhook token.")


def verify_telegram_secret(secret: str):
    expected_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected_secret:
        raise ValidationError("Telegram webhook secret is not configured.")
    if secret != expected_secret:
        raise ValidationError("Invalid Telegram webhook secret.")


def verify_green_api_request(*, token: str, remote_addr: str):
    if token != settings.GREEN_API_SHARED_SECRET:
        raise ValidationError("Invalid Green-API signature.")
    if settings.GREEN_API_ALLOWED_IPS and remote_addr not in settings.GREEN_API_ALLOWED_IPS:
        raise ValidationError("Green-API IP is not allowed.")


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


def build_conversation_context(*, business_id: int, client: Client, channel: str):
    messages = (
        ConversationMessage.objects.filter(
            business_id=business_id,
            client=client,
            channel=channel,
        )
        .order_by("created_at", "id")
        .values("role", "content")
    )
    return [{"role": item["role"], "content": item["content"]} for item in messages]


def enforce_client_rate_limit(*, business_id: int, client: Client, channel: str):
    window_start = timezone.now() - timedelta(minutes=1)
    recent_messages_count = ConversationMessage.objects.filter(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.USER,
        created_at__gte=window_start,
    ).count()
    if recent_messages_count >= settings.MAX_MESSAGES_PER_MINUTE:
        raise ValidationError(RATE_LIMIT_MESSAGE)


def process_opt_out(*, client: Client, text: str):
    if (text or "").strip().lower() in OPT_OUT_KEYWORDS:
        client.allow_follow_up = False
        client.save(update_fields=["allow_follow_up", "updated_at"])
        return True
    return False


def request_human_handoff(*, booking, reason: str, attempts: int):
    if booking is None:
        return {
            "reply": HUMAN_HANDOFF_DELAY_MESSAGE,
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
            "reply": HUMAN_HANDOFF_MESSAGE,
            "escalated": True,
        }
    return {
        "reply": HUMAN_HANDOFF_DELAY_MESSAGE,
        "escalated": False,
    }


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
    ai_manager = ai_manager or AIManager(business=business)
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

    if process_opt_out(client=client, text=normalized_text):
        reply = "Хорошо, больше не буду присылать напоминания по этой записи."
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    booking = get_latest_active_booking(business_id=business_id, client=client)
    requested_human = ai_manager.detect_human_request(normalized_text)
    if ai_manager.should_escalate(
        requested_human=requested_human,
        failed_attempts=client.ai_failure_count,
    ):
        handoff_response = request_human_handoff(
            booking=booking,
            reason="Client requested a human operator",
            attempts=client.ai_failure_count,
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

    try:
        reply = ai_manager.generate_reply(
            build_conversation_context(
                business_id=business_id,
                client=client,
                channel=channel,
            )
        )
    except Exception:
        logger.exception(
            "ai_reply_failed",
            extra={
                "business_id": business_id,
                "client_id": client.id,
                "channel": channel,
            },
        )
        client.ai_failure_count += 1
        client.save(update_fields=["ai_failure_count", "updated_at"])
        if ai_manager.should_escalate(
            requested_human=False,
            failed_attempts=client.ai_failure_count,
        ):
            handoff_response = request_human_handoff(
                booking=booking,
                reason="AI failed to answer three times in a row",
                attempts=client.ai_failure_count,
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
            content=AI_RETRY_MESSAGE,
        )
        return {"reply": AI_RETRY_MESSAGE, "escalated": False}

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
    ai_manager = ai_manager or AIManager(business=business)
    transcript = ai_manager.handle_voice_message(file_obj=audio_file)
    if transcript == VOICE_FALLBACK_MESSAGE:
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=transcript,
        )
        return {"reply": transcript, "transcript": None, "escalated": False}

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
