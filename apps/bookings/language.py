"""Language and locale helpers for client-facing replies.

Pure data + formatting helpers — no model imports, no AI dependencies.
The runtime language detector (`detect_client_language`) stays in
`webhooks.py` because it depends on the conversation-context builder
that has not yet been extracted.
"""

from datetime import datetime

from django.utils import timezone


# localized runtime replies keep emergency and media messages in the same language
# the client already chose during the conversation.
LOCALIZED_RUNTIME_MESSAGES = {
    "opt_out": {
        "ru": "Хорошо, больше не буду присылать напоминания по этой записи.",
        "kz": "Жақсы, бұл жазба бойынша енді еске салу хабарламаларын жібермеймін.",
    },
    "human_handoff": {
        "ru": "Сейчас подключу администратора.",
        "kz": "Қазір әкімшіге қосамын, ол сізге ары қарай көмектеседі.",
    },
    "human_handoff_delay": {
        "ru": "Передала запрос администратору. Если ответа не будет пару минут, напишите ещё раз.",
        "kz": "Сұрағыңызды әкімшіге жеткіздім. Егер жауап сәл кешіксе, маған тағы екі-үш минуттан кейін жаза салыңыз.",
    },
    "ai_retry": {
        "ru": "Извините, у меня сейчас не получилось сразу ответить. Напишите, пожалуйста, ещё раз через пару минут — я постараюсь помочь.",
        "kz": "Кешіріңіз, қазір бірден жауап бере алмай қалдым. Екі-үш минуттан кейін тағы жазсаңыз, көмектесуге тырысамын.",
    },
    "ai_clarify": {
        "ru": "Не совсем поняла. Уточните, пожалуйста, какую услугу хотите и на какой день?",
        "kz": "Толық түсінбедім. Қандай қызметке және қай күнге жазылғыңыз келеді?",
    },
    "no_active_bookings_status": {
        "ru": "У вас сейчас нет активных записей. Хотите записаться?",
        "kz": "Сізде қазір белсенді жазба жоқ. Жазылғыңыз келе ме?",
    },
    "voice_fallback": {
        "ru": "Я пока не смогла разобрать голосовое. Если удобно, продублируйте, пожалуйста, вопрос текстом.",
        "kz": "Дауыстық хабарламаны әзірге дұрыс түсіне алмадым. Ыңғайлы болса, сұрағыңызды мәтінмен жаза салыңыз.",
    },
}


SERVICE_NAME_LOCALIZATIONS = {
    "Women's Haircut": {"ru": "женская стрижка", "kz": "әйелдер шаш қиюы"},
    "Men's Haircut": {"ru": "мужская стрижка", "kz": "ерлер шаш қиюы"},
    "Fade Haircut": {"ru": "фейд-стрижка", "kz": "фейд шаш қию"},
    "Haircut + Beard Combo": {"ru": "стрижка и борода", "kz": "шаш қиюы және сақал"},
    "Beard Trim": {"ru": "стрижка бороды", "kz": "сақал қию"},
    "Kids Haircut": {"ru": "детская стрижка", "kz": "балалар шаш қиюы"},
    "Hair Coloring": {"ru": "окрашивание волос", "kz": "шаш бояу"},
    "Brow Shape + Tint": {"ru": "коррекция и окрашивание бровей", "kz": "қас пішіндеу және бояу"},
    "Lash Lift": {"ru": "лифтинг ресниц", "kz": "кірпік лифтингі"},
    "Express Makeup": {"ru": "экспресс-макияж", "kz": "экспресс макияж"},
    "Manicure + Gel Polish": {"ru": "маникюр с гель-лаком", "kz": "гель-лакпен маникюр"},
    "Pedicure": {"ru": "педикюр", "kz": "педикюр"},
}


MONTH_NAMES = {
    "ru": {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    },
    "kz": {
        1: "қаңтар",
        2: "ақпан",
        3: "наурыз",
        4: "сәуір",
        5: "мамыр",
        6: "маусым",
        7: "шілде",
        8: "тамыз",
        9: "қыркүйек",
        10: "қазан",
        11: "қараша",
        12: "желтоқсан",
    },
}


def get_localized_runtime_message(message_key: str, language: str) -> str:
    variants = LOCALIZED_RUNTIME_MESSAGES[message_key]
    return variants.get(language, variants["ru"])


def localize_service_name(name: str, language: str) -> str:
    translations = SERVICE_NAME_LOCALIZATIONS.get(name, {})
    return translations.get(language, translations.get("ru", name))


def format_local_date(value, *, language: str) -> str:
    month_name = MONTH_NAMES.get(language, MONTH_NAMES["ru"]).get(value.month, "")
    if language == "kz":
        return f"{value.day} {month_name}"
    return f"{value.day} {month_name}"


def format_local_datetime(value: datetime, *, language: str) -> str:
    local_value = timezone.localtime(value)
    return f"{format_local_date(local_value.date(), language=language)} {local_value:%H:%M}"
