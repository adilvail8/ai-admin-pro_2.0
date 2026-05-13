import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from openai import OpenAI

from .ai import PromptBuilder
from .models import AIInteractionLog, Booking, Business
from .services import OPENAI_FUNCTION_DEFINITIONS, execute_ai_function


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — интеллектуальный администратор бизнеса в Казахстане. "
    "Отвечай коротко, вежливо и только по данным из контекста."
)
VOICE_FALLBACK_MESSAGE = (
    "Кешіріңіз, мен әзірге дауыстық хабарламаларды түсінбеймін, "
    "өтініш, мәтінмен жазыңыз."
)
HUMAN_HANDOFF_MESSAGE = (
    "Сейчас подключу администратора."
)
AI_RETRY_MESSAGE = (
    "Извините, я сейчас обновляюсь, напишите через 5 минут "
    "или позвоните администратору."
)
DEFAULT_FOLLOW_UP_TEMPLATE = (
    '"{client_name}", заметила, что мы не завершили запись на '
    "[{service_name}]. Подсказать что-нибудь по процедуре или "
    'забронировать это время за вами? Если неактуально, просто напишите "стоп".'
)
DEFAULT_REMINDER_TEMPLATE = (
    "Сәлем! Ждем вас сегодня в {time} на услугу {service_name}. "
    "Если планы изменились, пожалуйста, предупредите нас заранее ✨"
)
ESCALATION_KEYWORDS = (
    "администратор",
    "оператор",
    "человек",
    "жалоба",
    "позовите",
    "позови",
    "соедините",
)


class AIManager:
    RUSSIAN_LANGUAGE_MARKERS = (
        "на русском",
        "по-русски",
        "по русски",
        "русском языке",
        "русский язык",
        "орысша",
        "русша",
    )
    RUSSIAN_PHRASE_MARKERS = (
        "здравствуйте",
        "здрасьте",
        "здрасте",
        "добрый день",
        "добрый вечер",
        "доброе утро",
        "привет",
        "подскажите",
        "сколько стоит",
        "вы еще работаете",
        "на русском",
        "по-русски",
    )
    KAZAKH_LANGUAGE_MARKERS = (
        "қазақша",
        "казакша",
        "на казахском",
        "на казахском языке",
        "казахском языке",
    )
    KAZAKH_SPECIFIC_LETTERS = set("әіңғүұқөһі")
    KAZAKH_PHRASE_MARKERS = (
        "саламатсыз",
        "саламалейкум",
        "салем",
        "салемет",
        "калайсыз",
        "калай",
        "ертен",
        "коремиз",
        "коремыз",
        "жарайды",
        "рахмет",
        "казакша",
        "сойле",
        "сөйле",
        "бола ма",
        "керек",
        "маган",
        "сизге",
        "кызмет",
        "жазыл",
    )
    def __init__(
        self,
        *,
        business: Business | None = None,
        client=None,
        openai_client=None,
        model: str | None = None,
    ):
        self.business = business
        self.client = client if not self._looks_like_openai_client(client) else None
        self.openai_client = (
            openai_client
            if openai_client is not None
            else client if self._looks_like_openai_client(client) else None
        )
        self.model = model or settings.OPENAI_MODEL
        self.prompt_builder = PromptBuilder()

    @staticmethod
    def _looks_like_openai_client(candidate) -> bool:
        if candidate is None:
            return False
        return hasattr(candidate, "chat") or hasattr(candidate, "audio")

    def get_openai_client(self):
        if self.openai_client is not None:
            return self.openai_client
        if not settings.OPENAI_API_KEY:
            return None
        return OpenAI(api_key=settings.OPENAI_API_KEY)

    def build_system_instruction(self) -> str:
        if self.business is None:
            return "\n\n".join([SYSTEM_PROMPT, self.prompt_builder.build_fallback_prompt()])
        return self.prompt_builder.build_system_prompt(self.business)

    def build_response_policy_instruction(self) -> str:
        return (
            "Follow these rules strictly:\n"
            "- Use only facts that already exist in the provided context or tool results.\n"
            "- Never invent masters, services, prices, addresses, booking statuses, free slots, durations, or availability.\n"
            "- If a fact is missing or uncertain, say so plainly and ask a short clarifying question.\n"
            "- If the user names a master that is not in the salon data, do not pretend that this master exists.\n"
            "- Do not confirm a booking unless the booking is already present in tool results or deterministic system data.\n"
            "- Stay within salon topics only: services, masters, prices, schedule, booking, reschedule, cancellation.\n"
            "- If the message is off-topic, briefly say that you can help only with salon services and booking.\n"
            "- Reply like a human salon administrator in messenger: short, clear, calm, natural.\n"
            "- Prefer 1-2 short sentences. No emojis. No long explanations. No generic filler."
        )

    def infer_response_language(self, conversation_messages) -> str:
        last_user_content = ""
        for message in reversed(conversation_messages):
            if message.get("role") == "user":
                last_user_content = (message.get("content") or "").strip().lower()
                break

        if not last_user_content:
            return "ru"

        if "на русском" in last_user_content or "по-русски" in last_user_content:
            return "ru"
        if "қазақша" in last_user_content or "на казахском" in last_user_content:
            return "kz"

        kazakh_letters = set("әіңғүұқөһі")
        if any(letter in last_user_content for letter in kazakh_letters):
            return "kz"
        return "ru"

    def summarize_conversation(self, history):
        """Scaffold for future LLM-based conversation summarization."""
        if not history:
            return ""

        compressed_fragments = []
        for item in history[:7]:
            role = item.get("role", "user")
            content = (item.get("content") or "").strip()
            if content:
                compressed_fragments.append(f"{role}: {content[:120]}")
        return "Краткое резюме предыдущего диалога: " + " | ".join(
            compressed_fragments
        )

    def prepare_conversation_messages(self, conversation_messages):
        if len(conversation_messages) <= 10:
            return conversation_messages, ""

        summary = self.summarize_conversation(conversation_messages[:-3])
        prepared_messages = []
        if summary:
            prepared_messages.append({"role": "system", "content": summary})
        prepared_messages.extend(conversation_messages[-3:])
        return prepared_messages, summary

    def build_messages(self, conversation_messages):
        prepared_messages, _ = self.prepare_conversation_messages(
            conversation_messages
        )
        messages = [{"role": "system", "content": self.build_system_instruction()}]
        preferred_language = self.infer_response_language(conversation_messages)
        if preferred_language == "kz":
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Отвечай строго на казахском языке. "
                        "Не переключайся на русский, пока клиент сам явно этого не попросит."
                    ),
                }
            )
        else:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Отвечай строго на русском языке. "
                        "Не переключайся на казахский, если клиент прямо не написал на казахском "
                        "или явно не попросил ответ на казахском."
                    ),
                }
            )
        messages.append(
            {
                "role": "system",
                "content": self.build_response_policy_instruction(),
            }
        )
        messages.extend(prepared_messages)
        return messages

    @classmethod
    def detect_explicit_language_preference(cls, text: str) -> str | None:
        normalized = (text or "").strip().lower()
        if not normalized:
            return None

        if any(marker in normalized for marker in cls.RUSSIAN_LANGUAGE_MARKERS):
            return "ru"
        if any(marker in normalized for marker in cls.KAZAKH_LANGUAGE_MARKERS):
            return "kz"
        return None

    @classmethod
    def detect_message_language_signal(cls, text: str) -> str | None:
        normalized = (text or "").strip().lower()
        if not normalized:
            return None

        explicit_preference = cls.detect_explicit_language_preference(normalized)
        if explicit_preference is not None:
            return explicit_preference

        if any(letter in normalized for letter in cls.KAZAKH_SPECIFIC_LETTERS):
            return "kz"
        if any(marker in normalized for marker in cls.KAZAKH_PHRASE_MARKERS):
            return "kz"
        if cls.has_strong_russian_signal(normalized):
            return "ru"
        cyrillic_chars = [
            char
            for char in normalized
            if ("а" <= char <= "я") or char == "ё"
        ]
        if len(cyrillic_chars) >= 5:
            return "ru"
        return None

    @classmethod
    def has_strong_russian_signal(cls, text: str) -> bool:
        normalized = (text or "").strip().lower()
        if not normalized:
            return False
        return any(marker in normalized for marker in cls.RUSSIAN_PHRASE_MARKERS)

    def infer_response_language(self, conversation_messages) -> str:
        user_messages = [
            (message.get("content") or "").strip()
            for message in conversation_messages
            if message.get("role") == "user" and (message.get("content") or "").strip()
        ]

        if not user_messages:
            return "ru"

        latest_user_message = user_messages[-1]
        latest_explicit_preference = self.detect_explicit_language_preference(
            latest_user_message
        )
        if latest_explicit_preference is not None:
            return latest_explicit_preference

        latest_language_signal = self.detect_message_language_signal(latest_user_message)
        if latest_language_signal is not None:
            return latest_language_signal

        recent_history = user_messages[-4:-1]
        for content in reversed(recent_history):
            explicit_preference = self.detect_explicit_language_preference(content)
            if explicit_preference is not None:
                return explicit_preference
        for content in reversed(recent_history):
            detected_language = self.detect_message_language_signal(content)
            if detected_language is not None:
                return detected_language

        return "ru"

    def build_messages(self, conversation_messages):
        prepared_messages, _ = self.prepare_conversation_messages(
            conversation_messages
        )
        messages = [{"role": "system", "content": self.build_system_instruction()}]
        preferred_language = self.infer_response_language(conversation_messages)
        if preferred_language == "kz":
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Отвечай строго на казахском языке. "
                        "Не комментируй смену языка и не объясняй, почему отвечаешь именно так. "
                        "Просто отвечай по сути вопроса."
                    ),
                }
            )
        else:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Отвечай строго на русском языке. "
                        "Не комментируй смену языка и не объясняй, почему отвечаешь именно так. "
                        "Просто отвечай по сути вопроса."
                    ),
                }
            )
        messages.append(
            {
                "role": "system",
                "content": self.build_response_policy_instruction(),
            }
        )
        messages.extend(prepared_messages)
        return messages

    def create_chat_completion(self, conversation_messages):
        openai_client = self.get_openai_client()
        if openai_client is None:
            raise RuntimeError("OpenAI client is not configured.")

        temperature = 0.2
        if self.business is not None:
            temperature = self.business.get_ai_setting("temperature", temperature)

        return openai_client.chat.completions.create(
            model=self.model,
            messages=self.build_messages(conversation_messages),
            tools=OPENAI_FUNCTION_DEFINITIONS,
            tool_choice="auto",
            temperature=temperature,
        )

    def log_interaction(
        self,
        *,
        request_messages,
        response_text: str = "",
        summary_text: str = "",
        status: str,
        error_message: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ):
        if self.business is None:
            return None
        return AIInteractionLog.objects.create(
            business=self.business,
            request_messages=request_messages,
            response_text=response_text,
            summary_text=summary_text,
            model_name=self.model,
            status=status,
            error_message=error_message,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    @staticmethod
    def _extract_token_usage(*responses):
        """Aggregate prompt/completion token counts across one or more
        OpenAI responses. Returns (None, None) when no response carried
        usage data (typical for mocked responses in tests).
        """
        prompt = 0
        completion = 0
        found = False
        for response in responses:
            usage = getattr(response, "usage", None)
            if usage is None:
                continue
            found = True
            prompt += getattr(usage, "prompt_tokens", 0) or 0
            completion += getattr(usage, "completion_tokens", 0) or 0
        return (prompt, completion) if found else (None, None)

    def get_ai_response(self, conversation_messages):
        prepared_messages, summary_text = self.prepare_conversation_messages(
            conversation_messages
        )
        response = self.create_chat_completion(prepared_messages)
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            final_text = message.content or ""
            prompt_tokens, completion_tokens = self._extract_token_usage(response)
            self.log_interaction(
                request_messages=self.build_messages(prepared_messages),
                response_text=final_text,
                summary_text=summary_text,
                status=AIInteractionLog.Status.SUCCESS,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return final_text

        tool_messages = list(prepared_messages)
        tool_messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in tool_calls
                ],
            }
        )

        for tool_call in tool_calls:
            payload = json.loads(tool_call.function.arguments)
            tool_result = self.execute_tool_call(
                function_name=tool_call.function.name,
                payload=payload,
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )

        follow_up_response = self.create_chat_completion(tool_messages)
        final_text = follow_up_response.choices[0].message.content or ""
        prompt_tokens, completion_tokens = self._extract_token_usage(
            response, follow_up_response,
        )
        self.log_interaction(
            request_messages=self.build_messages(tool_messages),
            response_text=final_text,
            summary_text=summary_text,
            status=AIInteractionLog.Status.SUCCESS,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return final_text

    def generate_reply(self, conversation_messages):
        try:
            return self.get_ai_response(conversation_messages)
        except Exception as error:
            logger.exception("ai_request_failed")
            self.log_interaction(
                request_messages=self.build_messages(conversation_messages),
                summary_text="",
                status=AIInteractionLog.Status.FAILED,
                error_message=str(error),
            )
            raise

    def execute_tool_call(self, *, function_name: str, payload: dict):
        payload = dict(payload)
        if self.business is not None:
            payload["business_id"] = self.business.id
        if function_name == "create_appointment" and self.client is not None:
            payload["client_id"] = self.client.id
            payload.setdefault(
                "client_data",
                {
                    "name": self.client.name or "",
                    "phone": str(self.client.phone or ""),
                    "whatsapp_id": self.client.whatsapp_id or "",
                    "telegram_id": self.client.telegram_id or "",
                },
            )
        return execute_ai_function(
            function_name=function_name,
            payload=payload,
        )

    def build_follow_up_message(self, *, client_name: str, service_name: str):
        template = DEFAULT_FOLLOW_UP_TEMPLATE
        if self.business is not None:
            template = self.business.get_ai_setting(
                "follow_up_template",
                DEFAULT_FOLLOW_UP_TEMPLATE,
            )
        return template.format(
            client_name=client_name or "Здравствуйте",
            service_name=service_name,
        )

    def should_send_follow_up(self, *, booking):
        if booking.status != booking.Status.PENDING:
            return False
        if not booking.client.allow_follow_up:
            return False
        if booking.follow_up_sent_at is not None:
            return False
        return booking.created_at <= timezone.now() - timedelta(hours=1)

    def should_send_reminder(self, *, booking):
        if booking.status != booking.Status.CONFIRMED:
            return False
        if booking.reminder_sent_at is not None:
            return False
        now = timezone.now()
        return booking.start_time - timedelta(hours=2) <= now < booking.start_time

    def build_reminder_message(self, *, booking):
        local_tz = timezone.get_current_timezone()
        if self.business is not None and self.business.timezone_name:
            local_tz = ZoneInfo(self.business.timezone_name)
        local_start = timezone.localtime(booking.start_time, local_tz)
        template = DEFAULT_REMINDER_TEMPLATE
        if self.business is not None:
            template = self.business.get_ai_setting(
                "reminder_template",
                DEFAULT_REMINDER_TEMPLATE,
            )
        return template.format(
            time=f"{local_start:%H:%M}",
            service_name=booking.service.name,
        )

    def transcribe_audio(self, *, file_obj):
        openai_client = self.get_openai_client()
        if openai_client is None:
            return VOICE_FALLBACK_MESSAGE

        try:
            transcript = openai_client.audio.transcriptions.create(
                model=settings.OPENAI_TRANSCRIPTION_MODEL,
                file=file_obj,
            )
        except Exception:
            logger.exception("voice_transcription_failed")
            return VOICE_FALLBACK_MESSAGE
        return getattr(transcript, "text", None) or VOICE_FALLBACK_MESSAGE

    def handle_voice_message(self, *, file_obj):
        return self.transcribe_audio(file_obj=file_obj)

    def detect_human_request(self, text: str):
        normalized = (text or "").lower()
        return any(keyword in normalized for keyword in ESCALATION_KEYWORDS)

    def escalate_to_human(
        self,
        *,
        booking_id: int | None = None,
        reason: str,
        attempts: int = 0,
    ):
        booking = None
        if booking_id is not None:
            booking = Booking.objects.filter(pk=booking_id).first()
            if booking is not None:
                booking.status = Booking.Status.NEEDS_ATTENTION
                booking.notes = "\n".join(
                    part for part in [booking.notes, f"Escalation: {reason}"] if part
                )
                booking.save(update_fields=["status", "notes", "updated_at"])

        return {
            "status": "needs_attention",
            "booking_id": booking_id,
            "reason": reason,
            "attempts": attempts,
            "admin_chat_id": settings.HUMAN_ESCALATION_CHAT_ID,
            "admin_email": settings.ADMIN_ALERT_EMAIL,
        }

    def should_escalate(self, *, requested_human: bool, failed_attempts: int):
        return requested_human or failed_attempts >= 3
