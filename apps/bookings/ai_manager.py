import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from openai import OpenAI

from .ai.prompt_builder import PromptBuilder
from .models import Booking, Business
from .services import OPENAI_FUNCTION_DEFINITIONS, execute_ai_function


logger = logging.getLogger(__name__)
SYSTEM_PROMPT = PromptBuilder.DEFAULT_PROMPT

VOICE_FALLBACK_MESSAGE = (
    "Кешіріңіз, мен әзірге дауыстық хабарламаларды түсінбеймін, "
    "өтініш, мәтінмен жазыңыз."
)
HUMAN_HANDOFF_MESSAGE = (
    "Сейчас подключу живого администратора. Он поможет с этим вопросом 🤝"
)
AI_RETRY_MESSAGE = (
    "Не до конца поняла запрос. Уточните, пожалуйста, что именно нужно: "
    "свободное время, стоимость или запись?"
)
DEFAULT_FOLLOW_UP_TEMPLATE = (
    '"{client_name}", заметила, что мы не завершили запись на '
    "[{service_name}]. Подсказать что-нибудь по процедуре или "
    "забронировать это время за вами? Оно сейчас очень популярно ✨ "
    'Если неактуально, просто напишите "стоп".'
)
DEFAULT_REMINDER_TEMPLATE = (
    "Сәлем! Ждем вас сегодня в {time} на процедуру {service_name}. "
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
        prompt_builder: PromptBuilder | None = None,
    ):
        self.business = business
        self.client = client
        self.model = model or settings.OPENAI_MODEL
        self.prompt_builder = prompt_builder or PromptBuilder()

    def get_openai_client(self):
        if self.client is not None:
            return self.client
        if not settings.OPENAI_API_KEY:
            return None
        return OpenAI(api_key=settings.OPENAI_API_KEY)

    def build_system_instruction(self) -> str:
        if self.business is None:
            return self.prompt_builder.build_fallback_prompt()
        return self.prompt_builder.build_system_prompt(self.business)

    def build_messages(self, conversation_messages):
        messages = [{"role": "system", "content": self.build_system_instruction()}]
        messages.extend(conversation_messages)
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

    def generate_reply(self, conversation_messages):
        response = self.create_chat_completion(conversation_messages)
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []

        if not tool_calls:
            return message.content or ""

        tool_messages = list(conversation_messages)
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
        return follow_up_response.choices[0].message.content or ""

    def execute_tool_call(self, *, function_name: str, payload: dict):
        return execute_ai_function(
            function_name=function_name,
            payload=payload,
        )

    def build_follow_up_message(self, *, client_name: str, service_name: str):
        if self.business is not None:
            template = self.business.get_ai_setting(
                "follow_up_template",
                DEFAULT_FOLLOW_UP_TEMPLATE,
            )
        else:
            template = DEFAULT_FOLLOW_UP_TEMPLATE
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
        return timezone.now() >= booking.start_time - timedelta(hours=2)

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
