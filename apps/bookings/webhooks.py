from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .ai_manager import AIManager, HUMAN_HANDOFF_MESSAGE, VOICE_FALLBACK_MESSAGE
from .models import Booking, Business, Client, ConversationMessage
from .tasks import notify_human_operator

OPT_OUT_KEYWORDS = {"stop", "стоп", "отписаться", "не пиши", "не писать"}
RATE_LIMIT_MESSAGE = (
    "Слишком много сообщений за короткое время. Давайте продолжим через минуту."
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


def get_or_create_client(
    *,
    business_id: int,
    channel: str,
    external_id: str = "",
    phone: str = "",
    name: str = "",
):
    business = Business.objects.get(pk=business_id, is_active=True)
    channel_field = {
        ConversationMessage.Channel.TELEGRAM: "telegram_id",
        ConversationMessage.Channel.WHATSAPP: "whatsapp_id",
    }.get(channel, "")

    client = None
    if channel_field and external_id:
        client = business.clients.filter(**{channel_field: external_id}).first()
    if client is None and phone:
        client = business.clients.filter(phone=phone).first()

    if client is None:
        client = business.clients.create(
            name=name,
            phone=phone,
            external_id=external_id or "",
            **({channel_field: external_id} if channel_field and external_id else {}),
        )
        return client

    updates = []
    if name and client.name != name:
        client.name = name
        updates.append("name")
    if external_id and client.external_id != external_id:
        client.external_id = external_id
        updates.append("external_id")
    if channel_field and external_id and getattr(client, channel_field) != external_id:
        setattr(client, channel_field, external_id)
        updates.append(channel_field)
    if updates:
        updates.append("updated_at")
        client.save(update_fields=updates)
    return client


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
    return ConversationMessage.objects.create(
        business_id=business_id,
        client=client,
        channel=channel,
        role=role,
        content=content,
    )


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
    return [
        {
            "role": item["role"],
            "content": item["content"],
        }
        for item in messages
    ]


def enforce_client_rate_limit(*, business_id: int, client: Client, channel: str):
    window_start = timezone.now() - timedelta(minutes=1)
    recent_messages_count = ConversationMessage.objects.filter(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.USER,
        created_at__gte=window_start,
    ).count()
    if recent_messages_count > settings.MAX_MESSAGES_PER_MINUTE:
        raise ValidationError(RATE_LIMIT_MESSAGE)


def process_opt_out(*, client: Client, text: str):
    if (text or "").strip().lower() in OPT_OUT_KEYWORDS:
        client.allow_follow_up = False
        client.save(update_fields=["allow_follow_up", "updated_at"])
        return True
    return False


def handle_text_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    text: str,
    ai_manager: AIManager | None = None,
):
    ai_manager = ai_manager or AIManager()
    store_message(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.USER,
        content=text,
    )
    enforce_client_rate_limit(
        business_id=business_id,
        client=client,
        channel=channel,
    )
    if process_opt_out(client=client, text=text):
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
    requested_human = ai_manager.detect_human_request(text)
    if ai_manager.should_escalate(
        requested_human=requested_human,
        failed_attempts=client.ai_failure_count,
    ):
        notify_human_operator.delay(
            booking_id=booking.id if booking else None,
            reason="Client requested a human operator",
            attempts=client.ai_failure_count,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=HUMAN_HANDOFF_MESSAGE,
        )
        return {"reply": HUMAN_HANDOFF_MESSAGE, "escalated": True}

    try:
        reply = ai_manager.generate_reply(
            build_conversation_context(
                business_id=business_id,
                client=client,
                channel=channel,
            )
        )
    except Exception:
        client.ai_failure_count += 1
        client.save(update_fields=["ai_failure_count", "updated_at"])
        if ai_manager.should_escalate(
            requested_human=False,
            failed_attempts=client.ai_failure_count,
        ):
            notify_human_operator.delay(
                booking_id=booking.id if booking else None,
                reason="AI failed to answer three times in a row",
                attempts=client.ai_failure_count,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=HUMAN_HANDOFF_MESSAGE,
            )
            return {"reply": HUMAN_HANDOFF_MESSAGE, "escalated": True}
        return {"reply": HUMAN_HANDOFF_MESSAGE, "escalated": False}

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

    ai_manager = ai_manager or AIManager()
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

    store_message(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.USER,
        content=transcript,
    )
    response = handle_text_message(
        business_id=business_id,
        channel=channel,
        client=client,
        text=transcript,
        ai_manager=ai_manager,
    )
    response["transcript"] = transcript
    return response
