import logging
import re
from itertools import islice
from types import SimpleNamespace
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .ai_manager import AIManager, AI_RETRY_MESSAGE, HUMAN_HANDOFF_MESSAGE, VOICE_FALLBACK_MESSAGE
from .client_identity import ClientIdentityResolver
from .conversation_threads import get_or_create_conversation_thread, is_bot_active
from .normalizers import normalize_telegram_event
from .models import Booking, BookingSession, Business, Client, ConversationMessage, InboundEvent, Master
from .session_state import (
    clear_booking_session,
    get_or_create_booking_session,
    set_session_selected_slot,
    set_session_service,
    set_session_slot_options,
)
from .services import create_appointment, get_available_slots
from .tasks import async_prune_history, notify_human_operator

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
    "voice_fallback": {
        "ru": "Я пока не смогла разобрать голосовое. Если удобно, продублируйте, пожалуйста, вопрос текстом.",
        "kz": "Дауыстық хабарламаны әзірге дұрыс түсіне алмадым. Ыңғайлы болса, сұрағыңызды мәтінмен жаза салыңыз.",
    },
}


def get_localized_runtime_message(message_key: str, language: str) -> str:
    variants = LOCALIZED_RUNTIME_MESSAGES[message_key]
    return variants.get(language, variants["ru"])


logger = logging.getLogger(__name__)
POST_BOOKING_CONTEXT_MESSAGE_LIMIT = 6

OPT_OUT_KEYWORDS = {"stop", "стоп", "отписаться", "не пиши", "не писать"}
RATE_LIMIT_MESSAGE = (
    "Слишком много сообщений за короткое время. Давайте продолжим через минуту."
)
HUMAN_HANDOFF_DELAY_MESSAGE = (
    "Не получилось сразу передать запрос администратору. "
    "Напишите еще раз через пару минут."
)


def verify_webhook_token(token: str):
    expected_token = settings.WEBHOOK_SHARED_SECRET
    if not expected_token:
        raise ValidationError("Webhook token is not configured.")
    if token != expected_token:
        raise ValidationError("Invalid webhook token.")


def normalize_telegram_payload(payload: dict, business_id: int) -> dict:
    event = normalize_telegram_event(payload, business_id)
    message = event["message"]
    return {
        "business_id": event["business_id"],
        "external_id": event["client"]["external_id"],
        "phone": event["client"]["phone"],
        "name": event["client"]["name"],
        "text": message["text"] or message["caption"],
        "unsupported_media": event["event_type"] == "unsupported",
        "provider_event_id": str(payload.get("update_id", "")).strip() or message["message_id"],
    }


def verify_telegram_secret(secret: str):
    expected_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected_secret:
        raise ValidationError("Telegram webhook secret is not configured.")
    if secret != expected_secret:
        raise ValidationError("Invalid Telegram webhook secret.")


def verify_green_api_request(
    *,
    token: str,
    authorization: str = "",
    remote_addr: str,
):
    candidate_token = token.strip()
    if not candidate_token and authorization:
        normalized_authorization = authorization.strip()
        if " " in normalized_authorization:
            _, candidate_token = normalized_authorization.split(" ", 1)
        else:
            candidate_token = normalized_authorization

    if candidate_token != settings.GREEN_API_SHARED_SECRET:
        raise ValidationError("Invalid Green-API signature.")
    if settings.GREEN_API_ALLOWED_IPS and remote_addr not in settings.GREEN_API_ALLOWED_IPS:
        raise ValidationError("Green-API IP is not allowed.")


def get_business(*, business_id: int) -> Business:
    return Business.objects.get(pk=business_id, is_active=True)


def get_or_create_client(
    *,
    business_id: int,
    channel: str,
    external_id: str = "",
    phone: str = "",
    name: str = "",
):
    business = get_business(business_id=business_id)
    return ClientIdentityResolver().resolve_or_create(
        business=business,
        channel=channel,
        external_id=external_id,
        phone=phone,
        name=name,
    )


def register_inbound_event(
    *,
    business: Business,
    channel: str,
    provider_event_id: str,
    payload: dict,
) -> tuple[InboundEvent, bool]:
    try:
        with transaction.atomic():
            event = InboundEvent.objects.create(
                business=business,
                channel=channel,
                provider_event_id=provider_event_id,
                payload=payload,
            )
        return event, True
    except IntegrityError:
        event = InboundEvent.objects.get(
            business=business,
            channel=channel,
            provider_event_id=provider_event_id,
        )
        return event, False


def mark_inbound_event_processed(event: InboundEvent):
    event.status = InboundEvent.Status.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at"])


def mark_inbound_event_failed(event: InboundEvent):
    event.status = InboundEvent.Status.FAILED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at"])


def get_latest_active_booking(*, business_id: int, client: Client):
    return (
        Booking.objects.filter(
            business_id=business_id,
            client=client,
            status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
        )
        .order_by("-created_at")
        .first()
    )


def store_message(*, business_id: int, client: Client, channel: str, role: str, content: str):
    message = ConversationMessage.objects.create(
        business_id=business_id,
        client=client,
        channel=channel,
        role=role,
        content=content,
    )
    message_count = ConversationMessage.objects.filter(
        business_id=business_id,
        client_id=client.id,
        channel=channel,
    ).count()
    if message_count >= 20 and message_count % 10 == 0:
        if settings.CELERY_TASK_ALWAYS_EAGER:
            async_prune_history.apply(
                kwargs={
                    "business_id": business_id,
                    "client_id": client.id,
                    "channel": channel,
                }
            ).get()
        else:
            async_prune_history.delay(
                business_id=business_id,
                client_id=client.id,
                channel=channel,
            )
    return message


def build_conversation_context(
    *,
    business_id: int,
    client: Client,
    channel: str,
    max_messages: int | None = None,
):
    messages = list(
        ConversationMessage.objects.filter(
            business_id=business_id,
            client=client,
            channel=channel,
        )
        .order_by("created_at", "id")
        .values("role", "content")
    )
    if max_messages is not None and max_messages > 0:
        messages = messages[-max_messages:]
    return [{"role": item["role"], "content": item["content"]} for item in messages]


def should_limit_post_booking_context(*, session: BookingSession, booking, text: str) -> bool:
    if booking is None or session.state != BookingSession.State.IDLE:
        return False

    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    if len(normalized) <= 80:
        return True

    return any(
        keyword in normalized
        for keyword in (
            "запис",
            "подтверж",
            "ок",
            "okay",
            "спасибо",
            "рахмет",
            "она",
            "он",
            "мастер",
        )
    )


def detect_client_language(
    *,
    ai_manager: AIManager,
    business_id: int,
    client: Client,
    channel: str,
    current_text: str = "",
):
    conversation_messages = build_conversation_context(
        business_id=business_id,
        client=client,
        channel=channel,
    )
    if current_text.strip():
        conversation_messages.append(
            {"role": ConversationMessage.Role.USER, "content": current_text.strip()}
        )
    return ai_manager.infer_response_language(conversation_messages)


def enforce_client_rate_limit(*, business_id: int, client: Client, channel: str):
    window_start = timezone.now() - timedelta(minutes=1)
    recent_messages_count = ConversationMessage.objects.filter(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.USER,
        created_at__gte=window_start,
    ).count()
    if recent_messages_count >= settings.MAX_MESSAGES_PER_MINUTE:
        raise ValidationError(RATE_LIMIT_MESSAGE)


def process_opt_out(*, client: Client, text: str):
    if (text or "").strip().lower() in OPT_OUT_KEYWORDS:
        client.allow_follow_up = False
        client.save(update_fields=["allow_follow_up", "updated_at"])
        return True
    return False


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


def detect_hours_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in HOURS_RU_KEYWORDS) or any(
        keyword in normalized for keyword in HOURS_KZ_KEYWORDS
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


def last_assistant_rejected_out_of_scope(text: str) -> bool:
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return False
    return (
        ("не помогу" in normalized and "только по услугам" in normalized)
        or ("салон" in normalized and "жазылу" in normalized)
    )


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


def build_greeting_reply(*, language: str) -> str:
    if language == "kz":
        return "Сәлеметсіз бе! Қандай қызметке жазылайын?"
    return "Здравствуйте! На какую услугу хотите записаться?"


def build_booking_intent_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қандай қызметке және қай күнге жазайын?"
    return "На какую услугу и на какой день записать?"


def build_out_of_scope_reply(*, language: str) -> str:
    if language == "kz":
        return "Бұл тақырыпқа көмектесе алмаймын. Мен салон қызметтері, баға, шеберлер және жазылу бойынша көмектесемін."
    return "С этим не помогу. Я отвечаю только по услугам, ценам, мастерам и записи в салон."


def build_gratitude_reply(*, language: str) -> str:
    if language == "kz":
        return "Оқасы жоқ."
    return "Пожалуйста."


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


def build_master_opinion_reply(*, master: Master, service=None, language: str) -> str:
    service_name = localize_service_name(service.name, language) if service is not None else ""
    specialization = (master.specialization or "").strip().lower()
    if language == "kz":
        if service is not None:
            return f"{master.full_name} осы {service_name} бойынша жұмыс істейді. Қаласаңыз, осы шеберге жазып қоямын."
        return f"{master.full_name} — {specialization}. Қай қызмет керек екенін жаза салыңыз, бірден бағыттаймын."
    if service is not None:
        return f"{master.full_name} у нас работает по услуге «{service_name}». Если хотите, могу записать к нему."
    return f"{master.full_name} — {specialization}. Напишите, на что хотите записаться, и я сразу сориентирую."


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


def build_unknown_master_reply(*, service, candidate: str, actual_master_name: str | None, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        if actual_master_name:
            return (
                f"{candidate.capitalize()} деген шеберді таппадым. "
                f"{service_name} үшін бұл уақытта қолжетімді шебер: {actual_master_name}."
            )
        return (
            f"{candidate.capitalize()} деген шеберді таппадым. "
            f"{service_name} үшін нақты шеберді тізімнен ұсынамын."
        )
    if actual_master_name:
        return (
            f"Мастера «{candidate.capitalize()}» у нас не вижу. "
            f"Для услуги «{service_name}» на это время доступен мастер {actual_master_name}."
        )
    return (
        f"Мастера «{candidate.capitalize()}» у нас не вижу. "
        f"Для услуги «{service_name}» могу подсказать реальных мастеров из салона."
    )


def build_current_session_master_reply(*, session: BookingSession, language: str) -> str:
    if session.master_id and session.master is not None and session.service_id and session.service is not None:
        service_name = localize_service_name(session.service.name, language)
        if language == "kz":
            return (
                f"Қазір {service_name} бойынша сізге {session.master.full_name} шебері таңдалған. "
                "Қаласаңыз, жазбаны жалғастырамын."
            )
        return (
            f"Сейчас по услуге «{service_name}» у вас выбран мастер {session.master.full_name}. "
            "Если хотите, продолжу запись."
        )

    if session.service_id and session.service is not None:
        return build_service_master_options_reply(
            business=session.business,
            language=language,
            texts=[],
            service=session.service,
        )

    return build_master_list_reply(business=session.business, language=language)


def build_session_master_mismatch_reply(
    *,
    session: BookingSession,
    mentioned_master: Master,
    language: str,
) -> str:
    service_name = localize_service_name(session.service.name, language)
    current_master_name = session.master.full_name if session.master_id and session.master is not None else ""

    if language == "kz":
        if current_master_name:
            return (
                f"{mentioned_master.full_name} шебері {mentioned_master.specialization.lower()} бағытымен жұмыс істейді. "
                f"Ал {service_name} бойынша қазір {current_master_name} шебері таңдалған."
            )
        return (
            f"{mentioned_master.full_name} шебері {mentioned_master.specialization.lower()} бағытымен жұмыс істейді. "
            f"{service_name} үшін лайықты шеберді ұсынып беремін."
        )

    if current_master_name:
        return (
            f"{mentioned_master.full_name} — мастер по направлению {mentioned_master.specialization.lower()}. "
            f"Для услуги «{service_name}» сейчас у вас выбран мастер {current_master_name}."
        )
    return (
        f"{mentioned_master.full_name} работает как {mentioned_master.specialization.lower()}. "
        f"Для услуги «{service_name}» я лучше подскажу подходящего мастера."
    )


def build_session_master_match_reply(
    *,
    session: BookingSession,
    mentioned_master: Master,
    language: str,
) -> str:
    service_name = localize_service_name(session.service.name, language)
    if language == "kz":
        return (
            f"Иә, {mentioned_master.full_name} шебері {service_name} бойынша қолайлы. "
            "Қаласаңыз, осы шебермен жалғастырамыз."
        )
    return (
        f"Да, {mentioned_master.full_name} подходит для услуги «{service_name}». "
        "Если хотите, продолжим запись с этим мастером."
    )


def build_master_recommendation_reply(*, business: Business, language: str, texts: list[str], service=None) -> str:
    service = service or infer_service_from_messages(business=business, texts=texts)
    if service is None:
        return build_master_list_reply(business=business, language=language)

    masters = get_service_recommended_masters(business=business, service=service)
    if not masters:
        return build_master_list_reply(business=business, language=language)

    recommended_master = masters[0]
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        return (
            f"{service_name} үшін {recommended_master.full_name} шеберін ұсына аламын. "
            f"Ол {recommended_master.specialization.lower()} бағытымен жұмыс істейді. "
            "Егер ыңғайлы болса, осы шеберге жаздырып қоямын."
        )
    return (
        f"Для услуги «{service_name}» я бы предложила мастера {recommended_master.full_name}. "
        f"Она работает как {recommended_master.specialization.lower()}. "
        "Если подходит, могу записать именно к ней."
    )


def build_service_master_options_reply(*, business: Business, language: str, texts: list[str], service=None) -> str:
    service = service or infer_service_from_messages(business=business, texts=texts)
    if service is None:
        return build_master_list_reply(business=business, language=language)

    masters = get_service_recommended_masters(business=business, service=service)
    if not masters:
        return build_master_list_reply(business=business, language=language)

    service_name = localize_service_name(service.name, language)
    if language == "kz":
        if len(masters) == 1:
            master = masters[0]
            return (
                f"{service_name} үшін бізде {master.full_name} шебері жұмыс істейді. "
                f"Ол {master.specialization.lower()} бағытымен айналысады. "
                "Қаласаңыз, осы шеберге жаздырып қоямын."
            )
        master_lines = "; ".join(f"{master.full_name} — {master.specialization}" for master in masters[:4])
        return (
            f"{service_name} үшін мына шеберлер қолжетімді: {master_lines}. "
            "Қайсысы ыңғайлы екенін жаза салыңыз, мен бірден жалғастырамын."
        )

    if len(masters) == 1:
        master = masters[0]
        return (
            f"На услугу «{service_name}» у нас работает мастер {master.full_name}. "
            f"Она специализируется на {master.specialization.lower()}. "
            "Если подходит, могу записать именно к ней."
        )

    master_lines = "; ".join(f"{master.full_name} — {master.specialization}" for master in masters[:4])
    return (
        f"На услугу «{service_name}» у нас работают такие мастера: {master_lines}. "
        "Напишите, кто из них вам ближе, и я продолжу запись."
    )


def build_service_catalog_reply(*, business: Business, language: str) -> str:
    services = list(
        business.services.filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )
    if not services:
        if language == "kz":
            return "Қазір нақты қызметтер тізімі қолымда жоқ. Қаласаңыз, қай қызмет қызықтыратынын жаза салыңыз, мен көмектесемін."
        return "Сейчас у меня под рукой нет точного списка услуг. Если хотите, напишите, что вас интересует, и я помогу сориентироваться."

    service_names = [localize_service_name(service.name, language) for service in services]
    short_list = ", ".join(islice(service_names, 0, 6))
    if language == "kz":
        return (
            f"Бізде мынадай қызметтер бар: {short_list}. "
            "Қаласаңыз, керегін жаза салыңыз — бағасын не бос уақытты бірден қарап беремін."
        )
    return (
        f"У нас есть такие услуги: {short_list}. "
        "Если хотите, могу сразу подсказать по нужной услуге, цене или свободному времени."
    )


def build_master_list_reply(*, business: Business, language: str) -> str:
    masters = list(
        business.masters.filter(is_active=True).order_by("full_name").values_list(
            "full_name",
            "specialization",
        )
    )
    if not masters:
        if language == "kz":
            return "Қазір белсенді шеберлер тізімі бос тұр. Қаласаңыз, қай қызмет керек екенін жаза салыңыз, мен бағыттаймын."
        return "Сейчас активных мастеров в списке нет. Если хотите, напишите, какая услуга вас интересует, и я сориентирую."

    if language == "kz":
        master_lines = "; ".join(f"{name} — {specialization}" for name, specialization in masters[:4])
        return (
            f"Бізде қазір мына шеберлер жұмыс істейді: {master_lines}. "
            "Қаласаңыз, қай қызмет қызықтыратынын жаза салыңыз, мен лайықты шеберді ұсынамын."
        )
    master_lines = "; ".join(f"{name} — {specialization}" for name, specialization in masters[:4])
    return (
        f"У нас сейчас работают такие мастера: {master_lines}. "
        "Если хотите, могу сразу подсказать, кто лучше подойдет под нужную услугу."
    )


def build_service_price_reply(*, service, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    duration_minutes = int((service.duration or timedelta()).total_seconds() // 60)
    price_value = int(service.price)
    if language == "kz":
        if duration_minutes > 0:
            return (
                f"{service_name} бағасы {price_value} тг. "
                f"Ұзақтығы шамамен {duration_minutes} минут."
            )
        return f"{service_name} бағасы {price_value} тг."
    if duration_minutes > 0:
        return (
            f"{service_name.capitalize()} стоит {price_value} тг. "
            f"По времени это примерно {duration_minutes} минут."
        )
    return f"{service_name.capitalize()} стоит {price_value} тг."


def build_price_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қай қызметтің бағасын білгіңіз келетінін жаза салыңыз, мен бірден айтамын."
    return "Напишите, пожалуйста, какая именно услуга вас интересует, и я сразу подскажу цену."


def build_working_hours_reply(*, business: Business, language: str) -> str:
    hours_text = (business.working_hours or "").strip()
    if not hours_text:
        if language == "kz":
            return "Жұмыс уақытын қазір нақтылап айта алмаймын, бірақ қажет болса әкімшіге қалдырып беремін."
        return "Сейчас не вижу точный график работы, но при необходимости могу передать вопрос администратору."

    if "-" in hours_text:
        start_time, end_time = [part.strip() for part in hours_text.split("-", 1)]
        if language == "kz":
            return f"Бүгін біз {start_time}-ден {end_time}-ге дейін жұмыс істейміз."
        return f"Сегодня мы работаем с {start_time} до {end_time}."

    if language == "kz":
        return f"Біздің жұмыс уақытымыз: {hours_text}."
    return f"Наш график работы: {hours_text}."


def build_haircut_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қайсысы керек: ерлер шаш қиюы ма, әлде әйелдер шаш қиюы ма?"
    return "Уточните, пожалуйста: мужская или женская стрижка?"


def request_human_handoff(*, booking, reason: str, attempts: int, language: str = "ru"):
    if booking is None:
        return {
            "reply": get_localized_runtime_message("human_handoff_delay", language),
            "escalated": False,
        }

    handoff_result = (
        notify_human_operator.apply(
            kwargs={
                "booking_id": booking.id,
                "reason": reason,
                "attempts": attempts,
            }
        ).get()
        if settings.CELERY_TASK_ALWAYS_EAGER
        else {
            "notification_status": "queued",
            "delivery_task_id": notify_human_operator.delay(
                booking_id=booking.id,
                reason=reason,
                attempts=attempts,
            ).id,
        }
    )
    if handoff_result.get("notification_status") in {
        "queued",
        "submitted",
        "delivered",
    }:
        return {
            "reply": get_localized_runtime_message("human_handoff", language),
            "escalated": True,
        }
    return {
        "reply": get_localized_runtime_message("human_handoff_delay", language),
        "escalated": False,
    }


def build_cancellation_reply(*, language: str) -> str:
    if language == "kz":
        return "Түсіндім, жазбаны тоқтату өтінішіңізді әкімшіге бірден жіберемін. Ол сізбен жақын арада байланысады."
    return "Поняла, передам администратору запрос на отмену записи. Он свяжется с вами и подтвердит отмену."


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


def _repair_mojibake(value: str) -> str:
    try:
        return value.encode("latin1").decode("utf-8").lower()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


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


def is_unspecified_other_day_request(text: str) -> bool:
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return False
    return (
        any(phrase in normalized for phrase in ("другой день", "другой", "басқа күн", "баска кун"))
        and parse_explicit_calendar_date(normalized) is None
    )


def is_brief_post_booking_follow_up(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    brief_affirmative_variants = {
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
    if normalized in brief_affirmative_variants:
        return True
    repaired = _repair_mojibake(normalized)
    return repaired in brief_affirmative_variants


MONTH_NAME_ALIASES = {
    "января": 1,
    "янв": 1,
    "ақпан": 2,
    "акпан": 2,
    "февраля": 2,
    "фев": 2,
    "наурыз": 3,
    "марта": 3,
    "мар": 3,
    "сәуір": 4,
    "сәуiр": 4,
    "сэуір": 4,
    "апреля": 4,
    "апр": 4,
    "мамыр": 5,
    "мая": 5,
    "май": 5,
    "маусым": 6,
    "июня": 6,
    "июн": 6,
    "шілде": 7,
    "шилде": 7,
    "июля": 7,
    "июл": 7,
    "тамыз": 8,
    "августа": 8,
    "авг": 8,
    "қыркүйек": 9,
    "кыркүйек": 9,
    "сентября": 9,
    "сен": 9,
    "сент": 9,
    "қазан": 10,
    "казан": 10,
    "октября": 10,
    "окт": 10,
    "қараша": 11,
    "караша": 11,
    "ноября": 11,
    "ноя": 11,
    "желтоқсан": 12,
    "желтоксан": 12,
    "декабря": 12,
    "дек": 12,
}


def resolve_day_in_current_or_next_month(day: int, *, today):
    for month_offset in range(13):
        month_index = today.month - 1 + month_offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        try:
            parsed = datetime(year, month, day).date()
        except ValueError:
            continue
        if parsed >= today:
            return parsed
    return None


def parse_full_calendar_date(text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None

    year_first_match = re.search(
        r"\b(\d{4})\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.|,)?\s*,?\s*(\d{1,2})\s+([^\W\d_]+)\b",
        normalized,
    )
    if year_first_match:
        year = int(year_first_match.group(1))
        day = int(year_first_match.group(2))
        month = MONTH_NAME_ALIASES.get(year_first_match.group(3))
        if month is None:
            return None
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    day_first_match = re.search(
        r"\b(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})(?:\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.))?\b",
        normalized,
    )
    if day_first_match:
        day = int(day_first_match.group(1))
        month = MONTH_NAME_ALIASES.get(day_first_match.group(2))
        year = int(day_first_match.group(3))
        if month is None:
            return None
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    return None


def parse_explicit_calendar_date(text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None

    today = timezone.localdate()
    full_date = parse_full_calendar_date(normalized)
    if full_date is not None:
        return full_date if full_date >= today else None

    weekday_date = parse_relative_weekday_date(normalized, today=today)
    if weekday_date is not None:
        return weekday_date

    dot_match = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", normalized)
    if dot_match:
        day = int(dot_match.group(1))
        month = int(dot_match.group(2))
        year = int(dot_match.group(3)) if dot_match.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            parsed = datetime(year, month, day).date()
        except ValueError:
            return None
        if parsed < today:
            try:
                parsed = datetime(year + 1, month, day).date()
            except ValueError:
                return None
        return parsed

    day_only_match = re.search(
        r"(?:\b(?:на|к|ко)\s+)?\b(\d{1,2})(?:\s*(?:число|числа)|-?го)\b",
        normalized,
    )
    if day_only_match:
        return resolve_day_in_current_or_next_month(
            int(day_only_match.group(1)),
            today=today,
        )

    day_with_part_of_day_match = re.search(
        r"\b(?:на|к|ко)\s+(\d{1,2})(?![:.\d])\s+(?:ближе\s+к\s+)?(?:вечер|утр|дн|кеш)",
        normalized,
    )
    if day_with_part_of_day_match:
        return resolve_day_in_current_or_next_month(
            int(day_with_part_of_day_match.group(1)),
            today=today,
        )

    word_match = re.search(r"\b(\d{1,2})\s+([^\W\d_]+)\b", normalized)
    if not word_match:
        return None

    day = int(word_match.group(1))
    month = MONTH_NAME_ALIASES.get(word_match.group(2))
    if month is None:
        return None

    try:
        parsed = datetime(today.year, month, day).date()
    except ValueError:
        return None
    if parsed < today:
        try:
            parsed = datetime(today.year + 1, month, day).date()
        except ValueError:
            return None
    return parsed


def detect_explicit_past_calendar_date(text: str) -> bool:
    parsed = parse_full_calendar_date(text)
    return parsed is not None and parsed < timezone.localdate()


WEEKDAY_NAME_ALIASES = {
    0: (
        "понедельник",
        "понедельника",
        "пн",
        "дүйсенбі",
        "дуйсенби",
        "дүйсенбіге",
        "дуйсенбиге",
    ),
    1: (
        "вторник",
        "вторника",
        "вт",
        "сейсенбі",
        "сейсенби",
        "сейсенбіге",
        "сейсенбиге",
    ),
    2: (
        "среда",
        "среду",
        "ср",
        "сәрсенбі",
        "сарсенби",
        "сәрсенбіге",
        "сарсенбиге",
    ),
    3: (
        "четверг",
        "четверга",
        "чт",
        "бейсенбі",
        "бейсенби",
        "бейсенбіге",
        "бейсенбиге",
    ),
    4: (
        "пятница",
        "пятницу",
        "пт",
        "жұма",
        "жума",
        "жұмаға",
        "жумага",
    ),
    5: (
        "суббота",
        "субботу",
        "сб",
        "сенбі",
        "сенби",
        "сенбіге",
        "сенбиге",
    ),
    6: (
        "воскресенье",
        "воскресения",
        "воскресенье",
        "вс",
        "жексенбі",
        "жексенби",
        "жексенбіге",
        "жексенбиге",
    ),
}


def parse_relative_weekday_date(text: str, *, today=None):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None
    today = today or timezone.localdate()
    for weekday, aliases in WEEKDAY_NAME_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized):
                days_ahead = (weekday - today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                return today + timedelta(days=days_ahead)
    return None


def infer_target_date_from_messages(*, texts: list[str], last_assistant_text: str):
    normalized_texts = [(text or "").strip().lower() for text in texts if (text or "").strip()]
    day_after_tomorrow_keywords = ("послезавтра", "бүрсігүні", "бурсикуни")
    tomorrow_keywords = ("завтра", "ертен", "ертең")
    today_keywords = ("сегодня", "бүгін", "бугин")

    for text in reversed(normalized_texts):
        if any(keyword in text for keyword in day_after_tomorrow_keywords):
            return timezone.localdate() + timedelta(days=2)
        if any(keyword in text for keyword in tomorrow_keywords):
            return timezone.localdate() + timedelta(days=1)
        if any(keyword in text for keyword in today_keywords):
            return timezone.localdate()

    assistant_text = (last_assistant_text or "").lower()
    if any(keyword in assistant_text for keyword in day_after_tomorrow_keywords):
        return timezone.localdate() + timedelta(days=2)
    if "завтра" in assistant_text or "ертең" in assistant_text or "ертен" in assistant_text:
        return timezone.localdate() + timedelta(days=1)
    return None


def parse_slot_choice(text: str, *, slots: list):
    normalized = (text or "").strip().lower()
    if not normalized or not slots:
        return None

    if normalized.isdigit():
        numeric_value = int(normalized)
        if 1 <= numeric_value <= min(len(slots), 9):
            return slots[numeric_value - 1]
        if len(normalized) == 4:
            hour = int(normalized[:2])
            minute = int(normalized[2:])
            for slot in slots:
                local_start = timezone.localtime(slot.start)
                if local_start.hour == hour and local_start.minute == minute:
                    return slot

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", normalized)
    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    for slot in slots:
        local_start = timezone.localtime(slot.start)
        if local_start.hour == hour and local_start.minute == minute:
            return slot
    return None


def extract_slot_time_preference(text: str):
    normalized = _repair_mojibake((text or "").strip().lower())
    if not normalized:
        return None

    time_text = re.sub(
        r"\b(?:на|к|ко)\s+\d{1,2}(?!\s*[:.])(?:\s*(?:число|числа)|-?го)?\b",
        " ",
        normalized,
    )
    time_text = re.sub(
        r"\b\d{4}\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.)?\s*,?\s*\d{1,2}\s+[^\W\d_]+\b",
        " ",
        time_text,
    )
    time_text = re.sub(
        r"\b\d{1,2}\s+[^\W\d_]+\s+\d{4}(?:\s*(?:года|год|г\.|РіРѕРґР°|РіРѕРґ|Рі\.))?\b",
        " ",
        time_text,
    )

    meridiem_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(вечера|вечером|вечере|кешке|кеш|утра|дня|днем|днём)\b",
        time_text,
    )
    if meridiem_match:
        hour = int(meridiem_match.group(1))
        minute = int(meridiem_match.group(2) or 0)
        period = meridiem_match.group(3)
        if period in {"вечера", "вечером", "вечере", "кешке", "кеш"} and hour < 12:
            hour += 12
        return {
            "kind": "exact",
            "hour": hour,
            "minute": minute,
        }

    if any(keyword in normalized for keyword in ("вечером", "вечер", "кешке", "кеш")):
        return {
            "kind": "range",
            "start": (17, 0),
            "end": (21, 0),
            "label_ru": "вечером",
            "label_kz": "кешке",
        }

    if any(keyword in normalized for keyword in ("попозже", "позже", "кейінірек", "кейинирек")):
        return {
            "kind": "later",
            "label_ru": "попозже",
            "label_kz": "кейінірек",
        }

    if any(keyword in normalized for keyword in ("утром", "с утра", "к утру", "танертен", "таңертең")):
        return {
            "kind": "range",
            "start": (8, 0),
            "end": (12, 0),
            "label_ru": "утром",
            "label_kz": "таңертең",
        }

    if any(
        keyword in normalized
        for keyword in ("днем", "днём", "после обеда", "тустен кейин", "түстен кейін")
    ):
        return {
            "kind": "range",
            "start": (12, 0),
            "end": (17, 0),
            "label_ru": "днем",
            "label_kz": "түстен кейін",
        }

    after_match = re.search(r"(?:после|кейін|кейин)\s*(\d{1,2})(?::?(\d{2}))?", time_text)
    if after_match:
        return {
            "kind": "from",
            "hour": int(after_match.group(1)),
            "minute": int(after_match.group(2) or 0),
        }

    compact_time_match = re.search(r"\b(\d{2})(\d{2})\b", time_text)
    if compact_time_match:
        hour = int(compact_time_match.group(1))
        minute = int(compact_time_match.group(2))
        if not (8 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return {
            "kind": "exact",
            "hour": hour,
            "minute": minute,
        }

    separated_time_matches = list(re.finditer(r"\b(\d{1,2})[.:](\d{2})\b", time_text))
    for separated_time_match in reversed(separated_time_matches):
        hour = int(separated_time_match.group(1))
        minute = int(separated_time_match.group(2))
        if 8 <= hour <= 23 and 0 <= minute <= 59:
            return {
                "kind": "exact",
                "hour": hour,
                "minute": minute,
            }

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", time_text)
    if not time_match:
        return None

    raw_match = time_match.group(0)
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)

    if time_text.strip().isdigit() and len(time_text.strip()) == 1:
        return None
    if ":" not in raw_match and time_match.group(2) is None and hour < 8:
        return None

    return {
        "kind": "exact",
        "hour": hour,
        "minute": minute,
    }


def filter_slots_by_time_preference(*, slots: list, preference: dict):
    if not slots or preference is None:
        return slots

    filtered = []
    for slot in slots:
        local_start = timezone.localtime(slot.start)
        slot_tuple = (local_start.hour, local_start.minute)
        if preference["kind"] == "range":
            if preference["start"] <= slot_tuple < preference["end"]:
                filtered.append(slot)
        elif preference["kind"] == "from":
            if slot_tuple >= (preference["hour"], preference["minute"]):
                filtered.append(slot)
        elif preference["kind"] == "exact":
            if slot_tuple == (preference["hour"], preference["minute"]):
                filtered.append(slot)
        elif preference["kind"] == "later":
            existing_slots = deserialize_session_slot_options(
                preference.get("slot_options", [])
            )
            if existing_slots:
                latest_existing = max(
                    (timezone.localtime(item.start).hour, timezone.localtime(item.start).minute)
                    for item in existing_slots
                )
                if slot_tuple > latest_existing:
                    filtered.append(slot)
    return filtered


def build_time_preference_unavailable_reply(*, service, preference: dict, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if preference["kind"] == "exact":
        time_label = f"{preference['hour']:02d}:{preference['minute']:02d}"
        if language == "kz":
            return f"{service_name} үшін {time_label}-ге бос уақыт көріп тұрғам жоқ."
        return f"На {time_label} по услуге «{service_name}» свободного времени не вижу."

    if preference["kind"] == "from":
        time_label = f"{preference['hour']:02d}:{preference['minute']:02d}"
        if language == "kz":
            return f"{service_name} үшін {time_label}-ден кейін бос уақыт көріп тұрғам жоқ."
        return f"После {time_label} по услуге «{service_name}» свободного времени не вижу."

    label = preference.get("label_kz") if language == "kz" else preference.get("label_ru")
    if language == "kz":
        return f"{service_name} үшін {label} бос уақыт көріп тұрғам жоқ."
    return f"На {label} по услуге «{service_name}» свободного времени не вижу."


def build_slot_options_reply(*, service, slots: list, language: str) -> str:
    top_slots = slots[:3]
    if not top_slots:
        if language == "kz":
            return (
                f"Әзірге {localize_service_name(service.name, language)} бойынша бос уақыт табылмады. "
                "Қаласаңыз, басқа күнді қарап беремін."
            )
        return (
            f"Пока не вижу свободных слотов на {localize_service_name(service.name, language)}. "
            "Если хотите, посмотрю другой день."
        )

    same_master = len({slot.master_id for slot in top_slots}) == 1
    slot_lines = []
    for index, slot in enumerate(top_slots, start=1):
        local_start = timezone.localtime(slot.start)
        if same_master:
            slot_lines.append(f"{index}. {local_start:%H:%M}")
        else:
            slot_lines.append(f"{index}. {local_start:%H:%M} — {slot.master_name}")
    slots_text = "\n".join(slot_lines)
    service_name = localize_service_name(service.name, language)
    master_name = top_slots[0].master_name
    actual_date = timezone.localtime(top_slots[0].start).date()
    date_label = format_local_date(actual_date, language=language)

    if language == "kz":
        if same_master:
            return (
                f"{date_label} күні {service_name} үшін {master_name} шеберінде мына уақыттар бос:\n\n"
                f"{slots_text}\n\nҚайсысы ыңғайлы?"
            )
        return (
            f"{date_label} күні {service_name} үшін мына бос уақыттар бар:\n\n"
            f"{slots_text}\n\nҚайсысы ыңғайлы?"
        )

    if same_master:
        return (
        f"На {date_label} есть такие варианты на {service_name} у мастера {master_name}:\n\n"
            f"{slots_text}\n\nКакой вариант вам удобнее?"
        )
    return (
        f"На {date_label} есть такие варианты на {service_name}:\n\n"
        f"{slots_text}\n\nКакой вариант вам удобнее?"
    )

    top_slots = slots[:3]
    if not top_slots:
        if language == "kz":
            return f"Әзірге {localize_service_name(service.name, language)} бойынша бос уақыт табылмады. Қаласаңыз, басқа күнді қарап беремін."
        return f"Пока не вижу свободных слотов на {localize_service_name(service.name, language)}. Если хотите, посмотрю другой день."

    same_master = len({slot.master_id for slot in top_slots}) == 1
    slot_lines = []
    for index, slot in enumerate(top_slots, start=1):
        local_start = timezone.localtime(slot.start)
        if same_master:
            slot_lines.append(f"{index}. {local_start:%H:%M}")
        else:
            slot_lines.append(f"{index}. {local_start:%H:%M} — {slot.master_name}")
    slots_text = "\n".join(slot_lines)
    service_name = localize_service_name(service.name, language)
    master_name = top_slots[0].master_name

    if language == "kz":
        if same_master:
            return (
            f"Ертең {service_name} үшін {master_name} шеберінде мына уақыттар бос:\n\n"
                f"{slots_text}\n\nҚайсысы ыңғайлы?"
            )
        return (
            f"Ертең {service_name} үшін мына бос уақыттар бар:\n\n"
            f"{slots_text}\n\nҚайсысы ыңғайлы?"
        )

    if same_master:
        return (
            f"На завтра для услуги «{service_name}» у мастера {master_name} есть такие варианты:\n\n"
            f"{slots_text}\n\nКакой вариант вам удобнее?"
        )
    return (
            f"На завтра для услуги «{service_name}» есть такие варианты:\n\n"
        f"{slots_text}\n\nКакой вариант вам удобнее?"
    )


def build_booking_confirmation_reply(*, service, slot, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    local_start = timezone.localtime(slot.start)
    if language == "kz":
        return (
            f"{service_name}: {local_start:%d %B}, {local_start:%H:%M}, шебер {slot.master_name}. Растайсыз ба?"
        )
    return (
        f"Записать на {service_name} {local_start:%d %B} в {local_start:%H:%M}, мастер {slot.master_name}?"
    )


def build_booking_created_reply(*, service_name: str, local_start: datetime, master_name: str, language: str) -> str:
    if language == "kz":
        return f"Жаздым: {service_name}, {local_start:%d %B} {local_start:%H:%M}, шебер {master_name}."
    return f"Записала: {service_name}, {local_start:%d %B} {local_start:%H:%M}, мастер {master_name}."


def build_existing_booking_reply(*, booking: Booking, language: str) -> str:
    local_start = timezone.localtime(booking.start_time)
    service_name = localize_service_name(booking.service.name, language)
    master_name = booking.master.full_name
    if booking.status == Booking.Status.CONFIRMED:
        if language == "kz":
            return (
                f"РРә, Р¶Р°Р·Р±Р° РЅР°Т›С‚С‹Р»Р°РґС‹: {service_name}, "
                f"{local_start:%d %B} {local_start:%H:%M}, С€РµР±РµСЂ {master_name}."
            )
        return (
            f"Р”Р°, Р·Р°РїРёСЃСЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°: {service_name}, "
            f"{local_start:%d %B} {local_start:%H:%M}, РјР°СЃС‚РµСЂ {master_name}."
        )
    if language == "kz":
        return (
            f"Р–Р°Р·Р±Р° У™Р»С– РЅР°Т›С‚С‹Р»РјР°Т“Р°РЅ: {service_name}, "
            f"{local_start:%d %B} {local_start:%H:%M}, С€РµР±РµСЂ {master_name}. "
            "Р Р°СЃС‚Р°Р№СЃС‹Р· Р±Р°?"
        )
    return (
        f"Р—Р°РїРёСЃСЊ РµС‰Рµ РЅРµ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°: {service_name}, "
        f"{local_start:%d %B} {local_start:%H:%M}, РјР°СЃС‚РµСЂ {master_name}. "
        "РџРѕРґС‚РІРµСЂРґРёС‚Рµ?"
    )


def build_date_selection_reply(*, service, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        return (
            f"{service_name} қызметіне жазылуға болады. "
            "Ертеңге қарайық па, әлде басқа күн керек пе?"
        )
    return (
        f"На {service_name} запишу. "
        "Смотрим на завтра или нужен другой день?"
    )


def build_booking_created_reply(*, service_name: str, local_start: datetime, master_name: str, language: str) -> str:
    if language == "kz":
        return (
            f"\u0416\u0430\u0437\u0434\u044b\u043c: {service_name}, "
            f"{local_start:%d %B} {local_start:%H:%M}, \u0448\u0435\u0431\u0435\u0440 {master_name}."
        )
    return (
        f"\u0417\u0430\u043f\u0438\u0441\u0430\u043b\u0430: {service_name}, "
        f"{local_start:%d %B} {local_start:%H:%M}, \u043c\u0430\u0441\u0442\u0435\u0440 {master_name}."
    )


def build_existing_booking_reply(*, booking: Booking, language: str) -> str:
    local_start = timezone.localtime(booking.start_time)
    service_name = localize_service_name(booking.service.name, language)
    master_name = booking.master.full_name
    if booking.status == Booking.Status.CONFIRMED:
        if language == "kz":
            return (
                f"\u0418\u04d9, \u0436\u0430\u0437\u0431\u0430 \u043d\u0430\u049b\u0442\u044b\u043b\u0430\u0434\u044b: {service_name}, "
                f"{local_start:%d %B} {local_start:%H:%M}, \u0448\u0435\u0431\u0435\u0440 {master_name}."
            )
        return (
            f"\u0414\u0430, \u0437\u0430\u043f\u0438\u0441\u044c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430: {service_name}, "
            f"{local_start:%d %B} {local_start:%H:%M}, \u043c\u0430\u0441\u0442\u0435\u0440 {master_name}."
        )
    if language == "kz":
        return (
            f"\u0416\u0430\u0437\u0431\u0430 \u04d9\u043b\u0456 \u043d\u0430\u049b\u0442\u044b\u043b\u0430\u043d\u0493\u0430\u043d \u0436\u043e\u049b: {service_name}, "
            f"{local_start:%d %B} {local_start:%H:%M}, \u0448\u0435\u0431\u0435\u0440 {master_name}. "
            "\u0420\u0430\u0441\u0442\u0430\u0439\u0441\u044b\u0437 \u0431\u0430?"
        )
    return (
        f"\u0417\u0430\u043f\u0438\u0441\u044c \u0435\u0449\u0435 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430: {service_name}, "
        f"{local_start:%d %B} {local_start:%H:%M}, \u043c\u0430\u0441\u0442\u0435\u0440 {master_name}. "
        "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435?"
    )


def build_booking_confirmation_reply(*, service, slot, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    local_start_label = format_local_datetime(slot.start, language=language)
    if language == "kz":
        return f"{service_name}: {local_start_label}, шебер {slot.master_name}. Растайсыз ба?"
    return f"Записать на «{service_name}» {local_start_label}, мастер {slot.master_name}?"


def build_date_selection_reply(*, service, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        return f"{service_name} қызметіне жазуға болады. Ертеңге қарайық па, әлде басқа күн керек пе?"
    return f"Ок, {service_name}. На завтра смотрим или нужен другой день?"


def build_booking_created_reply(*, service_name: str, local_start: datetime, master_name: str, language: str) -> str:
    local_start_label = format_local_datetime(local_start, language=language)
    if language == "kz":
        return f"Жаздым: {service_name}, {local_start_label}, шебер {master_name}."
    return f"Записала: {service_name}, {local_start_label}, мастер {master_name}."


def build_existing_booking_reply(*, booking: Booking, language: str) -> str:
    local_start_label = format_local_datetime(booking.start_time, language=language)
    service_name = localize_service_name(booking.service.name, language)
    master_name = booking.master.full_name
    if booking.status == Booking.Status.CONFIRMED:
        if language == "kz":
            return f"Иә, жазба нақтыланды: {service_name}, {local_start_label}, шебер {master_name}."
        return f"Да, запись подтверждена: «{service_name}», {local_start_label}, мастер {master_name}."
    if language == "kz":
        return f"Жазба әлі нақтыланбаған: {service_name}, {local_start_label}, шебер {master_name}. Растайсыз ба?"
    return f"Запись еще не подтверждена: «{service_name}», {local_start_label}, мастер {master_name}. Подтвердите?"


def last_assistant_message_targets_existing_booking(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    booking_markers = (
        "\u0437\u0430\u043f\u0438\u0441\u044c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430",
        "\u0437\u0430\u043f\u0438\u0441\u044c \u0435\u0449\u0435 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430",
        "\u0432\u0430\u0448 \u043c\u0430\u0441\u0442\u0435\u0440",
        "\u0432\u0430\u0448\u0430 \u0437\u0430\u043f\u0438\u0441\u044c",
        "\u0434\u0430, \u0437\u0430\u043f\u0438\u0441\u044c",
    )
    return any(marker in normalized for marker in booking_markers)


def parse_session_slot_choice(*, text: str, slot_options: list[dict]):
    lightweight_slots = deserialize_session_slot_options(slot_options)
    return parse_slot_choice(text, slots=lightweight_slots)


def deserialize_session_slot_options(slot_options: list[dict]):
    if not slot_options:
        return []

    lightweight_slots = []
    for option in slot_options:
        start_time_raw = option.get("start_time")
        if not start_time_raw:
            continue
        start_time = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
        end_time_raw = option.get("end_time") or start_time_raw
        end_time = datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
        lightweight_slots.append(
            SimpleNamespace(
                start=start_time,
                end=end_time,
                master_id=option.get("master_id"),
                master_name=option.get("master_name", ""),
            )
        )
    return lightweight_slots


def find_next_available_slots(*, business: Business, service, target_date, max_days: int = 14):
    fallback_date = target_date
    for offset in range(max_days):
        candidate_date = target_date + timedelta(days=offset)
        slots = get_available_slots(
            business,
            target_date=candidate_date,
            service_id=service.id,
        )
        if slots:
            return candidate_date, slots
    return fallback_date, []


def process_incoming_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    text: str,
    ai_manager: AIManager | None = None,
    persist_user_message: bool = True,
):
    normalized_text = (text or "").strip()
    if not normalized_text:
        raise ValidationError("Message text is required.")

    business = get_business(business_id=business_id)
    ai_manager = ai_manager or AIManager(business=business, client=client)
    enforce_client_rate_limit(
        business_id=business_id,
        client=client,
        channel=channel,
    )

    if persist_user_message:
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.USER,
            content=normalized_text,
        )

    thread = get_or_create_conversation_thread(
        business=business,
        client=client,
        channel=channel,
    )
    if not is_bot_active(thread):
        return {"reply": "", "escalated": False, "bot_paused": True}


    preferred_language = detect_client_language(
        ai_manager=ai_manager,
        business_id=business_id,
        client=client,
        channel=channel,
        current_text="" if persist_user_message else normalized_text,
    )

    if process_opt_out(client=client, text=normalized_text):
        reply = "Хорошо, больше не буду присылать напоминания по этой записи."
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    last_assistant_scope_text = (
        ConversationMessage.objects.filter(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
        )
        .order_by("-created_at", "-id")
        .values_list("content", flat=True)
        .first()
        or ""
    )
    if detect_out_of_scope_request(normalized_text) or detect_out_of_scope_followup_pressure(
        text=normalized_text,
        last_assistant_text=last_assistant_scope_text,
    ):
        reply = build_out_of_scope_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_service_catalog_request(normalized_text):
        reply = build_service_catalog_reply(
            business=business,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    booking = get_latest_active_booking(business_id=business_id, client=client)
    session = get_or_create_booking_session(
        business=business,
        client=client,
        channel=channel,
    )
    if detect_greeting_message(normalized_text):
        clear_booking_session(session)
        reply = build_greeting_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    post_booking_context_limited = should_limit_post_booking_context(
        session=session,
        booking=booking,
        text=normalized_text,
    )
    conversation_context = build_conversation_context(
        business_id=business_id,
        client=client,
        channel=channel,
        max_messages=POST_BOOKING_CONTEXT_MESSAGE_LIMIT if post_booking_context_limited else None,
    )
    recent_texts = [
        item["content"]
        for item in conversation_context
        if item.get("role") == ConversationMessage.Role.USER
    ]
    recent_assistant_texts = [
        item["content"]
        for item in conversation_context
        if item.get("role") == ConversationMessage.Role.ASSISTANT
    ]
    last_assistant_text = recent_assistant_texts[-1] if recent_assistant_texts else ""

    if detect_generic_haircut_request(business=business, text=normalized_text) and (
        session.service_id is None or is_haircut_service(session.service)
    ) and not detect_non_booking_service_question(normalized_text):
        clear_booking_session(session)
        reply = build_haircut_clarification_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    explicit_service = infer_service_from_messages(
        business=business,
        texts=[normalized_text],
    )
    explicit_booking_intent = detect_explicit_booking_intent(normalized_text)
    non_booking_service_question = (
        explicit_service is not None
        and detect_non_booking_service_question(normalized_text)
    )
    faq_context_service = (
        explicit_service
        or session.service
        or (booking.service if booking is not None else None)
        or infer_service_from_messages(
            business=business,
            texts=recent_texts,
        )
    )

    if detect_hours_request(normalized_text):
        reply = build_working_hours_reply(
            business=business,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_price_request(normalized_text) or (
        explicit_service is not None and is_price_clarification_prompt(last_assistant_text)
    ):
        if faq_context_service is not None:
            reply = build_service_price_reply(
                service=faq_context_service,
                language=preferred_language,
            )
        else:
            reply = build_price_clarification_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_gratitude_message(normalized_text):
        reply = build_gratitude_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if (
        booking is not None
        and session.state == BookingSession.State.IDLE
        and is_brief_post_booking_follow_up(normalized_text)
        and (
            not last_assistant_text
            or last_assistant_message_targets_existing_booking(last_assistant_text)
        )
    ):
        reply = build_existing_booking_reply(
            booking=booking,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    service_switched = (
        explicit_service is not None
        and session.service_id != explicit_service.id
        and not non_booking_service_question
        and (explicit_booking_intent or session.state != BookingSession.State.IDLE)
    )
    if service_switched:
        set_session_service(
            session,
            service=explicit_service,
            language=preferred_language,
        )

    history_texts_for_current_service = [] if service_switched else recent_texts
    history_last_assistant_text = "" if service_switched else last_assistant_text

    inferred_service = explicit_service or session.service or infer_service_from_messages(
        business=business,
        texts=history_texts_for_current_service,
    )
    current_message_target_date = parse_explicit_calendar_date(normalized_text) or infer_target_date_from_messages(
        texts=[normalized_text],
        last_assistant_text="",
    )
    if detect_explicit_past_calendar_date(normalized_text):
        reply = "Эта дата уже прошла. Напишите актуальную дату и время, например: 12 мая в 18:00."
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    inferred_target_date = session.target_date or current_message_target_date or infer_target_date_from_messages(
        texts=history_texts_for_current_service,
        last_assistant_text=history_last_assistant_text,
    )
    assistant_requests_confirmation = (not service_switched) and any(
        keyword in last_assistant_text.lower()
        for keyword in ("подтверж", "подтверд", "раста")
    )
    assistant_offered_tomorrow = (not service_switched) and any(
        keyword in last_assistant_text.lower()
        for keyword in ("завтра", "ертең", "ертен")
    )

    if (
        session.state == BookingSession.State.IDLE
        and booking is None
        and inferred_service is None
        and explicit_booking_intent
        and not detect_cancellation_request(normalized_text)
    ):
        reply = build_booking_intent_clarification_reply(language=preferred_language)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    mentioned_master = find_mentioned_master(business=business, text=normalized_text)

    if mentioned_master is not None and detect_master_opinion_request(normalized_text):
        opinion_service = session.service or (booking.service if booking is not None else None)
        if opinion_service is not None:
            service_masters = get_service_recommended_masters(
                business=business,
                service=opinion_service,
            )
            if mentioned_master.id in {item.id for item in service_masters}:
                reply = build_master_opinion_reply(
                    master=mentioned_master,
                    service=opinion_service,
                    language=preferred_language,
                )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}
        reply = build_master_opinion_reply(
            master=mentioned_master,
            service=None,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if session.service_id and mentioned_master is not None:
        service_masters = get_service_recommended_masters(
            business=business,
            service=session.service,
        )
        service_master_ids = {item.id for item in service_masters}
        if session.master_id and mentioned_master.id == session.master_id:
            reply = build_session_master_match_reply(
                session=session,
                mentioned_master=mentioned_master,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if mentioned_master.id not in service_master_ids:
            reply = build_session_master_mismatch_reply(
                session=session,
                mentioned_master=mentioned_master,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if detect_master_list_request(normalized_text):
        if session.service_id:
            reply = build_current_session_master_reply(
                session=session,
                language=preferred_language,
            )
        elif inferred_service is not None:
            reply = build_service_master_options_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=inferred_service,
            )
        else:
            reply = build_master_list_reply(
                business=business,
                language=preferred_language,
            )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if detect_cancellation_request(normalized_text):
        if booking is not None:
            handoff_response = request_human_handoff(
                booking=booking,
                reason="Client requested cancellation",
                attempts=client.ai_failure_count,
                language=preferred_language,
            )
            assistant_reply = build_cancellation_reply(language=preferred_language)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=assistant_reply,
            )
            return {
                "reply": assistant_reply,
                "escalated": handoff_response["escalated"],
            }

    if detect_master_recommendation_request(normalized_text):
        if session.service_id:
            reply = build_service_master_options_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=session.service,
            )
        elif inferred_service is not None:
            reply = build_service_master_options_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=inferred_service,
            )
        else:
            reply = build_master_recommendation_reply(
                business=business,
                language=preferred_language,
                texts=recent_texts,
                service=inferred_service,
            )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if (
        session.state == BookingSession.State.IDLE
        and inferred_service is not None
        and not non_booking_service_question
    ):
        if current_message_target_date is not None:
            set_session_service(
                session,
                service=inferred_service,
                language=preferred_language,
            )
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=inferred_service,
                target_date=current_message_target_date,
            )
            time_preference = extract_slot_time_preference(normalized_text)
            if time_preference is not None and time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            preferred_slots = filter_slots_by_time_preference(
                slots=slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or slots
            set_session_slot_options(
                session,
                service=inferred_service,
                target_date=resolved_target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if time_preference is not None and not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=inferred_service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=inferred_service, slots=slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=inferred_service,
                    slots=slots_for_reply,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if explicit_service is not None:
            set_session_service(
                session,
                service=inferred_service,
                language=preferred_language,
            )
            reply = build_date_selection_reply(
                service=inferred_service,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if (
            is_affirmative_message(normalized_text)
            and assistant_requests_confirmation
            and inferred_target_date is not None
        ):
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=inferred_service,
                target_date=inferred_target_date,
            )
            prior_slot = None
            for candidate_text in reversed(recent_texts):
                prior_slot = parse_slot_choice(candidate_text, slots=slots[:3])
                if prior_slot is not None:
                    break
            if prior_slot is not None:
                selected_master = business.masters.get(
                    pk=prior_slot.master_id,
                    is_active=True,
                )
                booking_record = create_appointment(
                    business=business,
                    master=selected_master,
                    service=inferred_service,
                    client=client,
                    start_time=prior_slot.start,
                    status=Booking.Status.CONFIRMED,
                    client_data={
                        "name": client.name or "",
                        "phone": str(client.phone or ""),
                        "whatsapp_id": client.whatsapp_id or "",
                        "telegram_id": client.telegram_id or "",
                    },
                )
                local_start = timezone.localtime(booking_record.start_time)
                service_name = localize_service_name(inferred_service.name, preferred_language)
                if preferred_language == "kz":
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                else:
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}

        if is_affirmative_message(normalized_text) and (assistant_offered_tomorrow or inferred_target_date is not None):
            target_date = inferred_target_date or (timezone.localdate() + timedelta(days=1))
            set_session_service(
                session,
                service=inferred_service,
                language=preferred_language,
            )
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=inferred_service,
                target_date=target_date,
            )
            set_session_slot_options(
                session,
                service=inferred_service,
                target_date=resolved_target_date,
                slots=slots[:3],
                language=preferred_language,
            )
            reply = build_slot_options_reply(
                service=inferred_service,
                slots=slots,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if (
        session.state == BookingSession.State.AWAITING_CONFIRMATION
        and session.service_id
        and session.master_id
        and session.selected_start_time is not None
        and session.selected_end_time is not None
        and is_affirmative_message(normalized_text)
    ):
        booking_record = create_appointment(
            business=business,
            master=session.master,
            service=session.service,
            client=client,
            start_time=session.selected_start_time,
            status=Booking.Status.CONFIRMED,
            client_data={
                "name": client.name or "",
                "phone": str(client.phone or ""),
                "whatsapp_id": client.whatsapp_id or "",
                "telegram_id": client.telegram_id or "",
            },
        )
        local_start = timezone.localtime(booking_record.start_time)
        service_name = localize_service_name(session.service.name, preferred_language)
        if preferred_language == "kz":
            reply = build_booking_created_reply(
                service_name=service_name,
                local_start=local_start,
                master_name=booking_record.master.full_name,
                language=preferred_language,
            )
        else:
            reply = build_booking_created_reply(
                service_name=service_name,
                local_start=local_start,
                master_name=booking_record.master.full_name,
                language=preferred_language,
            )
        clear_booking_session(session)
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if session.state == BookingSession.State.AWAITING_SLOT_CHOICE and session.service_id:
        if current_message_target_date is not None:
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=session.service,
                target_date=current_message_target_date,
            )
            time_preference = extract_slot_time_preference(normalized_text)
            if time_preference is not None and time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            preferred_slots = filter_slots_by_time_preference(
                slots=slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or slots
            set_session_slot_options(
                session,
                service=session.service,
                target_date=resolved_target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if time_preference is not None and not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=session.service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=session.service, slots=slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=session.service,
                    slots=slots_for_reply,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        selected_slot = parse_session_slot_choice(
            text=normalized_text,
            slot_options=session.slot_options,
        )
        unknown_master_candidate = extract_unmatched_master_candidate(
            business=business,
            text=normalized_text,
        )
        if selected_slot is not None and unknown_master_candidate is not None:
            reply = build_unknown_master_reply(
                service=session.service,
                candidate=unknown_master_candidate,
                actual_master_name=selected_slot.master_name,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if selected_slot is not None:
            selected_master = business.masters.get(
                pk=selected_slot.master_id,
                is_active=True,
            )
            booking_record = create_appointment(
                business=business,
                master=selected_master,
                service=session.service,
                client=client,
                start_time=selected_slot.start,
                status=Booking.Status.CONFIRMED,
                client_data={
                    "name": client.name or "",
                    "phone": str(client.phone or ""),
                    "whatsapp_id": client.whatsapp_id or "",
                    "telegram_id": client.telegram_id or "",
                },
            )
            local_start = timezone.localtime(booking_record.start_time)
            service_name = localize_service_name(session.service.name, preferred_language)
            reply = build_booking_created_reply(
                service_name=service_name,
                local_start=local_start,
                master_name=booking_record.master.full_name,
                language=preferred_language,
            )
            clear_booking_session(session)
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        time_preference = extract_slot_time_preference(normalized_text)
        if time_preference is not None and session.target_date is not None:
            if time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            all_slots = get_available_slots(
                business,
                target_date=session.target_date,
                service_id=session.service.id,
            )
            preferred_slots = filter_slots_by_time_preference(
                slots=all_slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or all_slots
            set_session_slot_options(
                session,
                service=session.service,
                target_date=session.target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=session.service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if all_slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=session.service, slots=all_slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=session.service,
                    slots=preferred_slots,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if session.state == BookingSession.State.AWAITING_DATE and session.service_id:
        target_date = current_message_target_date
        if target_date is None and extract_slot_time_preference(normalized_text) is not None:
            reply = "Время понял. На какой день записать?"
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}
        if target_date is None and is_unspecified_other_day_request(normalized_text):
            reply = "Какой день удобен? Например: среда, пятница или 12 мая."
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if target_date is None and has_affirmative_signal(normalized_text):
            target_date = timezone.localdate() + timedelta(days=1)

        if target_date is not None:
            resolved_target_date, slots = find_next_available_slots(
                business=business,
                service=session.service,
                target_date=target_date,
            )
            time_preference = extract_slot_time_preference(normalized_text)
            if time_preference is not None and time_preference.get("kind") == "later":
                time_preference["slot_options"] = session.slot_options
            preferred_slots = filter_slots_by_time_preference(
                slots=slots,
                preference=time_preference,
            )
            slots_for_reply = preferred_slots or slots
            set_session_slot_options(
                session,
                service=session.service,
                target_date=resolved_target_date,
                slots=slots_for_reply[:3],
                language=preferred_language,
            )
            if time_preference is not None and not preferred_slots:
                base_reply = build_time_preference_unavailable_reply(
                    service=session.service,
                    preference=time_preference,
                    language=preferred_language,
                )
                if slots:
                    reply = (
                        f"{base_reply}\n\n"
                        f"{build_slot_options_reply(service=session.service, slots=slots, language=preferred_language)}"
                    )
                else:
                    reply = base_reply
            else:
                reply = build_slot_options_reply(
                    service=session.service,
                    slots=slots_for_reply,
                    language=preferred_language,
                )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if (
            explicit_service is not None
            and explicit_service.id == session.service_id
            and not non_booking_service_question
        ):
            reply = build_date_selection_reply(
                service=session.service,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if False and inferred_service is not None and inferred_target_date is not None:
        slots = get_available_slots(
            business,
            target_date=inferred_target_date,
            service_id=inferred_service.id,
        )
        assistant_requests_confirmation = any(
            keyword in last_assistant_text.lower()
            for keyword in ("подтверж", "подтверд", "раста")
        )
        selected_slot = parse_slot_choice(normalized_text, slots=slots[:3])
        if selected_slot is not None:
            reply = build_booking_confirmation_reply(
                service=inferred_service,
                slot=selected_slot,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

        if (
            is_affirmative_message(normalized_text)
        ):
            prior_user_texts = recent_texts[:-1] if recent_texts else []
            prior_slot = None
            for candidate_text in reversed(prior_user_texts):
                prior_slot = parse_slot_choice(candidate_text, slots=slots[:3])
                if prior_slot is not None:
                    break
            if assistant_requests_confirmation and prior_slot is not None:
                booking_record = create_appointment(
                    business=business,
                    master=business.masters.get(pk=prior_slot.master_id, is_active=True),
                    service=inferred_service,
                    client=client,
                    start_time=prior_slot.start,
                    status=Booking.Status.CONFIRMED,
                    client_data={
                        "name": client.name or "",
                        "phone": str(client.phone or ""),
                        "whatsapp_id": client.whatsapp_id or "",
                        "telegram_id": client.telegram_id or "",
                    },
                )
                local_start = timezone.localtime(booking_record.start_time)
                service_name = localize_service_name(inferred_service.name, preferred_language)
                if preferred_language == "kz":
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                else:
                    reply = build_booking_created_reply(
                        service_name=service_name,
                        local_start=local_start,
                        master_name=booking_record.master.full_name,
                        language=preferred_language,
                    )
                store_message(
                    business_id=business_id,
                    client=client,
                    channel=channel,
                    role=ConversationMessage.Role.ASSISTANT,
                    content=reply,
                )
                return {"reply": reply, "escalated": False}

        if (
            is_affirmative_message(normalized_text)
            and any(keyword in last_assistant_text.lower() for keyword in ("завтра", "ертең", "ертен"))
            and not assistant_requests_confirmation
        ):
            reply = build_slot_options_reply(
                service=inferred_service,
                slots=slots,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    requested_human = ai_manager.detect_human_request(normalized_text)
    should_auto_escalate = requested_human or (
        booking is not None
        and ai_manager.should_escalate(
            requested_human=False,
            failed_attempts=client.ai_failure_count,
        )
    )
    if should_auto_escalate:
        handoff_response = request_human_handoff(
            booking=booking,
            reason="Client requested a human operator",
            attempts=client.ai_failure_count,
            language=preferred_language,
        )
        assistant_reply = handoff_response["reply"]
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=assistant_reply,
        )
        return handoff_response

    if session.state == BookingSession.State.AWAITING_DATE and session.service_id:
        reply = build_date_selection_reply(
            service=session.service,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    if session.state == BookingSession.State.AWAITING_SLOT_CHOICE and session.service_id:
        current_slots = deserialize_session_slot_options(session.slot_options)
        if current_slots:
            reply = build_slot_options_reply(
                service=session.service,
                slots=current_slots,
                language=preferred_language,
            )
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=reply,
            )
            return {"reply": reply, "escalated": False}

    if (
        session.state == BookingSession.State.AWAITING_CONFIRMATION
        and session.service_id
        and session.master_id
        and session.selected_start_time is not None
    ):
        confirmation_slot = SimpleNamespace(
            start=session.selected_start_time,
            master_name=session.master.full_name,
        )
        reply = build_booking_confirmation_reply(
            service=session.service,
            slot=confirmation_slot,
            language=preferred_language,
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "escalated": False}

    logger.info(
        "ai_fallback_entered",
        extra={
            "business_id": business_id,
            "client_id": client.id,
            "channel": channel,
            "session_state": session.state,
            "session_service_id": session.service_id,
            "session_master_id": session.master_id,
            "session_target_date": session.target_date.isoformat() if session.target_date else "",
            "booking_id": booking.id if booking is not None else None,
            "text": (normalized_text or "")[:120],
        },
    )
    try:
        reply = ai_manager.generate_reply(
            conversation_context
        )
    except Exception:
        logger.exception(
            "ai_reply_failed",
            extra={
                "business_id": business_id,
                "client_id": client.id,
                "channel": channel,
                "session_state": session.state,
                "session_service_id": session.service_id,
                "session_master_id": session.master_id,
                "session_target_date": session.target_date.isoformat() if session.target_date else "",
                "booking_id": booking.id if booking is not None else None,
                "text": (normalized_text or "")[:120],
            },
        )
        client.ai_failure_count += 1
        client.save(update_fields=["ai_failure_count", "updated_at"])
        if booking is not None and ai_manager.should_escalate(
            requested_human=False,
            failed_attempts=client.ai_failure_count,
        ):
            handoff_response = request_human_handoff(
                booking=booking,
                reason="AI failed to answer three times in a row",
                attempts=client.ai_failure_count,
                language=preferred_language,
            )
            assistant_reply = handoff_response["reply"]
            store_message(
                business_id=business_id,
                client=client,
                channel=channel,
                role=ConversationMessage.Role.ASSISTANT,
                content=assistant_reply,
            )
            return handoff_response

        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=get_localized_runtime_message("ai_retry", preferred_language),
        )
        return {
            "reply": get_localized_runtime_message("ai_retry", preferred_language),
            "escalated": False,
        }

    if client.ai_failure_count:
        client.ai_failure_count = 0
        client.save(update_fields=["ai_failure_count", "updated_at"])

    store_message(
        business_id=business_id,
        client=client,
        channel=channel,
        role=ConversationMessage.Role.ASSISTANT,
        content=reply,
    )
    return {"reply": reply, "escalated": False}


def handle_text_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    text: str,
    ai_manager: AIManager | None = None,
    persist_user_message: bool = True,
):
    return process_incoming_message(
        business_id=business_id,
        channel=channel,
        client=client,
        text=text,
        ai_manager=ai_manager,
        persist_user_message=persist_user_message,
    )


def handle_audio_message(
    *,
    business_id: int,
    channel: str,
    client: Client,
    audio_file,
    ai_manager: AIManager | None = None,
):
    max_audio_bytes = settings.MAX_VOICE_FILE_SIZE_BYTES
    file_size = getattr(audio_file, "size", 0)
    if file_size and file_size > max_audio_bytes:
        raise ValidationError("Audio file is too large.")

    business = get_business(business_id=business_id)
    ai_manager = ai_manager or AIManager(business=business, client=client)
    transcript = ai_manager.handle_voice_message(file_obj=audio_file)
    if transcript == VOICE_FALLBACK_MESSAGE:
        reply = get_localized_runtime_message(
            "voice_fallback",
            detect_client_language(
                ai_manager=ai_manager,
                business_id=business_id,
                client=client,
                channel=channel,
            ),
        )
        store_message(
            business_id=business_id,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=reply,
        )
        return {"reply": reply, "transcript": None, "escalated": False}

    response = handle_text_message(
        business_id=business_id,
        channel=channel,
        client=client,
        text=transcript,
        ai_manager=ai_manager,
        persist_user_message=True,
    )
    response["transcript"] = transcript
    return response


# Final human-style reply overrides. These are intentionally placed at the end
# of the module so they override earlier duplicated builder definitions without
# affecting booking/state logic.
def build_master_recommendation_reply(*, business: Business, language: str, texts: list[str], service=None) -> str:
    service = service or infer_service_from_messages(business=business, texts=texts)
    if service is None:
        return build_master_list_reply(business=business, language=language)

    masters = get_service_recommended_masters(business=business, service=service)
    if not masters:
        return build_master_list_reply(business=business, language=language)

    recommended_master = masters[0]
    service_name = localize_service_name(service.name, language)
    if language == "kz":
        return f"{service_name} үшін шебер: {recommended_master.full_name}. Бағыты — {recommended_master.specialization.lower()}."
    return f"Для услуги «{service_name}» подойдет мастер {recommended_master.full_name}. Направление — {recommended_master.specialization.lower()}."


def build_service_master_options_reply(*, business: Business, language: str, texts: list[str], service=None) -> str:
    service = service or infer_service_from_messages(business=business, texts=texts)
    if service is None:
        return build_master_list_reply(business=business, language=language)

    masters = get_service_recommended_masters(business=business, service=service)
    if not masters:
        return build_master_list_reply(business=business, language=language)

    service_name = localize_service_name(service.name, language)
    if language == "kz":
        if len(masters) == 1:
            master = masters[0]
            return f"{service_name} үшін шебер: {master.full_name}. Бағыты — {master.specialization.lower()}."
        master_lines = "; ".join(f"{master.full_name} — {master.specialization}" for master in masters[:4])
        return f"{service_name} үшін мына шеберлер бар: {master_lines}."

    if len(masters) == 1:
        master = masters[0]
        return f"На услугу «{service_name}» работает мастер {master.full_name}. Направление — {master.specialization.lower()}."

    master_lines = "; ".join(f"{master.full_name} — {master.specialization}" for master in masters[:4])
    return f"На услугу «{service_name}» работают: {master_lines}."


def build_service_catalog_reply(*, business: Business, language: str) -> str:
    services = list(
        business.services.filter(is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )
    if not services:
        if language == "kz":
            return "Қазір нақты қызмет тізімі бос тұр."
        return "Сейчас не вижу активный список услуг."

    lines = []
    for service in islice(services, 0, 6):
        service_name = localize_service_name(service.name, language)
        price_value = int(service.price)
        duration_minutes = int((service.duration or timedelta()).total_seconds() // 60)
        if duration_minutes > 0:
            lines.append(f"- {service_name} — {price_value} тг, {duration_minutes} мин")
        else:
            lines.append(f"- {service_name} — {price_value} тг")

    if language == "kz":
        return "Мына қызметтер бар:\n" + "\n".join(lines)
    return "Вот что есть:\n" + "\n".join(lines)


def build_master_list_reply(*, business: Business, language: str) -> str:
    masters = list(
        business.masters.filter(is_active=True).order_by("full_name").values_list(
            "full_name",
            "specialization",
        )
    )
    if not masters:
        if language == "kz":
            return "Қазір белсенді шеберлер тізімі бос."
        return "Сейчас активных мастеров в списке нет."

    master_lines = "; ".join(f"{name} — {specialization}" for name, specialization in masters[:4])
    if language == "kz":
        return f"Қазір жұмыс істейтін шеберлер: {master_lines}."
    return f"Сейчас работают: {master_lines}."


def build_service_price_reply(*, service, language: str) -> str:
    service_name = localize_service_name(service.name, language)
    duration_minutes = int((service.duration or timedelta()).total_seconds() // 60)
    price_value = int(service.price)
    if language == "kz":
        if duration_minutes > 0:
            return f"«{service_name}» бағасы {price_value} тг. Ұзақтығы шамамен {duration_minutes} минут."
        return f"«{service_name}» бағасы {price_value} тг."
    if duration_minutes > 0:
        return f"«{service_name}» стоит {price_value} тг. По времени — около {duration_minutes} минут."
    return f"«{service_name}» стоит {price_value} тг."


def build_price_clarification_reply(*, language: str) -> str:
    if language == "kz":
        return "Қай қызметтің бағасы керек екенін жазыңыз."
    return "Напишите, пожалуйста, какая услуга интересует."


def build_working_hours_reply(*, business: Business, language: str) -> str:
    hours_text = (business.working_hours or "").strip()
    if not hours_text:
        if language == "kz":
            return "Жұмыс уақытын қазір нақты айта алмаймын."
        return "Сейчас не вижу точный график работы."

    if "-" in hours_text:
        start_time, end_time = [part.strip() for part in hours_text.split("-", 1)]
        if language == "kz":
            return f"Бүгін {start_time}-ден {end_time}-ге дейін жұмыс істейміз."
        return f"Сегодня работаем с {start_time} до {end_time}."

    if language == "kz":
        return f"Жұмыс уақыты: {hours_text}."
    return f"График работы: {hours_text}."
