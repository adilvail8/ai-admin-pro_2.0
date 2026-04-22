import json
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import ConversationMessage
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
