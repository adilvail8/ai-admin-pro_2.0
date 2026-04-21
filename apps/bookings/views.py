import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import ConversationMessage
from .webhooks import (
    get_or_create_client,
    handle_audio_message,
    handle_text_message,
    verify_green_api_request,
    verify_telegram_secret,
    verify_webhook_token,
)


@csrf_exempt
@require_POST
def messenger_webhook(request):
    try:
        verify_webhook_token(request.headers.get("X-Webhook-Token", ""))
    except ValidationError as error:
        return JsonResponse({"detail": str(error)}, status=403)

    payload = {}
    if request.content_type.startswith("application/json"):
        payload = json.loads(request.body.decode("utf-8"))
    else:
        payload = request.POST.dict()

    try:
        business_id = int(payload["business_id"])
        channel = payload["channel"]
        if channel not in {
            ConversationMessage.Channel.TELEGRAM,
            ConversationMessage.Channel.WHATSAPP,
        }:
            raise ValidationError("Unsupported channel.")
        client = get_or_create_client(
            business_id=business_id,
            channel=channel,
            external_id=payload.get("external_id", ""),
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
            return JsonResponse(result, status=200)

        text = payload.get("text", "").strip()
        if not text:
            raise ValidationError("Message text is required.")
        result = handle_text_message(
            business_id=business_id,
            channel=channel,
            client=client,
            text=text,
        )
        return JsonResponse(result, status=200)
    except (KeyError, ValueError, ValidationError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def telegram_webhook(request, secret: str):
    try:
        verify_telegram_secret(secret)
        payload = json.loads(request.body.decode("utf-8"))
        business_id = int(payload["business_id"])
        client = get_or_create_client(
            business_id=business_id,
            channel=ConversationMessage.Channel.TELEGRAM,
            external_id=str(payload.get("external_id", "")),
            phone=payload.get("phone", ""),
            name=payload.get("name", ""),
        )
        result = handle_text_message(
            business_id=business_id,
            channel=ConversationMessage.Channel.TELEGRAM,
            client=client,
            text=payload.get("text", "").strip(),
        )
        return JsonResponse(result, status=200)
    except (KeyError, ValueError, ValidationError) as error:
        return JsonResponse({"detail": str(error)}, status=400)


@csrf_exempt
@require_POST
def green_api_webhook(request):
    try:
        verify_green_api_request(
            token=request.headers.get("X-GreenAPI-Secret", ""),
            remote_addr=request.META.get("REMOTE_ADDR", ""),
        )
        payload = json.loads(request.body.decode("utf-8"))
        business_id = int(payload["business_id"])
        client = get_or_create_client(
            business_id=business_id,
            channel=ConversationMessage.Channel.WHATSAPP,
            external_id=str(payload.get("external_id", "")),
            phone=payload.get("phone", ""),
            name=payload.get("name", ""),
        )
        result = handle_text_message(
            business_id=business_id,
            channel=ConversationMessage.Channel.WHATSAPP,
            client=client,
            text=payload.get("text", "").strip(),
        )
        return JsonResponse(result, status=200)
    except (KeyError, ValueError, ValidationError) as error:
        return JsonResponse({"detail": str(error)}, status=400)
