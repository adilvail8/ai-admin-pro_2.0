from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Business, Client, ConversationMessage


class ClientIdentityResolver:
    CHANNEL_FIELD_MAP = {
        ConversationMessage.Channel.TELEGRAM: "telegram_id",
        ConversationMessage.Channel.WHATSAPP: "whatsapp_id",
    }

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
        phone = (phone or "").strip()
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
                if not phone:
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
