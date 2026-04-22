from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class SendResult:
    accepted: bool
    provider_message_id: str | None
    raw_response: dict
    delivered: bool = False
    error_code: str | None = None
    error_message: str | None = None


class OutboundTransport(Protocol):
    def send_text(
        self,
        *,
        recipient: str,
        text: str,
        metadata: dict | None = None,
    ) -> SendResult:
        ...


class StubTransport:
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


class WhatsAppTransport(StubTransport):
    channel = "whatsapp"


class TelegramTransport(StubTransport):
    channel = "telegram"


class InternalAlertTransport(StubTransport):
    channel = "internal"


def get_transport_for_channel(channel: str) -> OutboundTransport:
    transports: dict[str, OutboundTransport] = {
        "whatsapp": WhatsAppTransport(),
        "telegram": TelegramTransport(),
        "internal": InternalAlertTransport(),
    }
    try:
        return transports[channel]
    except KeyError as error:
        raise ValueError(f"Unsupported outbound channel: {channel}") from error
