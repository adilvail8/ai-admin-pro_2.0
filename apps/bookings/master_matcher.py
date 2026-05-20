"""Master matching helpers.

Heuristics for recognising a master mentioned by name in a free-form
client message — handles transliteration of Latin-spelled names to
Cyrillic, builds short-name/full-name variants for fuzzy comparison,
and extracts unmatched candidate tokens (so the bot can say "we don't
have a master named X").

Dependencies: stdlib (re), Business model, text_utils._repair_mojibake,
and date_parser.MONTH_NAME_ALIASES (to exclude month words like "мая"
from master-name candidates). No webhooks imports — graph stays
one-way.
"""

import re

from .date_parser import MONTH_NAME_ALIASES
from .models import Business
from .text_utils import _repair_mojibake


# --- Transliteration table ---------------------------------------------

LATIN_TO_CYRILLIC_MAP = str.maketrans(
    {
        "a": "а",
        "b": "б",
        "c": "к",
        "d": "д",
        "e": "е",
        "f": "ф",
        "g": "г",
        "h": "х",
        "i": "и",
        "j": "ж",
        "k": "к",
        "l": "л",
        "m": "м",
        "n": "н",
        "o": "о",
        "p": "п",
        "q": "к",
        "r": "р",
        "s": "с",
        "t": "т",
        "u": "у",
        "v": "в",
        "w": "в",
        "x": "кс",
        "y": "й",
        "z": "з",
    }
)


# --- Transliteration ---------------------------------------------------

def transliterate_name_variant_to_cyrillic(value: str) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        return ""

    transliterated = normalized
    replacements = (
        ("shch", "\u0449"),
        ("sch", "\u0449"),
        ("zh", "\u0436"),
        ("kh", "\u0445"),
        ("sh", "\u0448"),
        ("ch", "\u0447"),
        ("ya", "\u044f"),
        ("yu", "\u044e"),
        ("yo", "\u0451"),
        ("ts", "\u0446"),
    )
    for source, target in replacements:
        transliterated = transliterated.replace(source, target)
    return transliterated.translate(LATIN_TO_CYRILLIC_MAP)


# --- Master name matching ----------------------------------------------

def build_master_name_variants(full_name: str) -> set[str]:
    normalized = (full_name or "").strip().lower()
    if not normalized:
        return set()

    variants = {normalized}
    parts = normalized.split()
    variants.update(parts)

    transliterated = transliterate_name_variant_to_cyrillic(normalized)
    if transliterated:
        variants.add(transliterated)
        variants.update(transliterated.split())

    compact_variants = set()
    for variant in variants:
        compact = re.sub(r"[^a-zа-яёқңғүұһәі]", "", variant)
        if compact:
            compact_variants.add(compact)
    variants.update(compact_variants)
    return {variant for variant in variants if variant}


def find_mentioned_master(*, business: Business, text: str):
    normalized = (text or "").strip().lower()
    compact_text = re.sub(r"[^a-zа-яёқңғүұһәі]", "", normalized)
    if not normalized:
        return None

    for master in business.masters.filter(is_active=True).order_by("full_name"):
        variants = build_master_name_variants(master.full_name)
        if any(variant in normalized or variant in compact_text for variant in variants):
            return master
    return None


# --- Unmatched candidate extraction ------------------------------------

MASTER_REFERENCE_STOPWORDS = {
    "да",
    "нет",
    "ок",
    "okay",
    "yes",
    "please",
    "пожалуйста",
    "тогда",
    "запишите",
    "запиши",
    "мастер",
    "мастера",
    "мастеру",
    "к",
    "с",
    "у",
    "на",
    "мне",
    "меня",
    "хочу",
    "можно",
    "пусть",
    "пойдет",
    "подойдет",
    "подходит",
    "ладно",
    "тогда",
    "әйел",
    "ер",
    "иә",
    "ия",
    "жарайды",
}


def extract_unmatched_master_candidate(*, business: Business, text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None
    if find_mentioned_master(business=business, text=normalized) is not None:
        return None

    tokens = re.findall(r"[a-zа-яёқңғүұөһі]{3,}", normalized)
    candidates = [
        token
        for token in tokens
        if token not in MASTER_REFERENCE_STOPWORDS and token not in MONTH_NAME_ALIASES
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None
