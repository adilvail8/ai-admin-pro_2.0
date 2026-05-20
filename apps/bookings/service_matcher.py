"""Service-and-master matching utilities.

Pure helpers used by reply builders to figure out which service a client
is asking about and which master should be recommended for it. Depends
only on Business/Service/Master models and the language helpers — no
imports from webhooks/replies, so it stays cycle-free.
"""

import re

from .language import localize_service_name
from .models import Business


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STEM_LEN = 5
_TOKEN_MIN_LEN = 3
_SCORE_THRESHOLD = 1.0


def _tokenize(text: str) -> list[str]:
    """Split text into word tokens, dropping anything shorter than 3 chars.

    Short tokens (predlogi like «и», single-letter abbreviations) just
    add noise to the overlap score, and the regex strips punctuation so
    «фейд-стрижка» splits into ["фейд", "стрижка"] (not a single token).
    """
    return [t for t in _WORD_RE.findall(text.lower()) if len(t) >= _TOKEN_MIN_LEN]


def _stem_match(a: str, b: str) -> bool:
    """Tokens count as a match if their first 5 chars agree.

    Catches Russian inflection (стрижка / стрижку / стрижки → стриж),
    but a typo like «стришка» fails because the 5-char stem «стриш»
    diverges from «стриж». For tokens shorter than 5 chars we fall back
    to strict equality so common 3- or 4-letter words (e.g. «фейд»,
    «fade») still match exactly.
    """
    if len(a) < _STEM_LEN or len(b) < _STEM_LEN:
        return a == b
    return a[:_STEM_LEN] == b[:_STEM_LEN]


def _service_name_token_score(text_tokens: list[str], name_tokens: list[str]) -> float:
    """``matched - 0.5 * unmatched`` over the service's tokens.

    Penalises service names with qualifier tokens not present in the
    user's text — bare «стрижка» should not steal the score from
    «фейд-стрижка» just because both contain «стрижка».
    """
    if not name_tokens:
        return 0.0
    matched = sum(
        1 for nt in name_tokens if any(_stem_match(tt, nt) for tt in text_tokens)
    )
    if matched == 0:
        return 0.0
    unmatched = len(name_tokens) - matched
    return matched - 0.5 * unmatched


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

    # Pass 1 — token-overlap scoring: tokenize the user's text and each
    # candidate service name, then award the service whose name tokens
    # are best covered by the user's tokens. Score combines match count
    # with a penalty for service tokens left uncovered, so:
    #   bare «стрижка» does NOT win Fade Haircut («фейд-стрижка»):
    #     matched=1 / unmatched=1 → 0.5  — below threshold, fall through
    #   «фейд-стрижка» wins Fade cleanly:
    #     matched=2 / unmatched=0 → 2.0  — above threshold, return
    #   typo «стришка» wins nothing — 5-char stem «стриш» ≠ «стриж».
    # Tie-break: shorter service name (more specific). Words ≤ 5 chars
    # match strictly, longer words compare by 5-char stem (Russian
    # inflection tolerance).
    for text in reversed(normalized_texts):
        text_tokens = _tokenize(text)
        if not text_tokens:
            continue
        best_match = None
        best_score = 0.0
        best_name_len = float("inf")
        for service in services:
            for variant_name in (
                service.name,
                localize_service_name(service.name, "ru"),
                localize_service_name(service.name, "kz"),
            ):
                name_tokens = _tokenize(variant_name)
                if not name_tokens:
                    continue
                score = _service_name_token_score(text_tokens, name_tokens)
                if score <= 0:
                    continue
                if score > best_score or (
                    score == best_score and len(name_tokens) < best_name_len
                ):
                    best_match = service
                    best_score = score
                    best_name_len = len(name_tokens)
        if best_match is not None and best_score >= _SCORE_THRESHOLD:
            return best_match

    service_aliases = {
        "Women's Haircut": ("женск", "әйел", "айел"),
        "Men's Haircut": ("мужск", "кроп", "barber"),
        "Fade Haircut": ("фейд", "fade"),
        "Kids Haircut": ("детск", "балалар", "детская стрижка"),
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
                variants.update({"шаш қию", "шаш кию", "кроп"})
                # «стриж» is only safe to add when the business has a
                # single haircut service — otherwise the alphabetically
                # first haircut absorbed a bare «стрижка» query (the
                # original Fade-vs-Men's-vs-Kids bug). With multiple
                # haircut services the user must qualify (фейд /
                # мужская / детская) — Pass 1 token scoring handles
                # those, this fallback would only get in its way.
                haircut_count = sum(
                    1 for s in services if "haircut" in s.name.lower()
                )
                if haircut_count == 1:
                    variants.add("стриж")
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


# --- Haircut request detection -----------------------------------------

def get_gendered_haircut_services(*, business: Business):
    mens = None
    womens = None
    for service in business.services.filter(is_active=True).order_by("name"):
        if service.name == "Men's Haircut":
            mens = service
        elif service.name == "Women's Haircut":
            womens = service
    return mens, womens


def is_haircut_service(service) -> bool:
    return service is not None and service.name in {"Men's Haircut", "Women's Haircut"}


def detect_generic_haircut_request(*, business: Business, text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    mens, womens = get_gendered_haircut_services(business=business)
    if mens is None or womens is None:
        return False

    generic_markers = ("стриж", "подстричь", "подстричься", "шаш қию", "шаш кию")
    male_markers = ("мужск", "кроп", "barber", "бород", "ерлер")
    female_markers = ("женск", "әйел", "айел", "девоч", "әйелдер")

    if not any(marker in normalized for marker in generic_markers):
        return False
    if any(marker in normalized for marker in male_markers):
        return False
    if any(marker in normalized for marker in female_markers):
        return False
    return True


# Generic haircut markers shared by detect_generic_haircut_request and
# the multi-haircut clarification path. "fade" appears here on purpose:
# a barbershop without Women's Haircut still needs the clarification
# when the client says bare "стрижка" / "fade haircut" at a multi-cut
# business.
_HAIRCUT_QUERY_MARKERS = (
    "стриж",
    "подстричь",
    "подстричься",
    "шаш қию",
    "шаш кию",
    "haircut",
    "fade",
)


def is_generic_haircut_query(text: str) -> bool:
    """True if the message looks like an unqualified "I want a haircut" ask.

    Used in front of the AI fallback: when a multi-haircut business
    can't unambiguously resolve a service from a bare "стрижка", we
    short-circuit with a deterministic clarification reply instead of
    letting the LLM hallucinate a service_id.
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _HAIRCUT_QUERY_MARKERS)


def get_active_haircut_services(*, business: Business):
    """Return active services whose name contains a haircut keyword."""
    haircut_name_markers = ("haircut", "стриж", "шаш")
    return [
        service
        for service in business.services.filter(is_active=True).order_by("name")
        if any(marker in service.name.lower() for marker in haircut_name_markers)
    ]


def business_has_multiple_haircut_services(business: Business) -> bool:
    return len(get_active_haircut_services(business=business)) >= 2
