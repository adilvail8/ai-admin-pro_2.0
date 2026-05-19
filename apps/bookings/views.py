import json
import logging
from datetime import timedelta
from hashlib import sha256

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .ai_manager import AIManager
from .audit import create_audit_log
from .health_checks import build_health_snapshot, check_broker_connection
from .models import ConversationMessage, OutboundMessage
from .normalizers import legacy_payload_from_internal_event, normalize_incoming_event
from .tasks import (
    dispatch_outbound_delivery,
    get_client_recipient,
    mark_outbound_as_failed,
    sync_booking_delivery_marker,
)
LOCALIZED_UNSUPPORTED_MEDIA_MESSAGES = {
    "generic": {
        "ru": "Я пока не умею полноценно разбирать такие сообщения. Если можете, напишите, пожалуйста, в двух словах текстом — и я сразу помогу.",
        "kz": "Мұндай хабарламаларды әзірге толық талдай алмаймын. Мүмкін болса, қысқаша мәтінмен жаза салыңыз — бірден көмектесемін.",
    },
    "image": {
        "ru": "Фото получила. Если можете, коротко напишите текстом, что именно хотите уточнить по этому фото — так я быстрее помогу.",
        "kz": "Фотоны алдым. Мүмкін болса, осы фото бойынша не білгіңіз келетінін қысқаша мәтінмен жаза салыңыз — сонда тезірек көмектесемін.",
    },
}


def build_unsupported_media_message(*, language: str, media_type: str = "") -> str:
    normalized_media_type = (media_type or "").lower()
    if "image" in normalized_media_type or "photo" in normalized_media_type:
        variants = LOCALIZED_UNSUPPORTED_MEDIA_MESSAGES["image"]
        return variants.get(language, variants["ru"])
    variants = LOCALIZED_UNSUPPORTED_MEDIA_MESSAGES["generic"]
    return variants.get(language, variants["ru"])


from .green_api_routing import resolve_business_from_green_api_payload
from .security import validate_green_api_business_id
from .webhooks import (
    get_business,
    get_or_create_client,
    handle_audio_message,
    handle_text_message,
    mark_inbound_event_failed,
    mark_inbound_event_processed,
    register_inbound_event,
    store_message,
    detect_client_language,
    get_localized_runtime_message,
    verify_green_api_request,
    verify_telegram_request,
    verify_telegram_secret,  # kept as deprecated alias for external callers
    verify_webhook_token,
)


logger = logging.getLogger(__name__)


UNSUPPORTED_MEDIA_MESSAGE = (
    "Пока я понимаю только текстовые сообщения. "
    "Напишите, пожалуйста, ваш вопрос текстом, и я сразу помогу."
)


def parse_request_payload(request):
    content_type = request.content_type or ""
    if content_type.startswith("application/json"):
        return json.loads(request.body.decode("utf-8"))
    return request.POST.dict()


def extract_provider_event_id(payload: dict, channel: str) -> str:
    provider_event_id = (
        str(payload.get("provider_event_id", "")).strip()
        or str(payload.get("message_id", "")).strip()
        or str(payload.get("event_id", "")).strip()
    )
    if provider_event_id:
        return provider_event_id
    digest = sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"{channel}:{digest}"


def download_whatsapp_audio(payload: dict):
    download_url = str(payload.get("audio_download_url", "")).strip()
    if not download_url:
        return None

    headers_candidates = [
        {},
        {"Authorization": f"Bearer {settings.GREEN_API_API_TOKEN}"},
        {"Authorization": settings.GREEN_API_API_TOKEN},
    ]
    for headers in headers_candidates:
        try:
            response = httpx.get(download_url, headers=headers, timeout=20.0)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            extension = ".ogg"
            if "mpeg" in content_type:
                extension = ".mp3"
            elif "wav" in content_type:
                extension = ".wav"
            return SimpleUploadedFile(
                payload.get("audio_file_name") or f"voice{extension}",
                response.content,
                content_type=content_type or payload.get("audio_mime_type") or "audio/ogg",
            )
        except Exception:
            continue
    return None


def download_telegram_voice(payload: dict, business):
    """Скачать голосовое из Telegram через Bot API getFile + file download.

    Два последовательных GET-а: метаданные (≤8s) + бинарь (≤15s). Суммарно
    под бюджет вебхука Telegram (~25-30s до тайм-аута). На любую ошибку
    возвращаем None — выше по стеку сработает voice_fallback.
    """
    file_id = str(payload.get("telegram_file_id", "")).strip()
    if not file_id:
        return None

    bot_token = (
        getattr(business, "telegram_bot_token", "") or settings.TELEGRAM_BOT_TOKEN
    )
    if not bot_token:
        return None

    try:
        meta_response = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=8.0,
        )
        meta_response.raise_for_status()
        file_path = (
            meta_response.json().get("result", {}).get("file_path", "")
        )
        if not file_path:
            return None

        file_response = httpx.get(
            f"https://api.telegram.org/file/bot{bot_token}/{file_path}",
            timeout=15.0,
        )
        file_response.raise_for_status()
    except Exception:
        logger.exception("telegram_voice_download_failed")
        return None

    content_type = (
        file_response.headers.get("Content-Type", "")
        or payload.get("audio_mime_type", "")
        or "audio/ogg"
    )
    extension = ".oga"
    if "mpeg" in content_type:
        extension = ".mp3"
    elif "wav" in content_type:
        extension = ".wav"
    return SimpleUploadedFile(
        f"voice{extension}",
        file_response.content,
        content_type=content_type,
    )


def normalize_whatsapp_green_api_payload(payload: dict, business_id: int) -> dict:
    event = normalize_incoming_event(
        ConversationMessage.Channel.WHATSAPP,
        payload,
        business_id,
    )
    return legacy_payload_from_internal_event(event)


def is_green_api_provider_payload(payload: dict) -> bool:
    return any(
        key in payload
        for key in (
            "typeWebhook",
            "senderData",
            "messageData",
            "idMessage",
        )
    )


def extract_green_api_business_id(*, payload: dict, request) -> int:
    """Резолвит business для Green-API webhook'а.

    Источник истины — ``instanceData.idInstance`` в payload, который
    маппится в ``Business.green_api_instance_id``. ``business_id`` из
    payload/query больше не доверяем (это было source of security debt).
    ``GREEN_API_BUSINESS_IDS`` whitelist остаётся как back-stop —
    финальная проверка после lookup.
    """
    business = resolve_business_from_green_api_payload(payload)
    return validate_green_api_business_id(business.id)


def process_internal_event(*, event: dict, request):
    if event.get("event_type") == "service":
        return JsonResponse(
            {"status": "ignored", "event_type": event.get("event_type")},
            status=200,
        )

    if event.get("channel") == ConversationMessage.Channel.TELEGRAM and event.get("event_type") == "text":
        payload = {
            "business_id": event["business_id"],
            "external_id": event["client"]["external_id"],
            "phone": event["client"]["phone"],
            "name": event["client"]["name"],
            "text": event["message"]["text"] or event["message"]["caption"],
            "unsupported_media": False,
            "provider_event_id": str(event["raw"].get("update_id", "")).strip() or event["message"]["message_id"],
        }
    else:
        payload = legacy_payload_from_internal_event(event)
    return process_webhook_request(
        payload=payload,
        request=request,
        channel=event["channel"],
    )


def process_webhook_request(*, payload: dict, request, channel: str):
    business_id = int(payload["business_id"])
    if channel == ConversationMessage.Channel.WHATSAPP:
        validate_green_api_business_id(business_id)
    business = get_business(business_id=business_id)
    provider_event_id = extract_provider_event_id(payload, channel)
    inbound_event, is_new = register_inbound_event(
        business=business,
        channel=channel,
        provider_event_id=provider_event_id,
        payload=payload,
    )
    if not is_new:
        return JsonResponse(
            {"status": "duplicate", "provider_event_id": provider_event_id},
            status=200,
        )

    try:
        client = get_or_create_client(
            business_id=business_id,
            channel=channel,
            external_id=str(payload.get("external_id", "")),
            phone=payload.get("phone", ""),
            name=payload.get("name", ""),
        )
        ai_manager = AIManager(business=business, client=client)
        audio_file = (
            request.FILES.get("audio")
            or request.FILES.get("voice")
            or download_telegram_voice(payload, business)
            or download_whatsapp_audio(payload)
        )
        preferred_language = detect_client_language(
            ai_manager=ai_manager,
            business_id=business_id,
            client=client,
            channel=channel,
            current_text=(payload.get("text", "") or "").strip(),
        )
        media_type = str(payload.get("media_type", "") or "").lower()
        text_value = (payload.get("text", "") or "").strip()
        is_audio_message = any(
            marker in media_type for marker in ("audio", "ptt", "voice")
        )
        has_unsupported_media = payload.get("unsupported_media", False) or (
            bool(request.FILES) and audio_file is None
        )
        if is_audio_message and audio_file is None and not text_value:
            voice_fallback_message = get_localized_runtime_message(
                "voice_fallback",
                preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=voice_fallback_message,
            )
            result = {
                "reply": voice_fallback_message,
                "escalated": False,
            }
        elif has_unsupported_media:
            unsupported_media_message = build_unsupported_media_message(
                language=preferred_language,
                media_type=str(payload.get("media_type", "")),
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=unsupported_media_message,
            )
            result = {
                "reply": unsupported_media_message,
                "escalated": False,
            }
        elif audio_file is not None:
            result = handle_audio_message(
                business_id=business_id,
                channel=channel,
                client=client,
                audio_file=audio_file,
                ai_manager=ai_manager,
            )
        else:
            result = handle_text_message(
                business_id=business_id,
                channel=channel,
                client=client,
                text=text_value,
                ai_manager=ai_manager,
            )

        if isinstance(result, dict):
            reply_text = (result.get("reply", "") or "").strip()
            if reply_text:
                # Idempotency guard: same client receiving the exact same
                # reply text within a short window almost always means a
                # duplicate dispatch — Green-API retrying a slow webhook
                # ack, a Telegram update + edit, or a Celery race. Skip
                # silently; the first OutboundMessage already covers this
                # response. register_inbound_event filters identical
                # provider_event_ids but doesn't catch retries that come
                # in with a fresh event id, hence this content-level guard.
                recent_window = timezone.now() - timedelta(seconds=30)
                duplicate = (
                    OutboundMessage.objects.filter(
                        business=business,
                        client=client,
                        channel=channel,
                        message_type="reply",
                        text=reply_text,
                        created_at__gte=recent_window,
                    )
                    .order_by("-created_at")
                    .first()
                )
                if duplicate is not None:
                    create_audit_log(
                        business=business,
                        client=client,
                        outbound_message=duplicate,
                        actor_type="system",
                        event_type="outbound_reply_skipped_duplicate",
                        channel=channel,
                        payload={
                            "inbound_event_id": inbound_event.id,
                            "original_outbound_id": duplicate.id,
                            "window_seconds": 30,
                        },
                    )
                    result.setdefault("outbound_message_id", duplicate.id)
                    result["deduped"] = True
                else:
                    outbound_message = OutboundMessage.objects.create(
                        business=business,
                        client=client,
                        channel=channel,
                        recipient=get_client_recipient(client, channel),
                        message_type="reply",
                        text=reply_text,
                    )
                    create_audit_log(
                        business=business,
                        client=client,
                        outbound_message=outbound_message,
                        actor_type="ai",
                        event_type="outbound_reply_queued",
                        channel=channel,
                        payload={"message_type": "reply"},
                    )
                    dispatch_result = dispatch_outbound_delivery(outbound_message.id)
                    result.setdefault("outbound_message_id", outbound_message.id)
                    if isinstance(dispatch_result, dict) and dispatch_result.get("status"):
                        result.setdefault(
                            "notification_status",
                            dispatch_result["status"],
                        )
        mark_inbound_event_processed(inbound_event)
        return JsonResponse(result, status=200)
    except Exception:
        mark_inbound_event_failed(inbound_event)
        raise


def verify_outbound_callback_token(token: str):
    expected_token = settings.OUTBOUND_CALLBACK_SECRET
    if not expected_token:
        raise ValidationError("Outbound callback secret is not configured.")
    if token != expected_token:
        raise ValidationError("Invalid outbound callback secret.")


def is_celery_eager_mode() -> bool:
    value = settings.CELERY_TASK_ALWAYS_EAGER
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@csrf_exempt
@require_POST
def messenger_webhook(request):
    try:
        verify_webhook_token(request.headers.get("X-Webhook-Token", ""))
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        payload = parse_request_payload(request)
        channel = payload["channel"]
        if channel not in {
            ConversationMessage.Channel.TELEGRAM,
            ConversationMessage.Channel.WHATSAPP,
        }:
            raise ValidationError("Unsupported channel.")
        return process_webhook_request(
            payload=payload,
            request=request,
            channel=channel,
        )
    except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def telegram_webhook(request, business_id: int, secret: str):
    try:
        verify_telegram_request(business_id=business_id, secret=secret)
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        event = normalize_incoming_event(
            ConversationMessage.Channel.TELEGRAM,
            parse_request_payload(request),
            business_id,
        )
        return process_internal_event(event=event, request=request)
    except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def green_api_webhook(request):
    try:
        verify_green_api_request(
            token=request.headers.get("X-GreenAPI-Secret", ""),
            authorization=request.headers.get("Authorization", ""),
            remote_addr=request.META.get("REMOTE_ADDR", ""),
        )
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        payload = parse_request_payload(request)
        if not is_green_api_provider_payload(payload):
            # Legacy internal-payload fallback (когда сюда прилетал наш
            # собственный формат `{business_id, external_id, phone, ...}`)
            # больше не принимается: единственная защита там — whitelist
            # GREEN_API_BUSINESS_IDS, а маршрутизация по уже привязанному
            # idInstance невозможна без provider payload. Реальные
            # Green-API клиенты всегда шлют provider payload — закрытие
            # ветки не ломает их. Для internal payloads используется
            # /api/v1/webhooks/whatsapp/<business_id>/.
            raise ValidationError(
                "Legacy internal payload at /api/v1/webhooks/green-api/ is "
                "no longer accepted. Use /api/v1/webhooks/whatsapp/"
                "<business_id>/ instead."
            )
        event = normalize_incoming_event(
            ConversationMessage.Channel.WHATSAPP,
            payload,
            extract_green_api_business_id(payload=payload, request=request),
        )
        return process_internal_event(event=event, request=request)
    except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def whatsapp_webhook(request, business_id: int):
    try:
        verify_green_api_request(
            token=request.headers.get("X-GreenAPI-Secret", ""),
            authorization=request.headers.get("Authorization", ""),
            remote_addr=request.META.get("REMOTE_ADDR", ""),
        )
        validate_green_api_business_id(business_id)
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        provider_payload = parse_request_payload(request)
        # Cross-check: URL/path business_id ДОЛЖЕН совпадать с business,
        # привязанным к idInstance из payload. Иначе атакующий может
        # отправить событие чужого Green-API instance на URL своего
        # business'а.
        resolved_business = resolve_business_from_green_api_payload(provider_payload)
        if resolved_business.id != business_id:
            raise ValidationError(
                "Green-API instance does not belong to the business in URL."
            )
        event = normalize_incoming_event(
            ConversationMessage.Channel.WHATSAPP,
            provider_payload,
            business_id,
        )
        return process_internal_event(event=event, request=request)
    except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def outbound_delivery_webhook(request):
    try:
        verify_outbound_callback_token(
            request.headers.get("X-Outbound-Callback-Secret", "")
        )
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        payload = parse_request_payload(request)
        provider_message_id = str(payload["provider_message_id"]).strip()
        delivery_status = str(payload["status"]).strip().lower()
        outbound_messages = OutboundMessage.objects.filter(
            provider_message_id=provider_message_id
        )
        channel = str(payload.get("channel", "")).strip()
        if channel:
            outbound_messages = outbound_messages.filter(channel=channel)
        business_id = payload.get("business_id")
        if business_id:
            outbound_messages = outbound_messages.filter(business_id=business_id)
        outbound_message = outbound_messages.order_by("-created_at").first()
        if outbound_message is None:
            raise OutboundMessage.DoesNotExist

        if delivery_status == "delivered":
            if outbound_message.status == OutboundMessage.Status.DELIVERED:
                return JsonResponse(
                    {
                        "outbound_message_id": outbound_message.id,
                        "status": outbound_message.status,
                    },
                    status=200,
                )
            outbound_message.status = OutboundMessage.Status.DELIVERED
            outbound_message.delivered_at = outbound_message.delivered_at or timezone.now()
            outbound_message.provider_response = {
                **outbound_message.provider_response,
                "delivery_callback": payload,
            }
            outbound_message.save(
                update_fields=["status", "delivered_at", "provider_response", "updated_at"]
            )
            create_audit_log(
                business=outbound_message.business,
                client=outbound_message.client,
                booking=outbound_message.booking,
                outbound_message=outbound_message,
                actor_type="provider",
                event_type="outbound_delivery_confirmed",
                channel=outbound_message.channel,
                payload=payload,
            )
            sync_booking_delivery_marker(outbound_message)
        elif delivery_status in {"failed", "failure", "undelivered", "rejected", "error"}:
            if outbound_message.status != OutboundMessage.Status.DELIVERED:
                retry_context = mark_outbound_as_failed(
                    outbound_message,
                    error_code=str(
                        payload.get("error_code")
                        or f"provider_{delivery_status}"
                    ),
                    error_message=str(
                        payload.get("error_message")
                        or payload.get("description")
                        or "Provider reported message delivery failure."
                    ),
                )
                outbound_message.refresh_from_db()
                outbound_message.provider_response = {
                    **outbound_message.provider_response,
                    "delivery_callback": payload,
                    "retry": retry_context or {},
                }
                outbound_message.save(
                    update_fields=["provider_response", "updated_at"]
                )
        return JsonResponse(
            {
                "outbound_message_id": outbound_message.id,
                "status": outbound_message.status,
            },
            status=200,
        )
    except (
        KeyError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
        OutboundMessage.DoesNotExist,
    ) as error:
        return JsonResponse({"detail": str(error)}, status=400)


def healthcheck(request):
    snapshot = build_health_snapshot()
    return JsonResponse(
        snapshot,
        status=200 if snapshot["status"] == "ok" else 503,
    )
