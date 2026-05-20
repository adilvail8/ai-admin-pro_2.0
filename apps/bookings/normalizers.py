from __future__ import annotations

from typing import Literal, TypedDict

from django.core.exceptions import ValidationError


EventType = Literal["text", "voice", "image", "service", "unsupported"]
SourceType = Literal["telegram", "whatsapp"]


class ClientPayload(TypedDict):
    external_id: str
    chat_id: str
    phone: str
    name: str


class MessagePayload(TypedDict):
    message_id: str
    timestamp: str
    text: str
    caption: str
    file_id: str
    file_url: str
    mime_type: str


class InternalEvent(TypedDict):
    source: SourceType
    event_type: EventType
    business_id: int
    channel: SourceType
    client: ClientPayload
    message: MessagePayload
    raw_event_type: str
    raw: dict


TELEGRAM_SERVICE_UPDATE_KEYS = (
    "edited_message",
    "callback_query",
    "my_chat_member",
    "chat_member",
    "channel_post",
    "edited_channel_post",
)

WHATSAPP_USER_EVENT_TYPE = "incomingMessageReceived"
WHATSAPP_SERVICE_EVENT_TYPES = (
    "stateInstanceChanged",
    "outgoingMessageReceived",
    "outgoingAPIMessageReceived",
    "outgoingMessageStatus",
)


def _empty_message_payload() -> MessagePayload:
    return {
        "message_id": "",
        "timestamp": "",
        "text": "",
        "caption": "",
        "file_id": "",
        "file_url": "",
        "mime_type": "",
    }


def normalize_telegram_event(payload: dict, business_id: int) -> InternalEvent:
    if not isinstance(payload, dict):
        raise ValidationError("Unsupported Telegram payload.")

    raw_event_type = next(
        (key for key in TELEGRAM_SERVICE_UPDATE_KEYS if key in payload),
        "message" if isinstance(payload.get("message"), dict) else "",
    )

    if raw_event_type in TELEGRAM_SERVICE_UPDATE_KEYS:
        return {
            "source": "telegram",
            "event_type": "service",
            "business_id": business_id,
            "channel": "telegram",
            "client": {
                "external_id": "",
                "chat_id": "",
                "phone": "",
                "name": "",
            },
            "message": _empty_message_payload(),
            "raw_event_type": raw_event_type,
            "raw": payload,
        }

    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValidationError("Unsupported Telegram payload.")

    chat_data = message.get("chat", {})
    from_data = message.get("from", {})
    chat_id = str(chat_data.get("id", "")).strip()
    if not chat_id:
        raise ValidationError("Telegram chat id is missing.")

    from_id = str(from_data.get("id", "")).strip()
    external_id = f"tg:{from_id or chat_id}"

    text = str(message.get("text", "") or "").strip()
    caption = str(message.get("caption", "") or "").strip()
    message_id = str(message.get("message_id", "")).strip()
    timestamp = str(message.get("date", "")).strip()

    voice = message.get("voice") if isinstance(message.get("voice"), dict) else {}
    photos = message.get("photo") if isinstance(message.get("photo"), list) else []
    largest_photo = photos[-1] if photos and isinstance(photos[-1], dict) else {}

    if text:
        event_type: EventType = "text"
    elif voice:
        event_type = "voice"
    elif largest_photo:
        event_type = "image"
    elif any(key in message for key in ("photo", "video", "sticker", "document", "audio", "voice")):
        event_type = "unsupported"
    else:
        event_type = "unsupported"

    file_id = ""
    mime_type = ""
    if event_type == "voice":
        file_id = str(voice.get("file_id", "")).strip()
        mime_type = str(voice.get("mime_type", "")).strip()
    elif event_type == "image":
        file_id = str(largest_photo.get("file_id", "")).strip()

    return {
        "source": "telegram",
        "event_type": event_type,
        "business_id": business_id,
        "channel": "telegram",
        "client": {
            "external_id": external_id,
            "chat_id": chat_id,
            "phone": "",
            "name": (
                str(from_data.get("first_name", "")).strip()
                or str(chat_data.get("first_name", "")).strip()
                or str(chat_data.get("title", "")).strip()
            ),
        },
        "message": {
            "message_id": message_id or str(payload.get("update_id", "")).strip(),
            "timestamp": timestamp,
            "text": text,
            "caption": caption,
            "file_id": file_id,
            "file_url": "",
            "mime_type": mime_type,
        },
        "raw_event_type": raw_event_type or "message",
        "raw": payload,
    }


def normalize_whatsapp_event(payload: dict, business_id: int) -> InternalEvent:
    if not isinstance(payload, dict):
        raise ValidationError("Unsupported WhatsApp payload.")

    type_webhook = str(payload.get("typeWebhook", "")).strip()
    message_data = payload.get("messageData", {}) if isinstance(payload.get("messageData"), dict) else {}

    # Green-API user inbound callbacks normally include typeWebhook=incomingMessageReceived,
    # but some legacy tests/payloads only include messageData. Treat those as inbound user
    # events instead of rejecting them as malformed.
    if not type_webhook and message_data:
        type_webhook = WHATSAPP_USER_EVENT_TYPE

    if type_webhook != WHATSAPP_USER_EVENT_TYPE:
        return {
            "source": "whatsapp",
            "event_type": "service",
            "business_id": business_id,
            "channel": "whatsapp",
            "client": {
                "external_id": "",
                "chat_id": "",
                "phone": "",
                "name": "",
            },
            "message": _empty_message_payload(),
            "raw_event_type": type_webhook or "service",
            "raw": payload,
        }

    sender_data = payload.get("senderData", {}) if isinstance(payload.get("senderData"), dict) else {}
    type_message = str(message_data.get("typeMessage", "")).strip()

    chat_id = str(sender_data.get("chatId", "")).strip()
    if chat_id.endswith("@g.us"):
        return {
            "source": "whatsapp",
            "event_type": "service",
            "business_id": business_id,
            "channel": "whatsapp",
            "client": {
                "external_id": "",
                "chat_id": chat_id,
                "phone": "",
                "name": str(sender_data.get("senderName", "")).strip(),
            },
            "message": _empty_message_payload(),
            "raw_event_type": "groupMessage",
            "raw": payload,
        }

    external_id = str(sender_data.get("sender", "")).strip() or chat_id
    phone = chat_id.replace("@c.us", "").replace("@g.us", "")
    if phone and not phone.startswith("+"):
        phone = f"+{phone}"

    text_message_data = (
        message_data.get("textMessageData", {})
        if isinstance(message_data.get("textMessageData"), dict)
        else {}
    )
    extended_text_data = (
        message_data.get("extendedTextMessageData", {})
        if isinstance(message_data.get("extendedTextMessageData"), dict)
        else {}
    )
    image_message_data = (
        message_data.get("imageMessageData", {})
        if isinstance(message_data.get("imageMessageData"), dict)
        else {}
    )
    file_message_data = (
        message_data.get("fileMessageData", {})
        if isinstance(message_data.get("fileMessageData"), dict)
        else {}
    )

    text = str(
        text_message_data.get("textMessage")
        or extended_text_data.get("text")
        or ""
    ).strip()
    caption = str(
        image_message_data.get("caption")
        or file_message_data.get("caption")
        or ""
    ).strip()
    message_id = (
        str(payload.get("idMessage", "")).strip()
        or str(message_data.get("idMessage", "")).strip()
    )
    timestamp = str(payload.get("timestamp", "")).strip()

    if type_message in {"textMessage", "extendedTextMessage"} and text:
        event_type: EventType = "text"
    elif type_message == "audioMessage":
        event_type = "voice"
    elif type_message == "imageMessage":
        event_type = "image"
    elif type_message:
        event_type = "unsupported"
    else:
        raise ValidationError("Unsupported WhatsApp payload.")

    file_url = ""
    mime_type = ""
    if event_type == "voice":
        file_url = str(
            file_message_data.get("downloadUrl")
            or file_message_data.get("downloadurl")
            or payload.get("downloadUrl")
            or payload.get("downloadurl")
            or ""
        ).strip()
        mime_type = str(file_message_data.get("mimeType", "")).strip()
    elif event_type == "image":
        file_url = str(
            image_message_data.get("downloadUrl")
            or image_message_data.get("downloadurl")
            or file_message_data.get("downloadUrl")
            or file_message_data.get("downloadurl")
            or ""
        ).strip()
        mime_type = str(
            image_message_data.get("mimeType")
            or file_message_data.get("mimeType")
            or ""
        ).strip()

    return {
        "source": "whatsapp",
        "event_type": event_type,
        "business_id": business_id,
        "channel": "whatsapp",
        "client": {
            "external_id": external_id,
            "chat_id": chat_id,
            "phone": phone,
            "name": str(sender_data.get("senderName", "")).strip(),
        },
        "message": {
            "message_id": message_id,
            "timestamp": timestamp,
            "text": text,
            "caption": caption,
            "file_id": "",
            "file_url": file_url,
            "mime_type": mime_type,
        },
        "raw_event_type": type_webhook,
        "raw": payload,
    }


def normalize_incoming_event(channel: str, payload: dict, business_id: int) -> InternalEvent:
    if channel == "telegram":
        return normalize_telegram_event(payload, business_id)
    if channel == "whatsapp":
        return normalize_whatsapp_event(payload, business_id)
    raise ValidationError(f"Unsupported channel for normalizer: {channel}")


def normalize_telegram_payload(payload: dict, business_id: int) -> dict:
    event = normalize_telegram_event(payload, business_id)
    message = event["message"]
    return {
        "business_id": event["business_id"],
        "external_id": event["client"]["external_id"],
        "phone": event["client"]["phone"],
        "name": event["client"]["name"],
        "text": message["text"] or message["caption"],
        "unsupported_media": event["event_type"] == "unsupported",
        "provider_event_id": str(payload.get("update_id", "")).strip() or message["message_id"],
    }


def legacy_payload_from_internal_event(event: InternalEvent) -> dict:
    text = event["message"]["text"] or event["message"]["caption"]
    return {
        "business_id": event["business_id"],
        "external_id": event["client"]["external_id"],
        "phone": event["client"]["phone"],
        "name": event["client"]["name"],
        "text": text,
        "unsupported_media": event["event_type"] in {"image", "unsupported"},
        "provider_event_id": event["message"]["message_id"],
        "media_type": event["event_type"],
        "audio_download_url": event["message"]["file_url"] if event["event_type"] == "voice" else "",
        "audio_mime_type": event["message"]["mime_type"] if event["event_type"] == "voice" else "",
        "audio_file_name": "",
        "telegram_file_id": (
            event["message"]["file_id"]
            if event.get("source") == "telegram" and event["event_type"] == "voice"
            else ""
        ),
    }
