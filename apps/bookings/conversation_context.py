"""Conversation context helpers.

Reads a client's recent message history into the role/content format
that the OpenAI Chat API expects, and uses that context to infer the
language the bot should reply in.

Dependencies: AIManager (for the language inference call) and the
Client/ConversationMessage models. No webhooks imports — graph stays
one-way.
"""

from .ai_manager import AIManager
from .models import Client, ConversationMessage


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
