import pytest

from apps.bookings.ai import PromptBuilder
from apps.bookings.ai_manager import AIManager
from apps.bookings.models import Business


@pytest.mark.django_db
def test_ai_manager_preserves_explicit_russian_preference_for_follow_ups():
    ai_manager = AIManager(client=object(), model="test-model")

    messages = ai_manager.build_messages(
        [
            {"role": "user", "content": "На русском, пожалуйста."},
            {"role": "assistant", "content": "Хорошо, отвечаю на русском."},
            {"role": "user", "content": "Саламатсыз"},
            {"role": "assistant", "content": "Здравствуйте!"},
            {"role": "user", "content": "10"},
        ]
    )

    assert messages[1]["role"] == "system"
    assert "Отвечай строго на русском языке" in messages[1]["content"]


@pytest.mark.django_db
def test_ai_manager_preserves_explicit_kazakh_preference_for_numeric_follow_up():
    ai_manager = AIManager(client=object(), model="test-model")

    messages = ai_manager.build_messages(
        [
            {"role": "user", "content": "қазақша сөйле"},
            {"role": "assistant", "content": "Жақсы, қазақша жауап беремін."},
            {"role": "user", "content": "ертең"},
            {"role": "assistant", "content": "Қай уақыт ыңғайлы?"},
            {"role": "user", "content": "10"},
        ]
    )

    assert messages[1]["role"] == "system"
    assert "Отвечай строго на казахском языке" in messages[1]["content"]


@pytest.mark.django_db
def test_ai_manager_detects_explicit_russian_preference_before_recent_short_message():
    assert (
        AIManager().infer_response_language(
            [
                {"role": "user", "content": "На русском"},
                {"role": "assistant", "content": "Хорошо."},
                {"role": "user", "content": "?"},
            ]
        )
        == "ru"
    )


@pytest.mark.django_db
def test_ai_manager_detects_kazakh_transliteration_without_specific_letters():
    assert (
        AIManager().infer_response_language(
            [
                {"role": "user", "content": "казакша сойле"},
                {"role": "assistant", "content": "Жақсы, қазақша жауап беремін."},
                {"role": "user", "content": "ертен коремыз"},
            ]
        )
        == "kz"
    )


@pytest.mark.django_db
def test_prompt_builder_marks_salon_as_closed_after_working_hours(monkeypatch):
    business = Business.objects.create(
        name="Aura",
        city="Almaty",
        address="Abylai Khan 68",
        working_hours="10:00-20:00",
        timezone_name="Asia/Almaty",
        is_active=True,
    )

    import apps.bookings.ai.prompt_builder as prompt_builder_module
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(
        prompt_builder_module.timezone,
        "now",
        lambda: datetime(2026, 4, 27, 21, 23, tzinfo=ZoneInfo("Asia/Almaty")),
    )

    prompt = PromptBuilder().build_system_prompt(business)

    assert "салон уже закрыт" in prompt
    assert "не отвечай так, будто салон открыт" in prompt


@pytest.mark.django_db
def test_ai_manager_switches_back_to_russian_on_new_clear_russian_message():
    assert (
        AIManager().infer_response_language(
            [
                {"role": "user", "content": "қазақша сөйле"},
                {"role": "assistant", "content": "Жақсы, қазақша жауап беремін."},
                {"role": "user", "content": "здравствуйте"},
            ]
        )
        == "ru"
    )


@pytest.mark.django_db
def test_ai_manager_prefers_current_clear_russian_message_over_old_kazakh_preference():
    assert (
        AIManager().infer_response_language(
            [
                {"role": "user", "content": "қазақша сөйле"},
                {"role": "assistant", "content": "Жақсы, қазақша жауап беремін."},
                {"role": "user", "content": "здравствуйте"},
                {"role": "assistant", "content": "Здравствуйте! Как я могу помочь?"},
                {"role": "user", "content": "на стрижку мужскую сегодня успеваю?"},
            ]
        )
        == "ru"
    )


@pytest.mark.django_db
def test_ai_manager_switches_to_russian_on_mixed_message_with_oryssha_request():
    assert (
        AIManager().infer_response_language(
            [
                {"role": "user", "content": "қазақша сөйле"},
                {"role": "assistant", "content": "Жақсы, қазақша жауап беремін."},
                {"role": "user", "content": "орысша сөйлесесіз бе? хочу уточнить время"},
            ]
        )
        == "ru"
    )


@pytest.mark.django_db
def test_prompt_builder_instructs_not_to_comment_language_switch():
    business = Business.objects.create(
        name="Aura",
        city="Almaty",
        address="Abylai Khan 68",
        working_hours="10:00-20:00",
        timezone_name="Asia/Almaty",
        is_active=True,
    )

    prompt = PromptBuilder().build_system_prompt(business)

    assert "Не объясняй, почему выбрал этот язык" in prompt
    assert "не комментируй переключение языка" in prompt
@pytest.mark.django_db
def test_ai_manager_build_messages_includes_anti_hallucination_policy():
    messages = AIManager().build_messages(
        [
            {"role": "user", "content": "какие услуги есть"},
        ]
    )

    combined = "\n".join(message["content"] for message in messages if message.get("content"))
    assert "Never invent masters, services, prices, addresses, booking statuses" in combined
    assert "Do not confirm a booking unless the booking is already present" in combined


@pytest.mark.django_db
def test_ai_manager_build_messages_includes_human_admin_tone_policy():
    messages = AIManager().build_messages(
        [
            {"role": "user", "content": "сколько стоит стрижка"},
        ]
    )

    combined = "\n".join(message["content"] for message in messages if message.get("content"))
    assert "Reply like a human salon administrator in messenger" in combined
    assert "Prefer 1-2 short sentences. No emojis. No long explanations. No generic filler." in combined
