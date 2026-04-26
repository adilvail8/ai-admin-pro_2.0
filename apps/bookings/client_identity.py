from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Business, Client, ConversationMessage


class ClientIdentityResolver:
    CHANNEL_FIELD_MAP = {
        ConversationMessage.Channel.TELEGRAM: "telegram_id",
        ConversationMessage.Channel.WHATSAPP: "whatsapp_id",
    }

    def normalize_kz_phone(self, phone: str | None) -> str:
        raw_phone = (phone or "").strip()
        if not raw_phone:
            return ""

        digits = "".join(symbol for symbol in raw_phone if symbol.isdigit())
        if len(digits) == 11 and digits.startswith("8"):
            digits = f"7{digits[1:]}"
        elif len(digits) == 10:
            digits = f"7{digits}"

        if len(digits) != 11 or not digits.startswith("7"):
            raise ValidationError(
                "Phone number must be a valid Kazakhstan number."
            )

        return f"+{digits}"

    def resolve_or_create(
        self,
        *,
        business: Business,
        channel: str,
        phone: str | None,
        external_id: str | None,
        name: str | None,
    ) -> Client:
        channel_field = self.CHANNEL_FIELD_MAP.get(channel, "")
        phone = self.normalize_kz_phone(phone)
        external_id = (external_id or "").strip()
        name = (name or "").strip()

        if channel == ConversationMessage.Channel.WHATSAPP and not phone:
            raise ValidationError("WhatsApp messages require a phone number.")
        if channel == ConversationMessage.Channel.TELEGRAM and not external_id:
            raise ValidationError("Telegram messages require an external_id.")

        with transaction.atomic():
            client = None
            if channel_field and external_id:
                client = business.clients.filter(
                    **{channel_field: external_id}
                ).first()
            if client is None and phone:
                client = business.clients.filter(phone=phone).first()

            if client is None:
                if not phone and channel != ConversationMessage.Channel.TELEGRAM:
                    raise ValidationError(
                        "Phone number is required to create a new client."
                    )
                create_kwargs = {
                    "business": business,
                    "name": name,
                    "external_id": external_id,
                }
                if phone:
                    create_kwargs["phone"] = phone
                if channel_field and external_id:
                    create_kwargs[channel_field] = external_id
                try:
                    return Client.objects.create(**create_kwargs)
                except IntegrityError:
                    if channel_field and external_id:
                        existing = business.clients.filter(
                            **{channel_field: external_id}
                        ).first()
                        if existing is not None:
                            return existing
                    return business.clients.get(phone=phone)

            update_fields = []
            if name and client.name != name:
                client.name = name
                update_fields.append("name")
            if external_id and client.external_id != external_id:
                client.external_id = external_id
                update_fields.append("external_id")
            if channel_field and external_id and getattr(client, channel_field) != external_id:
                setattr(client, channel_field, external_id)
                update_fields.append(channel_field)
            if update_fields:
                update_fields.append("updated_at")
                client.save(update_fields=update_fields)
            return client
