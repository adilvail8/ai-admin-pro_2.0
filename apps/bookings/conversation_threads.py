from datetime import timedelta

from django.utils import timezone

from .models import ConversationThread


def get_or_create_conversation_thread(*, business, client, channel):
    thread, _ = ConversationThread.objects.get_or_create(
        business=business,
        client=client,
        channel=channel,
        defaults={"mode": ConversationThread.Mode.BOT_ACTIVE},
    )
    return thread


def is_bot_active(thread):
    mode = ConversationThread.Mode
    if thread.mode == mode.BOT_ACTIVE:
        return True
    if thread.mode == mode.BOT_PAUSED_UNTIL:
        if not thread.bot_paused_until:
            return False
        if timezone.now() >= thread.bot_paused_until:
            set_thread_mode(thread, mode.BOT_ACTIVE)
            return True
        return False
    return False


def pause_bot_for_human_reply(thread, *, minutes=30):
    thread.mode = ConversationThread.Mode.BOT_PAUSED_UNTIL
    thread.bot_paused_until = timezone.now() + timedelta(minutes=minutes)
    thread.save(update_fields=["mode", "bot_paused_until"])
    return thread


def set_thread_mode(thread, mode):
    thread.mode = mode
    if mode != ConversationThread.Mode.BOT_PAUSED_UNTIL:
        thread.bot_paused_until = None
    thread.save(update_fields=["mode", "bot_paused_until"])
    return thread
