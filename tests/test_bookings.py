import json
from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import BusinessMembership
from apps.bookings.ai_manager import AIManager, AI_RETRY_MESSAGE, SYSTEM_PROMPT
from apps.bookings.client_identity import ClientIdentityResolver
from apps.bookings.models import (
    AuditLog,
    AIInteractionLog,
    Booking,
    Business,
    Category,
    Client,
    ConversationMessage,
    InboundEvent,
    Master,
    OutboundMessage,
    Service,
)
from apps.bookings.services import (
    OPENAI_FUNCTION_DEFINITIONS,
    create_appointment,
    execute_ai_function,
    get_available_slots,
)
from apps.bookings.tasks import (
    notify_human_operator,
    process_pending_reminders,
    send_booking_reminder,
    send_outbound_message,
    send_follow_up_if_pending,
)
from apps.bookings.transports import SendResult, TelegramTransport, WhatsAppTransport
from apps.bookings.webhooks import (
    VOICE_FALLBACK_MESSAGE,
    get_or_create_client,
    handle_audio_message,
    handle_text_message,
    store_message,
)


User = get_user_model()


@pytest.fixture
def business():
    return Business.objects.create(
        name="Barber House",
        brand_name="Urban Flow",
        address="Розыбакиева 247а",
        working_hours="10:00-20:00",
        knowledge_base="Standalone barbershop assistant.",
        ai_settings={
            "temperature": 0.2,
            "tone": "Care & Professionalism",
            "rules": ["Всегда подтверждай детали записи."],
        },
    )


@pytest.fixture
def owner_user():
    return User.objects.create_user(
        username="owner",
        password="StrongPass123!",
        email="owner@example.com",
    )


@pytest.fixture
def business_membership(owner_user, business):
    return BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
    )


@pytest.fixture
def another_business():
    return Business.objects.create(
        name="Second Studio",
        brand_name="Second Studio",
        address="Абая 10",
        working_hours="09:00-18:00",
        knowledge_base="Independent salon.",
        ai_settings={"temperature": 0.2},
    )


@pytest.fixture
def client_profile(business):
    return Client.objects.create(
        business=business,
        name="Adil",
        phone="+77071234567",
        whatsapp_id="wa_123",
    )


@pytest.fixture
def master(business):
    return Master.objects.create(
        business=business,
        full_name="Ivan Petrov",
        specialization="Barber",
        working_hours={
            "mon": {"start": "09:00", "end": "18:00"},
            "tue": {"start": "09:00", "end": "18:00"},
            "wed": {"start": "09:00", "end": "18:00"},
            "thu": {"start": "09:00", "end": "18:00"},
            "fri": {"start": "09:00", "end": "18:00"},
        },
    )


@pytest.fixture
def service(business):
    return Service.objects.create(
        business=business,
        name="Haircut",
        price=Decimal("25.00"),
        duration=timedelta(minutes=60),
        buffer_time=timedelta(minutes=15),
    )


class StubAIManager:
    def __init__(self, reply="ok", should_fail=False):
        self.reply = reply
        self.should_fail = should_fail

    def detect_human_request(self, text):
        return False

    def should_escalate(self, requested_human, failed_attempts):
        return False

    def generate_reply(self, conversation_messages):
        if self.should_fail:
            raise RuntimeError("llm down")
        return self.reply

    def handle_voice_message(self, file_obj):
        return "Хочу записаться"


class AcceptingTransport:
    def send_text(self, *, recipient, text, metadata=None):
        return SendResult(
            accepted=True,
            delivered=False,
            provider_message_id="provider-accepted-1",
            raw_response={"recipient": recipient, "metadata": metadata or {}},
        )


@pytest.mark.django_db
def test_client_phone_is_stored_in_e164_format(business):
    saved_client = Client.objects.create(
        business=business,
        name="Aruzhan",
        phone="87071234567",
    )

    assert str(saved_client.phone) == "+77071234567"


@pytest.mark.django_db
def test_booking_calculates_end_time_with_buffer(
    business,
    client_profile,
    master,
    service,
):
    start_time = timezone.now() + timedelta(days=1)
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=start_time,
        client_data={"name": "Alex"},
    )

    assert booking.service_duration == timedelta(minutes=60)
    assert booking.service_buffer_time == timedelta(minutes=15)
    assert booking.end_time == start_time + timedelta(minutes=75)


@pytest.mark.django_db
def test_booking_keeps_historical_timing_after_service_update(
    business,
    client_profile,
    master,
    service,
):
    start_time = timezone.now() + timedelta(days=1)
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=start_time,
        client_data={"name": "Alex"},
    )

    service.duration = timedelta(minutes=90)
    service.buffer_time = timedelta(minutes=20)
    service.save(update_fields=["duration", "buffer_time"])

    booking.notes = "Client confirmed by phone"
    booking.save(update_fields=["notes", "updated_at"])
    booking.refresh_from_db()

    assert booking.service_duration == timedelta(minutes=60)
    assert booking.service_buffer_time == timedelta(minutes=15)
    assert booking.end_time == start_time + timedelta(minutes=75)


@pytest.mark.django_db
def test_booking_rejects_overlap_with_buffer(
    business,
    client_profile,
    master,
    service,
):
    start_time = timezone.now() + timedelta(days=1)
    Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=start_time,
        client_data={"name": "Alex"},
        status=Booking.Status.CONFIRMED,
    )

    overlapping = Booking(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=start_time + timedelta(minutes=65),
        client_data={"name": "Maria"},
        status=Booking.Status.PENDING,
    )

    with pytest.raises(ValidationError):
        overlapping.full_clean()


@pytest.mark.django_db
def test_get_available_slots_respects_buffer_time(
    business,
    client_profile,
    master,
    service,
):
    days_until_monday = (7 - timezone.localdate().weekday()) % 7
    monday = timezone.localdate() + timedelta(days=days_until_monday)
    busy_start = Booking.make_aware_datetime(monday, time(hour=10, minute=0))

    Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=busy_start,
        client_data={"name": "Busy client"},
        status=Booking.Status.CONFIRMED,
    )

    slots = get_available_slots(
        business.id,
        target_date=monday,
        service_id=service.id,
    )

    slot_starts = {(slot.start.hour, slot.start.minute) for slot in slots}
    assert (10, 0) not in slot_starts
    assert (10, 30) not in slot_starts
    assert (11, 0) not in slot_starts
    assert (11, 30) in slot_starts


@pytest.mark.django_db
def test_create_appointment_persists_booking_for_business_id(
    business,
    client_profile,
    master,
    service,
):
    start_time = timezone.now() + timedelta(days=2)
    booking = create_appointment(
        business.id,
        master_id=master.id,
        service_id=service.id,
        client_id=client_profile.id,
        start_time=start_time,
        client_data={"name": "Olga", "phone": "+77000000000"},
        status=Booking.Status.CONFIRMED,
    )

    assert booking.pk is not None
    assert booking.client_id == client_profile.id
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.end_time == start_time + timedelta(minutes=75)


@pytest.mark.django_db
def test_create_appointment_rejects_cross_tenant_client(
    business,
    another_business,
    master,
    service,
):
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign client",
        phone="+77070000001",
    )

    with pytest.raises(Client.DoesNotExist):
        create_appointment(
            business.id,
            master_id=master.id,
            service_id=service.id,
            client_id=foreign_client.id,
            start_time=timezone.now() + timedelta(days=2),
            client_data={"name": "Olga"},
        )


@pytest.mark.django_db
def test_ai_manager_builds_multi_tenant_prompt(business):
    ai_manager = AIManager(business=business, client=object(), model="test-model")
    messages = ai_manager.build_messages([{"role": "user", "content": "Привет"}])

    assert messages[0]["role"] == "system"
    assert business.display_brand_name in messages[0]["content"]
    assert business.address in messages[0]["content"]
    assert business.working_hours in messages[0]["content"]
    assert "Сегодня:" in messages[0]["content"]
    assert "Бүгін:" in messages[0]["content"]
    assert "Если клиент пишет на казахском" in messages[0]["content"]


@pytest.mark.django_db
def test_ai_manager_fallback_prompt_keeps_system_prompt():
    ai_manager = AIManager(client=object(), model="test-model")
    messages = ai_manager.build_messages([{"role": "user", "content": "Привет"}])

    assert SYSTEM_PROMPT in messages[0]["content"]
    assert "Сегодня:" in messages[0]["content"]
    assert "Бүгін:" in messages[0]["content"]


@pytest.mark.django_db
def test_ai_manager_summarizes_long_history(business):
    ai_manager = AIManager(business=business, client=object(), model="test-model")
    history = [
        {"role": "user", "content": f"message-{index}"}
        for index in range(12)
    ]

    prepared_messages, summary = ai_manager.prepare_conversation_messages(history)

    assert summary.startswith("Краткое резюме")
    assert len(prepared_messages) == 4
    assert prepared_messages[0]["role"] == "system"
    assert [item["content"] for item in prepared_messages[1:]] == [
        "message-9",
        "message-10",
        "message-11",
    ]


@pytest.mark.django_db
def test_openai_tool_definition_uses_get_free_slots_name():
    tool_names = {
        item["function"]["name"] for item in OPENAI_FUNCTION_DEFINITIONS
    }

    assert "get_free_slots" in tool_names
    assert "create_appointment" in tool_names


@pytest.mark.django_db
def test_execute_ai_function_serializes_slots_to_json_payload(
    business,
    client_profile,
    master,
    service,
):
    days_until_monday = (7 - timezone.localdate().weekday()) % 7
    monday = timezone.localdate() + timedelta(days=days_until_monday)

    result = execute_ai_function(
        function_name="get_free_slots",
        payload={
            "business_id": business.id,
            "date": monday.isoformat(),
            "service_id": service.id,
        },
    )

    assert isinstance(result, list)
    assert {"start_time", "end_time", "master_id", "master_name"} <= set(result[0].keys())


@pytest.mark.django_db
def test_create_appointment_rejects_past_time_explicitly(
    business,
    client_profile,
    master,
    service,
):
    with pytest.raises(ValidationError):
        create_appointment(
            business.id,
            master_id=master.id,
            service_id=service.id,
            client_id=client_profile.id,
            start_time=timezone.now() - timedelta(minutes=5),
            client_data={"name": "Late client"},
        )


@pytest.mark.django_db
def test_create_appointment_writes_audit_log(
    business,
    client_profile,
    master,
    service,
):
    booking = create_appointment(
        business.id,
        master_id=master.id,
        service_id=service.id,
        client_id=client_profile.id,
        start_time=timezone.now() + timedelta(days=2),
        client_data={"name": "Olga"},
    )

    assert AuditLog.objects.filter(
        booking=booking,
        event_type="booking_created",
    ).exists()


@pytest.mark.django_db
def test_ai_manager_logs_request_and_response(business):
    class FakeMessage:
        content = "Здравствуйте!"
        tool_calls = []

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        chat = FakeChat()

    ai_manager = AIManager(business=business, client=FakeOpenAI(), model="test-model")
    reply = ai_manager.get_ai_response(
        [{"role": "user", "content": "Привет"}]
    )

    interaction_log = AIInteractionLog.objects.get(business=business)

    assert reply == "Здравствуйте!"
    assert interaction_log.response_text == "Здравствуйте!"
    assert interaction_log.status == AIInteractionLog.Status.SUCCESS


@pytest.mark.django_db
def test_follow_up_task_creates_and_sends_outbound_message(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: AcceptingTransport(),
    )
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
        status=Booking.Status.PENDING,
    )
    booking.created_at = timezone.now() - timedelta(hours=1, minutes=5)
    booking.save(update_fields=["created_at", "updated_at"])

    result = send_follow_up_if_pending.run(booking.id)
    booking.refresh_from_db()
    outbound_message = OutboundMessage.objects.get(
        booking=booking,
        message_type="follow_up",
    )

    assert result["status"] == OutboundMessage.Status.SUBMITTED
    assert outbound_message.submitted_at is not None
    assert booking.follow_up_sent_at is None
    assert AuditLog.objects.filter(
        outbound_message=outbound_message,
        event_type="outbound_submitted",
    ).exists()


def test_telegram_transport_builds_provider_result(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 77}}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json=None, headers=None):
            assert "sendMessage" in url
            assert json["chat_id"] == "12345"
            return FakeResponse()

    monkeypatch.setattr(
        "apps.bookings.transports.httpx.Client",
        FakeClient,
    )

    with override_settings(TELEGRAM_BOT_TOKEN="bot-token"):
        result = TelegramTransport().send_text(
            recipient="12345",
            text="hello",
        )

    assert result.accepted is True
    assert result.provider_message_id == "77"


def test_whatsapp_transport_normalizes_chat_id(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"idMessage": "wamid-1"}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json=None, headers=None):
            assert "waInstance123/sendMessage/token-1" in url
            assert json["chatId"] == "77071234567@c.us"
            return FakeResponse()

    monkeypatch.setattr(
        "apps.bookings.transports.httpx.Client",
        FakeClient,
    )

    with override_settings(
        GREEN_API_URL="https://7105.api.greenapi.com",
        GREEN_API_INSTANCE_ID="123",
        GREEN_API_API_TOKEN="token-1",
    ):
        result = WhatsAppTransport().send_text(
            recipient="+77071234567",
            text="hello",
        )

    assert result.accepted is True
    assert result.provider_message_id == "wamid-1"


@pytest.mark.django_db
def test_reminder_task_creates_and_sends_outbound_message(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: AcceptingTransport(),
    )
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1, minutes=50),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )

    result = send_booking_reminder.run(booking.id)
    booking.refresh_from_db()
    outbound_message = OutboundMessage.objects.get(
        booking=booking,
        message_type="reminder",
    )

    assert result["status"] == OutboundMessage.Status.SUBMITTED
    assert service.name in result["text"]
    assert outbound_message.submitted_at is not None
    assert booking.reminder_sent_at is None
    assert AuditLog.objects.filter(
        outbound_message=outbound_message,
        event_type="reminder_queued",
    ).exists()


@pytest.mark.django_db
def test_reminder_task_reuses_failed_outbound_instead_of_creating_duplicate(
    business,
    client_profile,
    master,
    service,
):
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1, minutes=30),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )
    failed_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        booking=booking,
        channel="whatsapp",
        recipient=str(client_profile.phone),
        message_type="reminder",
        text="Reminder",
        status=OutboundMessage.Status.FAILED,
        attempts=1,
        error_code="provider_down",
    )

    result = send_booking_reminder.run(booking.id)

    assert result["status"] == OutboundMessage.Status.FAILED
    assert result["outbound_message_id"] == failed_message.id
    assert OutboundMessage.objects.filter(
        booking=booking,
        message_type="reminder",
    ).count() == 1


@pytest.mark.django_db
def test_outbound_retry_cancels_expired_reminder_before_transport_call(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    class ShouldNotBeCalledTransport:
        def send_text(self, *, recipient, text, metadata=None):
            raise AssertionError("Transport should not be called for expired reminders")

    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: ShouldNotBeCalledTransport(),
    )

    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )
    Booking.objects.filter(pk=booking.pk).update(
        start_time=timezone.now() - timedelta(minutes=10),
        end_time=timezone.now() + timedelta(minutes=65),
    )
    outbound_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        booking=booking,
        channel="whatsapp",
        recipient=str(client_profile.phone),
        message_type="reminder",
        text="Reminder",
    )

    result = send_outbound_message.run(outbound_message.id)
    outbound_message.refresh_from_db()

    assert result["status"] == OutboundMessage.Status.CANCELLED
    assert outbound_message.status == OutboundMessage.Status.CANCELLED
    assert outbound_message.error_code == "booking_start_time_already_passed"


@pytest.mark.django_db
def test_outbound_message_is_not_marked_submitted_when_transport_fails(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    class FailingTransport:
        def send_text(self, *, recipient, text, metadata=None):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: FailingTransport(),
    )

    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1, minutes=50),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )

    result = send_booking_reminder.run(booking.id)
    booking.refresh_from_db()
    outbound_message = OutboundMessage.objects.get(
        booking=booking,
        message_type="reminder",
    )

    assert result["status"] == OutboundMessage.Status.FAILED
    assert outbound_message.submitted_at is None
    assert booking.reminder_sent_at is None


@pytest.mark.django_db
@override_settings(MAX_OUTBOUND_ATTEMPTS=1)
def test_outbound_message_moves_to_dead_letter_after_retry_limit(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    class FailingTransport:
        def send_text(self, *, recipient, text, metadata=None):
            return type(
                "Result",
                (),
                {
                    "accepted": False,
                    "delivered": False,
                    "provider_message_id": None,
                    "raw_response": {"ok": False},
                    "error_code": "provider_down",
                    "error_message": "Provider is unavailable",
                },
            )()

    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: FailingTransport(),
    )

    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1, minutes=50),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )

    result = send_booking_reminder.run(booking.id)
    outbound_message = OutboundMessage.objects.get(
        booking=booking,
        message_type="reminder",
    )

    assert result["status"] == OutboundMessage.Status.DEAD_LETTER
    assert outbound_message.dead_lettered_at is not None
    assert booking.reminder_sent_at is None


@pytest.mark.django_db
def test_notify_human_operator_reports_delivery_status(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: AcceptingTransport(),
    )
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
        status=Booking.Status.PENDING,
    )

    result = notify_human_operator.run(
        booking_id=booking.id,
        reason="Client requested a live administrator",
        attempts=1,
    )

    outbound_message = OutboundMessage.objects.get(
        booking=booking,
        message_type="handoff",
    )

    assert result["notification_status"] == OutboundMessage.Status.SUBMITTED
    assert outbound_message.provider_message_id
    assert AuditLog.objects.filter(
        booking=booking,
        event_type="handoff_requested",
    ).exists()


@pytest.mark.django_db
@override_settings(OUTBOUND_CALLBACK_SECRET="callback-secret")
def test_outbound_delivery_webhook_marks_message_as_delivered(
    client,
    business,
    client_profile,
):
    outbound_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient=client_profile.whatsapp_id or str(client_profile.phone),
        message_type="reminder",
        text="Reminder",
        status=OutboundMessage.Status.SUBMITTED,
        provider_message_id="provider-123",
        provider_response={"accepted": True},
        submitted_at=timezone.now(),
    )

    response = client.post(
        "/api/v1/webhooks/outbound-delivery/",
        data=json.dumps(
            {
                "provider_message_id": "provider-123",
                "status": "delivered",
            }
        ),
        content_type="application/json",
        HTTP_X_OUTBOUND_CALLBACK_SECRET="callback-secret",
    )

    outbound_message.refresh_from_db()

    assert response.status_code == 200
    assert outbound_message.status == OutboundMessage.Status.DELIVERED
    assert outbound_message.delivered_at is not None
    assert AuditLog.objects.filter(
        outbound_message=outbound_message,
        event_type="outbound_delivery_confirmed",
    ).exists()


@pytest.mark.django_db
def test_healthcheck_returns_ok(client):
    response = client.get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["celery"]["default_queue"] == "messages"
    assert "apps.bookings.tasks.async_prune_history" in response.json()["celery"]["routes"]


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_healthcheck_fails_when_broker_is_unavailable(client, monkeypatch):
    from apps.bookings.views import healthcheck

    monkeypatch.setitem(
        healthcheck.__globals__,
        "check_broker_connection",
        lambda: False,
    )

    response = client.get("/api/v1/health/")

    assert response.status_code == 503
    assert response.json()["status"] == "failed"
    assert response.json()["checks"]["broker"] == "failed"


@pytest.mark.django_db
def test_jwt_token_obtain_and_me_endpoint(client, owner_user, business_membership):
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )

    assert token_response.status_code == 200
    access_token = token_response.json()["access"]

    me_response = client.get(
        "/api/v1/auth/me/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert me_response.status_code == 200
    assert me_response.json()["username"] == "owner"


@pytest.mark.django_db
def test_bookings_api_is_scoped_by_business_membership(
    client,
    owner_user,
    business,
    business_membership,
    client_profile,
    master,
    service,
):
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )

    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]

    bookings_response = client.get(
        "/api/v1/bookings/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert bookings_response.status_code == 200
    assert bookings_response.json()[0]["id"] == booking.id


@pytest.mark.django_db
def test_cannot_deactivate_last_business_owner(business_membership):
    business_membership.is_active = False

    with pytest.raises(ValidationError):
        business_membership.save()


@pytest.mark.django_db
def test_cannot_delete_last_business_owner(business_membership):
    with pytest.raises(ValidationError):
        business_membership.delete()


@pytest.mark.django_db
def test_ai_manager_escalates_to_human_by_request(
    business,
    client_profile,
    master,
    service,
):
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
        status=Booking.Status.PENDING,
    )

    ai_manager = AIManager(business=business, client=object(), model="test-model")

    assert ai_manager.should_escalate(
        requested_human=True,
        failed_attempts=0,
    ) is True

    result = notify_human_operator.run(
        booking_id=booking.id,
        reason="Client requested a live administrator",
        attempts=1,
    )
    booking.refresh_from_db()

    assert result["status"] == "needs_attention"
    assert booking.status == Booking.Status.NEEDS_ATTENTION


@pytest.mark.django_db
def test_voice_message_falls_back_when_transcription_fails():
    class FailingAudioTranscriptions:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("whisper unavailable")

    class FailingAudio:
        transcriptions = FailingAudioTranscriptions()

    class FailingClient:
        audio = FailingAudio()

    ai_manager = AIManager(client=FailingClient(), model="test-model")
    message = ai_manager.handle_voice_message(file_obj=object())

    assert message == VOICE_FALLBACK_MESSAGE


@pytest.mark.django_db
def test_handle_audio_message_does_not_duplicate_user_message(
    business,
    client_profile,
    monkeypatch,
):
    monkeypatch.setattr(
        "apps.bookings.webhooks.AIManager",
        lambda business=None: StubAIManager(reply="Принято"),
    )
    response = handle_audio_message(
        business_id=business.id,
        channel=ConversationMessage.Channel.WHATSAPP,
        client=client_profile,
        audio_file=SimpleUploadedFile("voice.ogg", b"test", content_type="audio/ogg"),
    )

    user_messages = ConversationMessage.objects.filter(
        business=business,
        client=client_profile,
        role=ConversationMessage.Role.USER,
    )

    assert response["transcript"] == "Хочу записаться"
    assert user_messages.count() == 1


@pytest.mark.django_db
def test_rate_limit_is_checked_before_user_message_persist(
    business,
    client_profile,
):
    ConversationMessage.objects.create(
        business=business,
        client=client_profile,
        channel=ConversationMessage.Channel.WHATSAPP,
        role=ConversationMessage.Role.USER,
        content="msg-1",
    )

    with override_settings(MAX_MESSAGES_PER_MINUTE=1):
        with pytest.raises(ValidationError):
            handle_text_message(
                business_id=business.id,
                channel=ConversationMessage.Channel.WHATSAPP,
                client=client_profile,
                text="spam",
                ai_manager=StubAIManager(),
            )

    assert (
        ConversationMessage.objects.filter(
            business=business,
            client=client_profile,
            role=ConversationMessage.Role.USER,
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_store_message_schedules_async_prune_instead_of_running_inline(
    business,
    client_profile,
    monkeypatch,
):
    scheduled_calls = []

    def fake_delay(**kwargs):
        scheduled_calls.append(kwargs)

    monkeypatch.setattr(
        "apps.bookings.webhooks.async_prune_history.delay",
        fake_delay,
    )

    for index in range(20):
        store_message(
            business_id=business.id,
            client=client_profile,
            channel=ConversationMessage.Channel.WHATSAPP,
            role=ConversationMessage.Role.USER,
            content=f"message-{index}",
        )

    assert scheduled_calls == [
        {
            "business_id": business.id,
            "client_id": client_profile.id,
            "channel": ConversationMessage.Channel.WHATSAPP,
        }
    ]


@pytest.mark.django_db
def test_ai_failures_return_retry_message_until_escalation(
    business,
    client_profile,
):
    response = handle_text_message(
        business_id=business.id,
        channel=ConversationMessage.Channel.WHATSAPP,
        client=client_profile,
        text="Привет",
        ai_manager=StubAIManager(should_fail=True),
    )

    client_profile.refresh_from_db()

    assert response == {"reply": AI_RETRY_MESSAGE, "escalated": False}
    assert client_profile.ai_failure_count == 1


@pytest.mark.django_db
def test_client_identity_resolver_requires_phone_for_whatsapp(business):
    resolver = ClientIdentityResolver()

    with pytest.raises(ValidationError):
        resolver.resolve_or_create(
            business=business,
            channel=ConversationMessage.Channel.WHATSAPP,
            phone="",
            external_id="wa-1",
            name="Test",
        )


@pytest.mark.django_db
def test_get_or_create_client_reuses_existing_phone_record(business):
    original = Client.objects.create(
        business=business,
        name="Adil",
        phone="+77070000010",
    )

    resolved = get_or_create_client(
        business_id=business.id,
        channel=ConversationMessage.Channel.WHATSAPP,
        external_id="wa-new",
        phone="+77070000010",
        name="Adil Updated",
    )

    original.refresh_from_db()
    assert resolved.id == original.id
    assert original.whatsapp_id == "wa-new"


@pytest.mark.django_db
@override_settings(WEBHOOK_SHARED_SECRET="secret-token")
def test_webhook_rejects_invalid_token(client):
    response = client.post(
        "/api/v1/webhooks/messenger/",
        data=json.dumps(
            {
                "business_id": 1,
                "channel": "whatsapp",
                "phone": "+77071234567",
                "text": "Привет",
            }
        ),
        content_type="application/json",
        HTTP_X_WEBHOOK_TOKEN="wrong-token",
    )

    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(WEBHOOK_SHARED_SECRET="secret-token")
def test_webhook_accepts_text_message(client, business, monkeypatch):
    def fake_handle_text_message(**kwargs):
        return {"reply": "Здравствуйте!", "escalated": False}

    monkeypatch.setattr(
        "apps.bookings.views.handle_text_message",
        fake_handle_text_message,
    )

    response = client.post(
        "/api/v1/webhooks/messenger/",
        data=json.dumps(
            {
                "business_id": business.id,
                "channel": "whatsapp",
                "external_id": "wa-1",
                "phone": "+77071234567",
                "name": "Adil",
                "text": "Привет",
                "provider_event_id": "evt-1",
            }
        ),
        content_type="application/json",
        HTTP_X_WEBHOOK_TOKEN="secret-token",
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "Здравствуйте!"
    assert Client.objects.filter(business=business, phone="+77071234567").exists()
    assert InboundEvent.objects.filter(provider_event_id="evt-1").exists()


@pytest.mark.django_db
@override_settings(WEBHOOK_SHARED_SECRET="secret-token")
def test_webhook_deduplicates_inbound_event(client, business, monkeypatch):
    def fake_handle_text_message(**kwargs):
        return {"reply": "Здравствуйте!", "escalated": False}

    monkeypatch.setattr(
        "apps.bookings.views.handle_text_message",
        fake_handle_text_message,
    )

    payload = {
        "business_id": business.id,
        "channel": "whatsapp",
        "external_id": "wa-1",
        "phone": "+77071234567",
        "name": "Adil",
        "text": "Привет",
        "provider_event_id": "evt-duplicate",
    }
    first = client.post(
        "/api/v1/webhooks/messenger/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_WEBHOOK_TOKEN="secret-token",
    )
    second = client.post(
        "/api/v1/webhooks/messenger/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_WEBHOOK_TOKEN="secret-token",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert InboundEvent.objects.filter(provider_event_id="evt-duplicate").count() == 1


@pytest.mark.django_db
@override_settings(WEBHOOK_SHARED_SECRET="secret-token")
def test_webhook_accepts_voice_message(client, business, monkeypatch):
    def fake_handle_audio_message(**kwargs):
        return {
            "reply": "Сәлем!",
            "transcript": "Салем",
            "escalated": False,
        }

    monkeypatch.setattr(
        "apps.bookings.views.handle_audio_message",
        fake_handle_audio_message,
    )

    audio = SimpleUploadedFile(
        "voice.ogg",
        b"voice-bytes",
        content_type="audio/ogg",
    )
    response = client.post(
        "/api/v1/webhooks/messenger/",
        data={
            "business_id": str(business.id),
            "channel": "whatsapp",
            "external_id": "wa-2",
            "phone": "+77071234568",
            "name": "Aruzhan",
            "audio": audio,
            "provider_event_id": "evt-voice",
        },
        HTTP_X_WEBHOOK_TOKEN="secret-token",
    )

    assert response.status_code == 200
    assert response.json()["transcript"] == "Салем"


@pytest.mark.django_db
@override_settings(TELEGRAM_WEBHOOK_SECRET="tg-secret-123")
def test_telegram_webhook_requires_secret(client, business, monkeypatch):
    def fake_handle_text_message(**kwargs):
        return {"reply": "Здравствуйте!", "escalated": False}

    monkeypatch.setattr(
        "apps.bookings.views.handle_text_message",
        fake_handle_text_message,
    )

    response = client.post(
        "/api/v1/webhooks/telegram/tg-secret-123/",
        data=json.dumps(
            {
                "business_id": business.id,
                "external_id": "tg-1",
                "phone": "+77070000003",
                "name": "Telegram User",
                "text": "Сәлем",
                "provider_event_id": "evt-tg",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(
    GREEN_API_SHARED_SECRET="green-secret",
    GREEN_API_ALLOWED_IPS=["127.0.0.1"],
)
def test_green_api_webhook_checks_secret_and_ip(client, business, monkeypatch):
    def fake_handle_text_message(**kwargs):
        return {"reply": "Здравствуйте!", "escalated": False}

    monkeypatch.setattr(
        "apps.bookings.views.handle_text_message",
        fake_handle_text_message,
    )

    response = client.post(
        "/api/v1/webhooks/green-api/",
        data=json.dumps(
            {
                "business_id": business.id,
                "external_id": "wa-green",
                "phone": "+77070000004",
                "name": "Green User",
                "text": "Привет",
                "provider_event_id": "evt-green",
            }
        ),
        content_type="application/json",
        HTTP_X_GREENAPI_SECRET="green-secret",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(
    GREEN_API_SHARED_SECRET="green-secret",
    GREEN_API_ALLOWED_IPS=["127.0.0.1"],
)
def test_whatsapp_webhook_normalizes_green_api_payload(client, business, monkeypatch):
    def fake_handle_text_message(**kwargs):
        assert kwargs["text"] == "Привет из WhatsApp"
        return {"reply": "Здравствуйте!", "escalated": False}

    monkeypatch.setattr(
        "apps.bookings.views.handle_text_message",
        fake_handle_text_message,
    )

    response = client.post(
        f"/api/v1/webhooks/whatsapp/{business.id}/",
        data=json.dumps(
            {
                "idMessage": "wamid-123",
                "senderData": {
                    "chatId": "77070000004@c.us",
                    "senderName": "Green User",
                },
                "messageData": {
                    "typeMessage": "textMessage",
                    "textMessageData": {
                        "textMessage": "Привет из WhatsApp",
                    },
                },
            }
        ),
        content_type="application/json",
        HTTP_X_GREENAPI_SECRET="green-secret",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    assert Client.objects.filter(
        business=business,
        phone="+77070000004",
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("raw_phone", "expected_phone"),
    [
        ("87070000011", "+77070000011"),
        ("77070000011", "+77070000011"),
        ("+77070000011", "+77070000011"),
    ],
)
def test_client_identity_resolver_normalizes_kz_phone(
    business,
    raw_phone,
    expected_phone,
):
    resolved = ClientIdentityResolver().resolve_or_create(
        business=business,
        channel=ConversationMessage.Channel.WHATSAPP,
        phone=raw_phone,
        external_id=f"wa-{raw_phone}",
        name="Normalized client",
    )

    assert str(resolved.phone) == expected_phone


@pytest.mark.django_db
@override_settings(
    GREEN_API_SHARED_SECRET="green-secret",
    GREEN_API_ALLOWED_IPS=["127.0.0.1"],
)
def test_whatsapp_webhook_returns_friendly_reply_for_media_callback(
    client,
    business,
):
    response = client.post(
        f"/api/v1/webhooks/whatsapp/{business.id}/",
        data=json.dumps(
            {
                "idMessage": "wamid-image-1",
                "senderData": {
                    "chatId": "87070000012@c.us",
                    "senderName": "Media User",
                },
                "messageData": {
                    "typeMessage": "imageMessage",
                },
            }
        ),
        content_type="application/json",
        HTTP_X_GREENAPI_SECRET="green-secret",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    assert "только текстовые сообщения" in response.json()["reply"]


@pytest.mark.django_db
def test_ai_manager_includes_business_ai_rules(business):
    business.ai_rules = {
        "rules": [
            "Never book forbidden service combinations.",
            "Offer an alternative if a business rule blocks the request.",
        ]
    }
    business.save(update_fields=["ai_rules", "updated_at"])

    ai_manager = AIManager(business=business, client=object(), model="test-model")
    system_prompt = ai_manager.build_messages(
        [{"role": "user", "content": "Hello"}]
    )[0]["content"]

    assert "Индивидуальные правила бизнеса" in system_prompt
    assert "Never book forbidden service combinations." in system_prompt


@pytest.mark.django_db
def test_service_can_belong_to_generic_category(business):
    category = Category.objects.create(
        business=business,
        name="Universal Services",
        description="Reusable category for any business domain.",
    )
    categorized_service = Service.objects.create(
        business=business,
        category=category,
        name="Consultation",
        price=Decimal("10.00"),
        duration=timedelta(minutes=30),
    )

    assert categorized_service.category == category
    assert categorized_service.category.name == "Universal Services"


@pytest.mark.django_db
def test_service_rejects_category_from_another_business(
    business,
    another_business,
):
    foreign_category = Category.objects.create(
        business=another_business,
        name="Foreign category",
    )

    with pytest.raises(ValidationError):
        Service.objects.create(
            business=business,
            category=foreign_category,
            name="Consultation",
            price=Decimal("10.00"),
            duration=timedelta(minutes=30),
        )


@pytest.mark.django_db
def test_process_pending_reminders_queues_due_tasks(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    reminder_booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1, minutes=30),
        client_data={"name": client_profile.name},
        status=Booking.Status.CONFIRMED,
    )
    follow_up_booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
        status=Booking.Status.PENDING,
    )
    follow_up_booking.created_at = timezone.now() - timedelta(hours=1, minutes=5)
    follow_up_booking.save(update_fields=["created_at", "updated_at"])

    queued_calls = []

    def fake_delay(booking_id):
        queued_calls.append(booking_id)

    monkeypatch.setattr(
        "apps.bookings.tasks.send_booking_reminder.delay",
        fake_delay,
    )
    monkeypatch.setattr(
        "apps.bookings.tasks.send_follow_up_if_pending.delay",
        fake_delay,
    )

    result = process_pending_reminders()

    assert result["reminders_queued"] == 1
    assert result["follow_ups_queued"] == 1
    assert reminder_booking.id in queued_calls
    assert follow_up_booking.id in queued_calls


@pytest.mark.django_db
def test_send_booking_reminder_uses_whatsapp_when_only_phone_exists(
    business,
    master,
    service,
    monkeypatch,
):
    client_with_phone = Client.objects.create(
        business=business,
        name="Phone only",
        phone="+77079999999",
    )
    booking = Booking.objects.create(
        business=business,
        client=client_with_phone,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(hours=1, minutes=30),
        client_data={"name": client_with_phone.name},
        status=Booking.Status.CONFIRMED,
    )
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: AcceptingTransport(),
    )

    result = send_booking_reminder.run(booking.id)
    outbound_message = OutboundMessage.objects.get(
        booking=booking,
        message_type="reminder",
    )

    assert result["status"] == OutboundMessage.Status.SUBMITTED
    assert outbound_message.channel == "whatsapp"
    assert outbound_message.recipient == "+77079999999"
