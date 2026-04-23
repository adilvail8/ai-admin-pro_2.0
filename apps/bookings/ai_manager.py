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
    "Сейчас подключу живого администратора. Он поможет с этим вопросом 🤝"
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
    def __init__(
        self,
        *,
        business: Business | None = None,
        client=None,
        model: str | None = None,
    ):
        self.business = business
        self.client = client
        self.model = model or settings.OPENAI_MODEL
        self.prompt_builder = PromptBuilder()

    def get_openai_client(self):
        if self.client is not None:
            return self.client
        if not settings.OPENAI_API_KEY:
            return None
        return OpenAI(api_key=settings.OPENAI_API_KEY)

    def build_system_instruction(self) -> str:
        if self.business is None:
            return "\n\n".join([SYSTEM_PROMPT, self.prompt_builder.build_fallback_prompt()])
        return self.prompt_builder.build_system_prompt(self.business)

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
        )

    def get_ai_response(self, conversation_messages):
        prepared_messages, summary_text = self.prepare_conversation_messages(
            conversation_messages
        )
        response = self.create_chat_completion(prepared_messages)
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            final_text = message.content or ""
            self.log_interaction(
                request_messages=self.build_messages(prepared_messages),
                response_text=final_text,
                summary_text=summary_text,
                status=AIInteractionLog.Status.SUCCESS,
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
        self.log_interaction(
            request_messages=self.build_messages(tool_messages),
            response_text=final_text,
            summary_text=summary_text,
            status=AIInteractionLog.Status.SUCCESS,
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
