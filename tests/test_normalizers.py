import pytest
from django.core.exceptions import ValidationError

from apps.bookings.normalizers import (
    normalize_incoming_event,
    normalize_telegram_event,
    normalize_whatsapp_event,
)


def test_normalize_telegram_event_maps_text_message():
    payload = {
        "update_id": 123456,
        "message": {
            "message_id": 77,
            "date": 1715000000,
            "chat": {"id": 998877},
            "from": {"id": 998877, "first_name": "Adil"},
            "text": "Здравствуйте",
        },
    }

    event = normalize_telegram_event(payload, 2)

    assert event["source"] == "telegram"
    assert event["event_type"] == "text"
    assert event["business_id"] == 2
    assert event["client"]["external_id"] == "tg:998877"
    assert event["client"]["chat_id"] == "998877"
    assert event["client"]["name"] == "Adil"
    assert event["message"]["message_id"] == "77"
    assert event["message"]["timestamp"] == "1715000000"
    assert event["message"]["text"] == "Здравствуйте"
    assert event["message"]["caption"] == ""


def test_normalize_telegram_event_maps_voice_message():
    payload = {
        "update_id": 123457,
        "message": {
            "message_id": 78,
            "date": 1715000001,
            "chat": {"id": 112233},
            "from": {"id": 112233, "first_name": "Aruzhan"},
            "voice": {
                "file_id": "voice-file-id",
                "mime_type": "audio/ogg",
            },
        },
    }

    event = normalize_telegram_event(payload, 2)

    assert event["event_type"] == "voice"
    assert event["message"]["file_id"] == "voice-file-id"
    assert event["message"]["mime_type"] == "audio/ogg"
    assert event["message"]["text"] == ""


def test_normalize_telegram_event_maps_photo_and_caption():
    payload = {
        "update_id": 123458,
        "message": {
            "message_id": 79,
            "date": 1715000002,
            "chat": {"id": 445566},
            "from": {"id": 445566, "first_name": "Dana"},
            "caption": "Хочу такой дизайн",
            "photo": [{"file_id": "small"}, {"file_id": "large"}],
        },
    }

    event = normalize_telegram_event(payload, 2)

    assert event["event_type"] == "image"
    assert event["message"]["file_id"] == "large"
    assert event["message"]["caption"] == "Хочу такой дизайн"
    assert event["message"]["text"] == ""


def test_normalize_telegram_event_marks_service_update():
    payload = {
        "update_id": 123459,
        "my_chat_member": {
            "chat": {"id": 445566},
        },
    }

    event = normalize_telegram_event(payload, 2)

    assert event["event_type"] == "service"
    assert event["raw_event_type"] == "my_chat_member"
    assert event["message"]["text"] == ""


def test_normalize_incoming_event_dispatches_telegram():
    payload = {
        "update_id": 123460,
        "message": {
            "message_id": 80,
            "chat": {"id": 999000},
            "from": {"id": 999000, "first_name": "Emir"},
            "text": "Привет",
        },
    }

    event = normalize_incoming_event("telegram", payload, 5)

    assert event["channel"] == "telegram"
    assert event["business_id"] == 5
    assert event["message"]["text"] == "Привет"


def test_normalize_whatsapp_event_maps_text_message():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "wamid-123",
        "timestamp": 1715000100,
        "senderData": {
            "chatId": "77070000004@c.us",
            "sender": "77070000004@c.us",
            "senderName": "Green User",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {
                "textMessage": "Привет из WhatsApp",
            },
        },
    }

    event = normalize_whatsapp_event(payload, 2)

    assert event["source"] == "whatsapp"
    assert event["event_type"] == "text"
    assert event["client"]["chat_id"] == "77070000004@c.us"
    assert event["client"]["external_id"] == "77070000004@c.us"
    assert event["client"]["phone"] == "+77070000004"
    assert event["message"]["message_id"] == "wamid-123"
    assert event["message"]["text"] == "Привет из WhatsApp"


def test_normalize_whatsapp_event_maps_extended_text_message():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "wamid-extended",
        "senderData": {
            "chatId": "77070000015@c.us",
            "sender": "77070000015@c.us",
            "senderName": "Reply User",
        },
        "messageData": {
            "typeMessage": "extendedTextMessage",
            "extendedTextMessageData": {
                "text": "РћС‚РІРµС‚ РЅР° С†РёС‚Р°С‚Сѓ",
            },
        },
    }

    event = normalize_whatsapp_event(payload, 2)

    assert event["event_type"] == "text"
    assert event["client"]["phone"] == "+77070000015"
    assert event["message"]["text"] == "РћС‚РІРµС‚ РЅР° С†РёС‚Р°С‚Сѓ"


def test_normalize_whatsapp_event_ignores_group_chat():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "wamid-group",
        "senderData": {
            "chatId": "120363012345@g.us",
            "sender": "77070000016@c.us",
            "senderName": "Group User",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {
                "textMessage": "РџСЂРёРІРµС‚ РёР· РіСЂСѓРїРїС‹",
            },
        },
    }

    event = normalize_whatsapp_event(payload, 2)

    assert event["event_type"] == "service"
    assert event["raw_event_type"] == "groupMessage"
    assert event["client"]["chat_id"] == "120363012345@g.us"
    assert event["client"]["phone"] == ""


def test_normalize_whatsapp_event_maps_audio_message():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "wamid-voice",
        "senderData": {
            "chatId": "77070000005@c.us",
            "senderName": "Voice User",
        },
        "messageData": {
            "typeMessage": "audioMessage",
            "fileMessageData": {
                "downloadUrl": "https://example.com/voice.ogg",
                "mimeType": "audio/ogg",
            },
        },
    }

    event = normalize_whatsapp_event(payload, 2)

    assert event["event_type"] == "voice"
    assert event["message"]["file_url"] == "https://example.com/voice.ogg"
    assert event["message"]["mime_type"] == "audio/ogg"


def test_normalize_whatsapp_event_maps_image_and_caption():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "wamid-image",
        "senderData": {
            "chatId": "77070000006@c.us",
            "senderName": "Image User",
        },
        "messageData": {
            "typeMessage": "imageMessage",
            "imageMessageData": {
                "downloadUrl": "https://example.com/image.jpg",
                "caption": "Хочу такой цвет",
                "mimeType": "image/jpeg",
            },
        },
    }

    event = normalize_whatsapp_event(payload, 2)

    assert event["event_type"] == "image"
    assert event["message"]["file_url"] == "https://example.com/image.jpg"
    assert event["message"]["caption"] == "Хочу такой цвет"
    assert event["message"]["mime_type"] == "image/jpeg"


def test_normalize_whatsapp_event_marks_service_update():
    payload = {
        "typeWebhook": "outgoingMessageStatus",
        "status": "delivered",
    }

    event = normalize_whatsapp_event(payload, 2)

    assert event["event_type"] == "service"
    assert event["raw_event_type"] == "outgoingMessageStatus"


def test_normalize_incoming_event_dispatches_whatsapp():
    payload = {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "wamid-321",
        "senderData": {
            "chatId": "77070000007@c.us",
            "senderName": "Dispatch User",
        },
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {
                "textMessage": "Сәлем",
            },
        },
    }

    event = normalize_incoming_event("whatsapp", payload, 3)

    assert event["channel"] == "whatsapp"
    assert event["business_id"] == 3
    assert event["message"]["text"] == "Сәлем"


def test_normalize_incoming_event_rejects_unknown_channel():
    with pytest.raises(ValidationError):
        normalize_incoming_event("viber", {}, 1)
