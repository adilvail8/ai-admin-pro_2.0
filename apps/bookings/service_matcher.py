"""Service-and-master matching utilities.

Pure helpers used by reply builders to figure out which service a client
is asking about and which master should be recommended for it. Depends
only on Business/Service/Master models and the language helpers — no
imports from webhooks/replies, so it stays cycle-free.
"""

from .language import localize_service_name
from .models import Business


def infer_service_from_messages(*, business: Business, texts: list[str]):
    services = list(business.services.filter(is_active=True).order_by("name"))
    normalized_texts = [(text or "").strip().lower() for text in texts if (text or "").strip()]
    if not normalized_texts:
        return None

    def find_service_by_names(*names: str):
        lowered_names = {name.lower() for name in names}
        for service in services:
            if service.name.lower() in lowered_names:
                return service
        return None

    haircut_combo_service = find_service_by_names("Haircut + Beard Combo")
    beard_trim_service = find_service_by_names("Beard Trim")
    mens_haircut_service = find_service_by_names("Men's Haircut")

    beard_markers = (
        "бород",
        "бороду",
        "борода",
        "сақал",
        "сакал",
        "beard",
    )
    haircut_markers = (
        "стриж",
        "подстричь",
        "подстричься",
        "шаш қию",
        "шаш кию",
        "haircut",
        "fade",
    )

    for text in reversed(normalized_texts):
        if haircut_combo_service is not None and any(marker in text for marker in haircut_markers) and any(
            marker in text for marker in beard_markers
        ):
            return haircut_combo_service
        if beard_trim_service is not None and any(marker in text for marker in beard_markers):
            beard_only_markers = (
                "подровнять бороду",
                "стрижка бороды",
                "бороду хочу",
                "бороду подровнять",
                "trim beard",
            )
            if any(marker in text for marker in beard_only_markers):
                return beard_trim_service
        if mens_haircut_service is not None and any(marker in text for marker in ("мужск", "ерлер", "barber")):
            if not any(marker in text for marker in beard_markers):
                return mens_haircut_service

    service_aliases = {
        "Women's Haircut": ("женск", "әйел", "айел"),
        "Men's Haircut": ("мужск", "кроп", "barber"),
        "Haircut + Beard Combo": (
            "стрижка и борода",
            "стрижку и бороду",
            "стрижка + борода",
            "стрижку и подровнять бороду",
            "стрижка и подровнять бороду",
            "волосы и борода",
            "haircut beard",
            "combo",
        ),
        "Beard Trim": (
            "бород",
            "бороду",
            "борода",
            "подровнять бороду",
            "стрижка бороды",
            "сақал",
            "сакал",
            "beard trim",
        ),
        "Hair Coloring": ("окраш", "освет", "мелир", "бояу", "боя"),
        "Brow Shape + Tint": ("бров", "қас", "кас"),
        "Lash Lift": ("ресниц", "реснич", "кірпік", "кирпик", "lash"),
        "Express Makeup": ("макияж", "makeup", "визаж"),
        "Manicure + Gel Polish": ("маник", "ногт", "гель", "gel"),
        "Pedicure": ("педик",),
    }

    for text in reversed(normalized_texts):
        for service in services:
            variants = {
                service.name.lower(),
                localize_service_name(service.name, "ru").lower(),
                localize_service_name(service.name, "kz").lower(),
            }
            variants.update(service_aliases.get(service.name, ()))
            lowered_name = service.name.lower()
            if "haircut" in lowered_name:
                variants.update({"стриж", "шаш қию", "шаш кию", "кроп"})
            if "lash" in lowered_name:
                variants.update({"ресниц", "реснич", "кірпік", "кирпик"})
            if "manicure" in lowered_name:
                variants.update({"маник", "ногт"})
            if "pedicure" in lowered_name:
                variants.update({"педик"})
            if any(variant and variant in text for variant in variants):
                return service
    return None


def get_service_recommended_masters(*, business: Business, service):
    masters = business.masters.filter(is_active=True).order_by("full_name")
    ai_rules = business.ai_rules if isinstance(business.ai_rules, dict) else {}
    allowed_pairs = ai_rules.get("allowed_master_service_pairs", [])
    allowed_master_ids = [
        pair.get("master_id")
        for pair in allowed_pairs
        if isinstance(pair, dict) and pair.get("service_id") == service.id
    ]
    if allowed_master_ids:
        masters = masters.filter(id__in=allowed_master_ids)
    return list(masters)
