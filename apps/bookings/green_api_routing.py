"""Маршрутизация Green-API (WhatsApp) webhook'ов по per-business creds.

До этого модуля business_id брался из URL/query/payload и просто
сверялся с whitelist GREEN_API_BUSINESS_IDS. Это позволяло клиенту,
знающему shared secret, подделать business_id и впрыснуть сообщение
в чужой tenant.

Новый контракт: business определяется только по
``instanceData.idInstance`` из payload через уникальное поле
``Business.green_api_instance_id``. URL/query — лишь для совместимости
с существующим роутингом; они сверяются с результатом lookup.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError

from .models import Business


def _extract_id_instance(payload: dict) -> str | None:
    """Достать ``idInstance`` из Green-API payload.

    Green-API кладёт его в ``instanceData.idInstance``; иногда дублирует
    на верхнем уровне. Возвращаем строку (instance id — число, но в БД
    хранится как ``CharField`` чтобы избежать сюрпризов с ведущими нулями
    и большими значениями).
    """
    instance_data = payload.get("instanceData")
    raw = None
    if isinstance(instance_data, dict):
        raw = instance_data.get("idInstance")
    if raw in (None, ""):
        raw = payload.get("idInstance")
    if raw in (None, ""):
        return None
    return str(raw).strip()


def resolve_business_from_green_api_payload(payload: dict) -> Business:
    """Найти active Business по ``idInstance`` из Green-API payload.

    Бросает ``ValidationError``:
    - если в payload нет ``idInstance``;
    - если ни один активный Business не привязан к этому instance.

    Lookup строится по уникальному ``green_api_instance_id`` (partial
    unique constraint на непустых значениях из миграции 0020).
    """
    id_instance = _extract_id_instance(payload)
    if not id_instance:
        raise ValidationError(
            "Green-API payload is missing instanceData.idInstance."
        )
    try:
        return Business.objects.get(
            green_api_instance_id=id_instance,
            is_active=True,
        )
    except Business.DoesNotExist as exc:
        raise ValidationError(
            f"No active business is bound to Green-API instance {id_instance}."
        ) from exc
