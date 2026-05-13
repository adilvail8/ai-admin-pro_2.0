"""Intent and text-classification helpers.

Pure functions and keyword tables for detecting client intent (catalog,
master list, price, hours, booking, out-of-scope, etc.) plus affirmative
signal checks.

Dependencies: only stdlib (``re``) and ``text_utils._repair_mojibake``.
No model or webhooks imports — safe to depend on from any module.
"""

import re

from .text_utils import _repair_mojibake


# --- Keyword tables -----------------------------------------------------

SERVICE_CATALOG_RU_KEYWORDS = (
    "какие услуги",
    "какие процедуры",
    "что у вас есть",
    "что вы делаете",
    "какие сервисы",
    "какие есть услуги",
    "какие есть процедуры",
)

SERVICE_CATALOG_KZ_KEYWORDS = (
    "қандай процедура",
    "кандай процедура",
    "қандай қызмет",
    "кандай кызмет",
    "не бар",
    "қандай қызметтер",
)

MASTER_LIST_RU_KEYWORDS = (
    "какие мастера",
    "какие специалисты",
    "кто у вас работает",
    "какие у вас мастера",
    "мастера есть",
)

MASTER_LIST_KZ_KEYWORDS = (
    "қандай шеберлер",
    "кандай шеберлер",
    "кім жұмыс істейді",
    "ким жумыс истейди",
    "шеберлер бар ма",
)

PRICE_RU_KEYWORDS = (
    "сколько стоит",
    "какая цена",
    "цена",
    "сколько будет стоить",
)

PRICE_KZ_KEYWORDS = (
    "бағасы",
    "багасы",
    "қанша тұрады",
    "канша турады",
    "қанша болады",
)

HOURS_RU_KEYWORDS = (
    "до скольки",
    "во сколько работаете",
    "работаете до",
    "часы работы",
    "график работы",
    "вы еще работаете",
)

HOURS_KZ_KEYWORDS = (
    "сағат нешеге дейін",
    "нешеге дейін",
    "қашанға дейін жұмыс",
    "жұмыс уақыты",
    "график",
)

BOOKING_INTENT_KEYWORDS = (
    "\u0437\u0430\u043f\u0438\u0441",
    "\u0437\u0430\u043f\u0438\u0448",
    "\u0445\u043e\u0447\u0443",
    "\u0445\u043e\u0442\u0435\u043b",
    "\u0445\u043e\u0442\u0435\u043b\u0430",
    "\u0434\u0430\u0432\u0430\u0439\u0442\u0435",
    "\u043d\u0443\u0436\u0435\u043d",
    "\u043d\u0443\u0436\u043d\u0430",
    "\u043d\u0443\u0436\u043d\u043e",
    "\u0441\u0435\u0433\u043e\u0434\u043d\u044f",
    "\u0437\u0430\u0432\u0442\u0440\u0430",
    "\u043f\u043e\u0441\u043b\u0435\u0437\u0430\u0432\u0442\u0440\u0430",
    "\u0441\u0432\u043e\u0431\u043e\u0434\u043d",
    "\u043e\u043a\u043d\u043e",
    "\u0443\u0441\u043f\u0435\u044e",
    "\u0443\u0441\u043f\u0435\u0432\u0430\u044e",
    "\u0436\u0430\u0437\u044b\u043b",
    "\u0436\u0430\u0437\u044b\u043f",
    "\u0431\u043e\u0441 \u0443\u0430\u049b\u044b\u0442",
    "\u0435\u0440\u0442\u0435\u04a3",
    "\u0435\u0440\u0442\u0435\u043d",
)

NON_BOOKING_SERVICE_QUESTION_KEYWORDS = (
    "\u0434\u0435\u043b\u0430\u0435\u0442",
    "\u0434\u0435\u043b\u0430\u0435\u0442\u0435",
    "\u0434\u0435\u043b\u0430\u044e\u0442",
    "\u043c\u043e\u0436\u043d\u043e \u043b\u0438",
    "\u0432\u043e \u0432\u0440\u0435\u043c\u044f",
    "\u0432\u043c\u0435\u0441\u0442\u0435 \u0441\u043e",
    "\u043c\u0443\u0436\u0447\u0438\u043d\u0430\u043c",
    "\u0436\u0435\u043d\u0449\u0438\u043d\u0430\u043c",
    "\u0434\u043b\u044f \u043c\u0443\u0436\u0447\u0438\u043d",
    "\u0434\u043b\u044f \u0436\u0435\u043d\u0449\u0438\u043d",
    "\u043f\u043e\u0434\u0445\u043e\u0434\u0438\u0442",
    "\u043f\u043e\u0434\u043e\u0439\u0434\u0435\u0442",
    "\u0441\u043e\u0432\u043c\u0435\u0441\u0442",
)

OUT_OF_SCOPE_KEYWORDS = (
    "полисем",
    "когнитив",
    "лингвист",
    "семинар",
    "универ",
    "домаш",
    "эссе",
    "реферат",
    "курсов",
    "диплом",
    "лекци",
    "экзамен",
    "билет",
    "интеграл",
    "уравнен",
    "математ",
    "программ",
    "python",
    "javascript",
    "код",
    "истори",
    "политик",
    "новост",
    "медицин",
    "диагноз",
    "юрид",
    "закон",
    "polysemy",
    "linguistic",
    "seminar",
    "homework",
    "essay",
)

PROMPT_INJECTION_KEYWORDS = (
    "игнорируй предыдущ",
    "забудь инструк",
    "системный промпт",
    "system prompt",
    "ты chatgpt",
    "ты gpt",
    "не будь ботом",
    "представь что ты",
    "ignore previous",
    "forget instructions",
)

OUT_OF_SCOPE_FOLLOWUP_KEYWORDS = (
    "пожалуйста помоги",
    "помоги пожалуйста",
    "ну помоги",
    "помоги",
    "пожалуйста",
    "прошу",
    "очень надо",
    "очень нужно",
    "почему нет",
    "ну пожалуйста",
    "please help",
    "help me",
)

SALON_FOLLOWUP_ALLOW_KEYWORDS = (
    "запис",
    "услуг",
    "цен",
    "стоим",
    "мастер",
    "салон",
    "барбер",
    "стриж",
    "бород",
    "волос",
    "окраш",
    "ресниц",
    "бров",
    "маникюр",
    "педикюр",
    "шаш",
    "сақал",
    "сакал",
    "қызмет",
    "кызмет",
)


# --- Intent detectors (Tier 1: pure) ------------------------------------

def detect_gratitude_message(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {
        "спасибо",
        "спс",
        "благодарю",
        "рахмет",
        "рахм",
        "thanks",
        "thank you",
    }


def detect_greeting_message(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return normalized in {
        "/start",
        "start",
        "здравствуйте",
        "здравствуй",
        "добрый день",
        "доброе утро",
        "добрый вечер",
        "привет",
        "сәлем",
        "салем",
        "сәлеметсіз бе",
        "салеметсиз бе",
    }


def detect_master_opinion_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    keywords = (
        "хороший мастер",
        "хороший?",
        "хорошая?",
        "нормальный мастер",
        "норм мастер",
        "как мастер",
        "как он",
        "как она",
        "good master",
    )
    return any(keyword in normalized for keyword in keywords)


def detect_master_recommendation_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    keywords = (
        "порекомендуй",
        "порекомендуйте",
        "посоветуй",
        "посоветуйте",
        "кого лучше",
        "какого мастера",
        "с каким мастером",
        "кого выбрать",
        "ұсыныңыз",
        "усыныныз",
        "қай шебер",
        "кай шебер",
        "кімге жазылайын",
        "кимге жазылайын",
        "кто мастер",
        "какой мастер",
        "какие мастера",
        "кто из мастеров",
        "уточните мастера",
        "кто работает",
    )
    return any(keyword in normalized for keyword in keywords)


def detect_cancellation_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    keywords = (
        "отмен",
        "не приду",
        "не смогу прийти",
        "не получится прийти",
        "хочу отменить",
        "болдырма",
        "келмеймін",
        "келе алмаймын",
        "жазбаны отмен",
    )
    return any(keyword in normalized for keyword in keywords)


# --- Intent detectors (Tier 2: keyword tables + siblings) --------------

def detect_service_catalog_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in SERVICE_CATALOG_RU_KEYWORDS) or any(
        keyword in normalized for keyword in SERVICE_CATALOG_KZ_KEYWORDS
    )


def detect_master_list_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in MASTER_LIST_RU_KEYWORDS) or any(
        keyword in normalized for keyword in MASTER_LIST_KZ_KEYWORDS
    )


def detect_price_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in PRICE_RU_KEYWORDS) or any(
        keyword in normalized for keyword in PRICE_KZ_KEYWORDS
    )


def detect_hours_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in HOURS_RU_KEYWORDS) or any(
        keyword in normalized for keyword in HOURS_KZ_KEYWORDS
    )


def detect_explicit_booking_intent(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in BOOKING_INTENT_KEYWORDS)


def detect_non_booking_service_question(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized or detect_explicit_booking_intent(normalized):
        return False
    return (
        "?" in normalized
        or any(keyword in normalized for keyword in NON_BOOKING_SERVICE_QUESTION_KEYWORDS)
    )


def detect_out_of_scope_request(text: str) -> bool:
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return False
    if any(keyword in normalized for keyword in PROMPT_INJECTION_KEYWORDS):
        return True
    return any(keyword in normalized for keyword in OUT_OF_SCOPE_KEYWORDS)


def detect_out_of_scope_followup_pressure(*, text: str, last_assistant_text: str) -> bool:
    if not last_assistant_rejected_out_of_scope(last_assistant_text):
        return False
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return False
    if detect_explicit_booking_intent(normalized) or detect_out_of_scope_request(normalized):
        return False
    if any(keyword in normalized for keyword in SALON_FOLLOWUP_ALLOW_KEYWORDS):
        return False
    if len(normalized) > 80:
        return False
    return any(keyword in normalized for keyword in OUT_OF_SCOPE_FOLLOWUP_KEYWORDS)


# --- Text classifiers and helpers --------------------------------------

def is_price_clarification_prompt(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return (
        "какая именно услуга" in normalized
        or "подскажу цену" in normalized
        or "қай қызметтің бағасын" in normalized
        or "бірден айтамын" in normalized
    )


def last_assistant_rejected_out_of_scope(text: str) -> bool:
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return False
    return (
        ("не помогу" in normalized and "только по услугам" in normalized)
        or ("салон" in normalized and "жазылу" in normalized)
    )


def is_affirmative_message(text: str) -> bool:
    normalized = (text or "").strip().lower()
    affirmative_variants = {
        "\u0434\u0430",
        "\u0430\u0433\u0430",
        "\u043e\u043a",
        "okay",
        "yes",
        "\u0443\u0433\u0443",
        "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0430\u044e",
        "\u0438\u04d9",
        "\u0438\u044f",
        "\u0445\u0430",
        "\u0436\u0430\u0440\u0430\u0439\u0434\u044b",
    }
    if normalized in affirmative_variants:
        return True
    try:
        repaired = normalized.encode("latin1").decode("utf-8").lower()
    except (UnicodeEncodeError, UnicodeDecodeError):
        repaired = normalized
    return repaired in affirmative_variants


def has_affirmative_signal(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if is_affirmative_message(normalized):
        return True
    patterns = (
        r"^(да|ага|ок|угу|подтверждаю)\b",
        r"^(иә|ия|жарайды|ха)\b",
    )
    for candidate in (normalized, _repair_mojibake(normalized)):
        if not candidate:
            continue
        if any(re.search(pattern, candidate) for pattern in patterns):
            return True
    return False
