import json
from hashlib import sha256

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .audit import create_audit_log
from .models import ConversationMessage, OutboundMessage
from .webhooks import (
    get_business,
    get_or_create_client,
    handle_audio_message,
    handle_text_message,
    mark_inbound_event_failed,
    mark_inbound_event_processed,
    register_inbound_event,
    verify_green_api_request,
    verify_telegram_secret,
    verify_webhook_token,
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


def process_webhook_request(*, payload: dict, request, channel: str):
    business_id = int(payload["business_id"])
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

        audio_file = request.FILES.get("audio") or request.FILES.get("voice")
        if audio_file is not None:
            result = handle_audio_message(
                business_id=business_id,
                channel=channel,
                client=client,
                audio_file=audio_file,
            )
        else:
            result = handle_text_message(
                business_id=business_id,
                channel=channel,
                client=client,
                text=payload.get("text", "").strip(),
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
def telegram_webhook(request, secret: str):
    try:
        verify_telegram_secret(secret)
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        payload = parse_request_payload(request)
        return process_webhook_request(
            payload=payload,
            request=request,
            channel=ConversationMessage.Channel.TELEGRAM,
        )
    except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def green_api_webhook(request):
    try:
        verify_green_api_request(
            token=request.headers.get("X-GreenAPI-Secret", ""),
            remote_addr=request.META.get("REMOTE_ADDR", ""),
        )
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    try:
        payload = parse_request_payload(request)
        return process_webhook_request(
            payload=payload,
            request=request,
            channel=ConversationMessage.Channel.WHATSAPP,
        )
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
        outbound_message = OutboundMessage.objects.get(
            provider_message_id=provider_message_id
        )
        if delivery_status == "delivered":
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
        return JsonResponse(
            {
                "outbound_message_id": outbound_message.id,
                "status": outbound_message.status,
            },
            status=200,
        )
    except (KeyError, ValueError, ValidationError, json.JSONDecodeError, OutboundMessage.DoesNotExist) as error:
        return JsonResponse({"detail": str(error)}, status=400)


def healthcheck(request):
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        db_ok = False

    checks = {
        "database": "ok" if db_ok else "failed",
        "broker_configured": "ok" if settings.CELERY_BROKER_URL else "failed",
        "openai_configured": "ok" if settings.OPENAI_API_KEY else "degraded",
        "telegram_transport": (
            "ok" if settings.TELEGRAM_BOT_TOKEN else "degraded"
        ),
        "whatsapp_transport": (
            "ok"
            if (
                settings.GREEN_API_URL
                and settings.GREEN_API_INSTANCE_ID
                and settings.GREEN_API_API_TOKEN
            )
            else "degraded"
        ),
        "internal_alert_transport": (
            "ok" if settings.INTERNAL_ALERT_WEBHOOK_URL else "degraded"
        ),
    }
    overall_status = "ok" if checks["database"] == "ok" else "failed"
    return JsonResponse({"status": overall_status, "checks": checks}, status=200 if db_ok else 503)
