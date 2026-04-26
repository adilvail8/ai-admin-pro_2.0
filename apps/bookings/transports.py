from dataclasses import dataclass
from uuid import uuid4

import httpx
from django.conf import settings


@dataclass(frozen=True)
class SendResult:
    accepted: bool
    provider_message_id: str | None
    raw_response: dict
    delivered: bool = False
    error_code: str | None = None
    error_message: str | None = None


class OutboundTransport:
    def send_text(
        self,
        *,
        recipient: str,
        text: str,
        metadata: dict | None = None,
    ) -> SendResult:
        raise NotImplementedError


class StubTransport(OutboundTransport):
    channel = "stub"

    def send_text(
        self,
        *,
        recipient: str,
        text: str,
        metadata: dict | None = None,
    ) -> SendResult:
        return SendResult(
            accepted=True,
            delivered=False,
            provider_message_id=f"{self.channel}-{uuid4().hex}",
            raw_response={
                "channel": self.channel,
                "recipient": recipient,
                "text": text,
                "metadata": metadata or {},
                "mode": "stub",
            },
        )


class HTTPTransportBase(OutboundTransport):
    channel = "http"

    def __init__(self, *, timeout_seconds: int | None = None):
        self.timeout_seconds = (
            timeout_seconds or settings.OUTBOUND_TRANSPORT_TIMEOUT_SECONDS
        )

    def build_request(self, *, recipient: str, text: str, metadata: dict | None):
        raise NotImplementedError

    def extract_result(self, *, response_data: dict) -> SendResult:
        raise NotImplementedError

    def send_text(
        self,
        *,
        recipient: str,
        text: str,
        metadata: dict | None = None,
    ) -> SendResult:
        request = self.build_request(
            recipient=recipient,
            text=text,
            metadata=metadata,
        )
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    request["url"],
                    json=request.get("json"),
                    headers=request.get("headers"),
                )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            return SendResult(
                accepted=False,
                delivered=False,
                provider_message_id=None,
                raw_response={"channel": self.channel},
                error_code="timeout",
                error_message=str(error),
            )
        except httpx.HTTPError as error:
            response = getattr(error, "response", None)
            return SendResult(
                accepted=False,
                delivered=False,
                provider_message_id=None,
                raw_response={
                    "channel": self.channel,
                    "status_code": getattr(response, "status_code", None),
                    "body": getattr(response, "text", ""),
                },
                error_code="http_error",
                error_message=str(error),
            )

        response_data = response.json()
        return self.extract_result(response_data=response_data)


class WhatsAppTransport(HTTPTransportBase):
    channel = "whatsapp"

    @staticmethod
    def normalize_chat_id(recipient: str) -> str:
        normalized = recipient.strip()
        if normalized.endswith("@c.us") or normalized.endswith("@g.us"):
            return normalized
        digits_only = normalized.replace("+", "")
        return f"{digits_only}@c.us"

    def build_request(self, *, recipient: str, text: str, metadata: dict | None):
        if not settings.GREEN_API_URL:
            raise ValueError("GREEN_API_URL is not configured.")
        if not settings.GREEN_API_INSTANCE_ID or not settings.GREEN_API_API_TOKEN:
            raise ValueError(
                "GREEN_API_INSTANCE_ID and GREEN_API_API_TOKEN must be configured."
            )

        url = (
            f"{settings.GREEN_API_URL}/waInstance"
            f"{settings.GREEN_API_INSTANCE_ID}/sendMessage/"
            f"{settings.GREEN_API_API_TOKEN}"
        )
        return {
            "url": url,
            "json": {
                "chatId": self.normalize_chat_id(recipient),
                "message": text,
            },
            "headers": {"Content-Type": "application/json"},
        }

    def extract_result(self, *, response_data: dict) -> SendResult:
        message_id = (
            response_data.get("idMessage")
            or response_data.get("id")
            or response_data.get("messageId")
        )
        return SendResult(
            accepted=bool(message_id),
            delivered=False,
            provider_message_id=message_id,
            raw_response=response_data,
            error_code=None if message_id else "provider_rejected",
            error_message=None if message_id else "Green-API did not return a message id.",
        )


class TelegramTransport(HTTPTransportBase):
    channel = "telegram"

    @staticmethod
    def normalize_chat_id(recipient: str) -> str:
        normalized = recipient.strip()
        if normalized.startswith("tg:"):
            return normalized.removeprefix("tg:")
        return normalized

    def build_request(self, *, recipient: str, text: str, metadata: dict | None):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured.")

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        return {
            "url": url,
            "json": {
                "chat_id": self.normalize_chat_id(recipient),
                "text": text,
                "disable_web_page_preview": True,
            },
            "headers": {"Content-Type": "application/json"},
        }

    def extract_result(self, *, response_data: dict) -> SendResult:
        result = response_data.get("result", {})
        accepted = bool(response_data.get("ok")) and bool(result)
        provider_message_id = (
            str(result.get("message_id"))
            if result.get("message_id") is not None
            else None
        )
        return SendResult(
            accepted=accepted,
            delivered=False,
            provider_message_id=provider_message_id,
            raw_response=response_data,
            error_code=None if accepted else "provider_rejected",
            error_message=None if accepted else "Telegram API returned an unsuccessful response.",
        )


class InternalAlertTransport(HTTPTransportBase):
    channel = "internal"

    def build_request(self, *, recipient: str, text: str, metadata: dict | None):
        if not settings.INTERNAL_ALERT_WEBHOOK_URL:
            raise ValueError("INTERNAL_ALERT_WEBHOOK_URL is not configured.")

        headers = {"Content-Type": "application/json"}
        if settings.INTERNAL_ALERT_WEBHOOK_TOKEN:
            headers["Authorization"] = (
                f"Bearer {settings.INTERNAL_ALERT_WEBHOOK_TOKEN}"
            )
        return {
            "url": settings.INTERNAL_ALERT_WEBHOOK_URL,
            "json": {
                "recipient": recipient,
                "text": text,
                "metadata": metadata or {},
            },
            "headers": headers,
        }

    def extract_result(self, *, response_data: dict) -> SendResult:
        provider_message_id = (
            response_data.get("message_id")
            or response_data.get("id")
            or f"internal-{uuid4().hex}"
        )
        return SendResult(
            accepted=True,
            delivered=False,
            provider_message_id=str(provider_message_id),
            raw_response=response_data,
        )


def get_transport_for_channel(channel: str) -> OutboundTransport:
    transports: dict[str, OutboundTransport] = {
        "whatsapp": WhatsAppTransport(),
        "telegram": TelegramTransport(),
        "internal": InternalAlertTransport(),
    }
    return transports.get(channel, WhatsAppTransport())
