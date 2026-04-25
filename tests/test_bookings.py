import json
import zoneinfo
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import override_settings
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.api.mixins import BusinessContextMixin, BusinessScopedQuerysetMixin
from apps.api.permissions import (
    ROLE_HIERARCHY,
    BusinessAccessPermission,
    _roles_gte,
)
from apps.bookings.admin import (
    BookingAdmin,
    BusinessAdmin,
    OutboundMessageAdmin,
    ServiceAdmin,
)
from apps.api.serializers import (
    BookingCreateSerializer,
    BookingReadSerializer,
    BookingRescheduleSerializer,
    BookingStatusUpdateSerializer,
)
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
    process_outbound_health_alerts,
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


class InternalAlertAcceptingTransport:
    def __init__(self):
        self.calls = []

    def send_text(self, *, recipient, text, metadata=None):
        self.calls.append(
            {
                "recipient": recipient,
                "text": text,
                "metadata": metadata or {},
            }
        )
        return SendResult(
            accepted=True,
            delivered=False,
            provider_message_id="internal-alert-1",
            raw_response={"recipient": recipient, "metadata": metadata or {}},
        )


def obtain_access_token(client, *, username="owner", password="StrongPass123!"):
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": username, "password": password}
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["access"]


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
        business,
        target_date=monday,
        service_id=service.id,
    )

    slot_starts = {(slot.start.hour, slot.start.minute) for slot in slots}
    assert (10, 0) not in slot_starts
    assert (10, 30) not in slot_starts
    assert (11, 0) not in slot_starts
    assert (11, 30) in slot_starts


@pytest.mark.django_db
def test_get_available_slots_rejects_foreign_master_id(
    business,
    another_business,
    service,
):
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign Master",
        specialization="Stylist",
        is_active=True,
    )

    with pytest.raises(ValidationError):
        get_available_slots(
            business,
            target_date=timezone.localdate() + timedelta(days=1),
            service_id=service.id,
            master_id=foreign_master.id,
        )


@pytest.mark.django_db
def test_get_available_slots_applies_business_rules_for_blocked_master_service_pair(
    business,
    master,
    service,
):
    business.ai_rules = {
        "blocked_master_service_pairs": [
            {"master_id": master.id, "service_id": service.id}
        ]
    }
    business.save(update_fields=["ai_rules", "updated_at"])

    with pytest.raises(
        ValidationError,
        match="This master cannot be booked for the selected service.",
    ):
        get_available_slots(
            business,
            target_date=timezone.localdate() + timedelta(days=1),
            service_id=service.id,
            master_id=master.id,
        )


@pytest.mark.django_db
def test_get_available_slots_filters_past_slots_using_business_timezone(
    business,
    master,
    service,
    monkeypatch,
):
    business.timezone_name = "Asia/Almaty"
    business.save(update_fields=["timezone_name", "updated_at"])
    target_date = date(2026, 4, 27)
    fixed_now = datetime(2026, 4, 27, 5, 15, tzinfo=zoneinfo.ZoneInfo("UTC"))

    monkeypatch.setattr("apps.bookings.services.timezone.now", lambda: fixed_now)

    slots = get_available_slots(
        business,
        target_date=target_date,
        service_id=service.id,
        master_id=master.id,
    )

    slot_starts = {(slot.start.hour, slot.start.minute) for slot in slots}
    assert (9, 0) not in slot_starts
    assert (9, 30) not in slot_starts
    assert (10, 0) not in slot_starts
    assert (10, 30) in slot_starts


@pytest.mark.django_db
def test_create_appointment_persists_booking_for_business_id(
    business,
    client_profile,
    master,
    service,
):
    start_time = timezone.now() + timedelta(days=2)
    booking = create_appointment(
        business=business,
        master=master,
        service=service,
        client=client_profile,
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

    with pytest.raises(
        ValidationError,
        match="Client does not belong to the selected business",
    ):
        create_appointment(
            business=business,
            master=master,
            service=service,
            client=foreign_client,
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
def test_ai_manager_overrides_tool_payload_scope(
    business,
    client_profile,
    another_business,
    monkeypatch,
):
    captured_payload = {}

    def fake_execute_ai_function(*, function_name, payload):
        captured_payload.update(payload)
        return {"ok": True}

    monkeypatch.setattr(
        "apps.bookings.ai_manager.execute_ai_function",
        fake_execute_ai_function,
    )

    ai_manager = AIManager(
        business=business,
        client=client_profile,
        model="test-model",
    )
    ai_manager.execute_tool_call(
        function_name="create_appointment",
        payload={
            "business_id": another_business.id,
            "client_id": 999999,
            "master_id": 1,
            "service_id": 1,
            "start_time": (timezone.now() + timedelta(days=1)).isoformat(),
            "client_data": {},
        },
    )

    assert captured_payload["business_id"] == business.id
    assert captured_payload["client_id"] == client_profile.id


@pytest.mark.django_db
def test_create_appointment_rejects_past_time_explicitly(
    business,
    client_profile,
    master,
    service,
):
    with pytest.raises(ValidationError):
        create_appointment(
            business=business,
            master=master,
            service=service,
            client=client_profile,
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
        business=business,
        master=master,
        service=service,
        client=client_profile,
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
def test_scheduled_outbound_message_is_unique_per_booking_and_type(
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
    OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        booking=booking,
        channel="whatsapp",
        recipient=str(client_profile.phone),
        message_type="reminder",
        text="Reminder",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            OutboundMessage.objects.create(
                business=business,
                client=client_profile,
                booking=booking,
                channel="whatsapp",
                recipient=str(client_profile.phone),
                message_type="reminder",
                text="Duplicate reminder",
            )


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
@override_settings(
    OUTBOUND_CALLBACK_SECRET="callback-secret",
    CELERY_TASK_ALWAYS_EAGER=True,
)
def test_outbound_delivery_webhook_marks_provider_failure(
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
        provider_message_id="provider-failed-123",
        provider_response={"accepted": True},
        submitted_at=timezone.now(),
        attempts=1,
    )

    response = client.post(
        "/api/v1/webhooks/outbound-delivery/",
        data=json.dumps(
            {
                "provider_message_id": "provider-failed-123",
                "status": "failed",
                "error_code": "recipient_unreachable",
                "error_message": "Recipient is not reachable.",
            }
        ),
        content_type="application/json",
        HTTP_X_OUTBOUND_CALLBACK_SECRET="callback-secret",
    )

    outbound_message.refresh_from_db()

    assert response.status_code == 200
    assert outbound_message.status == OutboundMessage.Status.FAILED
    assert outbound_message.error_code == "recipient_unreachable"
    assert outbound_message.provider_response["delivery_callback"]["status"] == "failed"


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
def test_roles_gte_returns_expected_hierarchy():
    assert ROLE_HIERARCHY == {
        BusinessMembership.Role.STAFF: 0,
        BusinessMembership.Role.ADMIN: 1,
        BusinessMembership.Role.OWNER: 2,
    }
    assert _roles_gte(BusinessMembership.Role.STAFF) == [
        BusinessMembership.Role.STAFF,
        BusinessMembership.Role.ADMIN,
        BusinessMembership.Role.OWNER,
    ]
    assert _roles_gte(BusinessMembership.Role.ADMIN) == [
        BusinessMembership.Role.ADMIN,
        BusinessMembership.Role.OWNER,
    ]
    assert _roles_gte(BusinessMembership.Role.OWNER) == [
        BusinessMembership.Role.OWNER,
    ]


@pytest.mark.django_db
def test_business_access_permission_factory_rejects_unknown_role():
    with pytest.raises(ValueError, match="Unknown business role"):
        BusinessAccessPermission("super_admin")


@pytest.mark.django_db
def test_business_access_permission_checks_membership_and_role(
    owner_user,
    business,
    business_membership,
):
    request = APIRequestFactory().get("/api/v1/businesses/1/bookings/")
    request.user = owner_user
    view = SimpleNamespace(business=business)

    assert BusinessAccessPermission(BusinessMembership.Role.STAFF)().has_permission(
        request,
        view,
    )
    assert BusinessAccessPermission(BusinessMembership.Role.ADMIN)().has_permission(
        request,
        view,
    )
    assert BusinessAccessPermission(BusinessMembership.Role.OWNER)().has_permission(
        request,
        view,
    )


@pytest.mark.django_db
def test_business_access_permission_denies_without_business_context(
    owner_user,
):
    request = APIRequestFactory().get("/api/v1/bookings/")
    request.user = owner_user
    view = SimpleNamespace(business=None)

    assert not BusinessAccessPermission()().has_permission(request, view)


@pytest.mark.django_db
def test_business_access_permission_denies_unauthenticated_request(
    business,
):
    request = APIRequestFactory().get("/api/v1/businesses/1/bookings/")
    request.user = AnonymousUser()
    view = SimpleNamespace(business=business)

    assert not BusinessAccessPermission()().has_permission(request, view)


@pytest.mark.django_db
def test_business_access_permission_checks_object_business_id(
    owner_user,
    business,
    business_membership,
    another_business,
):
    request = APIRequestFactory().get("/api/v1/businesses/1/bookings/1/")
    request.user = owner_user
    view = SimpleNamespace(business=business)

    assert BusinessAccessPermission()().has_object_permission(
        request,
        view,
        SimpleNamespace(business_id=business.id),
    )
    assert not BusinessAccessPermission()().has_object_permission(
        request,
        view,
        SimpleNamespace(business_id=another_business.id),
    )


@pytest.mark.django_db
def test_business_access_permission_uses_view_object_business_id_adapter(
    owner_user,
    business,
    business_membership,
    another_business,
):
    request = APIRequestFactory().get("/api/v1/businesses/1/outbound/1/")
    request.user = owner_user

    class StubView:
        def __init__(self, business):
            self.business = business

        @staticmethod
        def get_object_business_id(obj):
            return obj.target_business_id

    view = StubView(business)

    assert BusinessAccessPermission()().has_object_permission(
        request,
        view,
        SimpleNamespace(target_business_id=business.id),
    )
    assert not BusinessAccessPermission()().has_object_permission(
        request,
        view,
        SimpleNamespace(target_business_id=another_business.id),
    )


@pytest.mark.django_db
def test_business_context_mixin_resolves_business_before_permissions(
    owner_user,
    business,
):
    class StubView(BusinessContextMixin, APIView):
        permission_classes = [AllowAny]

    django_request = APIRequestFactory().get(
        f"/api/v1/businesses/{business.id}/bookings/"
    )
    django_request.user = owner_user

    view = StubView()
    request = view.initialize_request(django_request)
    view.request = request
    view.args = ()
    view.kwargs = {"business_id": business.id}

    view.initial(request)

    assert view.business == business


@pytest.mark.django_db
def test_business_context_mixin_skips_resolution_for_unauthenticated_request():
    class StubView(BusinessContextMixin, APIView):
        permission_classes = [AllowAny]

        def resolve_business(self):
            raise AssertionError("resolve_business should not be called")

    django_request = APIRequestFactory().get("/api/v1/businesses/1/bookings/")
    django_request.user = AnonymousUser()

    view = StubView()
    request = view.initialize_request(django_request)
    view.request = request
    view.args = ()
    view.kwargs = {"business_id": 1}

    view.initial(request)

    assert view.business is None


@pytest.mark.django_db
def test_business_context_mixin_requires_business_scope_in_url(
    owner_user,
):
    class StubView(BusinessContextMixin, APIView):
        permission_classes = [AllowAny]

    django_request = APIRequestFactory().get("/api/v1/bookings/")
    django_request.user = owner_user

    view = StubView()
    request = view.initialize_request(django_request)
    view.request = request
    view.args = ()
    view.kwargs = {}

    with pytest.raises(Http404, match="Business scope is required"):
        view.initial(request)


@pytest.mark.django_db
def test_business_scoped_queryset_mixin_uses_queryset_attribute(
    business,
    another_business,
    client_profile,
    master,
    service,
):
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign client",
        phone="+77070000009",
    )
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign master",
        specialization="Foreign",
        working_hours=master.working_hours,
    )
    foreign_service = Service.objects.create(
        business=another_business,
        name="Foreign service",
        price=Decimal("50.00"),
        duration=timedelta(minutes=30),
    )
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
    )
    Booking.objects.create(
        business=another_business,
        client=foreign_client,
        master=foreign_master,
        service=foreign_service,
        start_time=timezone.now() + timedelta(days=1, hours=1),
        client_data={"name": foreign_client.name},
    )

    class StubView(BusinessScopedQuerysetMixin):
        queryset = Booking.objects.select_related(
            "business",
            "client",
            "master",
            "service",
        )

    view = StubView()
    view.business = business

    queryset = view.get_queryset()

    assert list(queryset) == [booking]
    assert queryset.query.select_related


@pytest.mark.django_db
def test_booking_read_serializer_uses_explicit_id_fields(
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
        notes="Need a reminder",
    )

    payload = BookingReadSerializer(booking).data

    assert payload["business_id"] == business.id
    assert payload["client_id"] == client_profile.id
    assert payload["master_id"] == master.id
    assert payload["service_id"] == service.id
    assert "business" not in payload
    assert "client" not in payload
    assert "master" not in payload
    assert "service" not in payload


@pytest.mark.django_db
def test_booking_create_serializer_scopes_related_fields_to_business(
    business,
    client_profile,
    master,
    service,
):
    serializer = BookingCreateSerializer(
        context={"business": business},
    )

    assert list(serializer.fields["client"].queryset) == [client_profile]
    assert list(serializer.fields["master"].queryset) == [master]
    assert list(serializer.fields["service"].queryset) == [service]


@pytest.mark.django_db
def test_booking_create_serializer_rejects_foreign_master(
    business,
    client_profile,
    service,
    another_business,
):
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign master",
        specialization="Foreign",
        working_hours={"mon": {"start": "09:00", "end": "18:00"}},
    )

    serializer = BookingCreateSerializer(
        data={
            "client": client_profile.id,
            "master": foreign_master.id,
            "service": service.id,
            "start_time": (timezone.now() + timedelta(days=1)).isoformat(),
        },
        context={"business": business},
    )

    assert not serializer.is_valid()
    assert "master" in serializer.errors


@pytest.mark.django_db
def test_booking_create_serializer_ignores_business_from_payload(
    business,
    client_profile,
    master,
    service,
    another_business,
):
    serializer = BookingCreateSerializer(
        data={
            "business": another_business.id,
            "client": client_profile.id,
            "master": master.id,
            "service": service.id,
            "start_time": (timezone.now() + timedelta(days=1)).isoformat(),
        },
        context={"business": business},
    )

    assert serializer.is_valid(), serializer.errors
    assert "business" not in serializer.validated_data


@pytest.mark.django_db
def test_booking_create_serializer_uses_business_from_context(
    business,
    client_profile,
    master,
    service,
    another_business,
    monkeypatch,
):
    captured_call = {}

    def fake_create_appointment(
        *,
        business,
        client,
        master,
        service,
        start_time,
        client_data,
        status,
        notes,
    ):
        captured_call.update(
            {
                "business": business,
                "client": client,
                "master": master,
                "service": service,
                "start_time": start_time,
                "client_data": client_data,
                "status": status,
                "notes": notes,
            }
        )
        return Booking.objects.create(
            business=business,
            client=client_profile,
            master=master,
            service=service,
            start_time=start_time,
            client_data=client_data,
            status=status,
            notes=notes,
        )

    monkeypatch.setattr(
        "apps.api.serializers.create_appointment",
        fake_create_appointment,
    )

    serializer = BookingCreateSerializer(
        data={
            "business": another_business.id,
            "client": client_profile.id,
            "master": master.id,
            "service": service.id,
            "start_time": (timezone.now() + timedelta(days=1)).isoformat(),
            "client_data": {"name": client_profile.name},
            "notes": "Window seat",
        },
        context={"business": business},
    )

    assert serializer.is_valid(), serializer.errors
    booking = serializer.save()

    assert captured_call["business"] == business
    assert captured_call["client"] == client_profile
    assert captured_call["master"] == master
    assert captured_call["service"] == service
    assert captured_call["notes"] == "Window seat"
    assert booking.notes == "Window seat"


@pytest.mark.django_db
def test_booking_reschedule_serializer_scopes_master_queryset_to_business(
    business,
    master,
):
    serializer = BookingRescheduleSerializer(context={"business": business})

    assert list(serializer.fields["master"].queryset) == [master]


@pytest.mark.django_db
def test_booking_reschedule_serializer_rejects_foreign_master(
    business,
    client_profile,
    master,
    service,
    another_business,
):
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign master",
        specialization="Foreign",
        working_hours={"mon": {"start": "09:00", "end": "18:00"}},
    )
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
    )

    serializer = BookingRescheduleSerializer(
        instance=booking,
        data={
            "master": foreign_master.id,
            "start_time": (timezone.now() + timedelta(days=2)).isoformat(),
        },
        context={"business": business},
        partial=True,
    )

    assert not serializer.is_valid()
    assert "master" in serializer.errors


@pytest.mark.django_db
def test_booking_reschedule_serializer_uses_business_from_context(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
    )
    captured_call = {}
    new_start_time = timezone.now() + timedelta(days=2)

    def fake_reschedule_appointment(*, booking, business, master, start_time):
        captured_call.update(
            {
                "booking": booking,
                "business": business,
                "master": master,
                "start_time": start_time,
            }
        )
        booking.start_time = start_time
        return booking

    monkeypatch.setattr(
        "apps.api.serializers.reschedule_appointment",
        fake_reschedule_appointment,
    )

    serializer = BookingRescheduleSerializer(
        instance=booking,
        data={
            "master": master.id,
            "start_time": new_start_time.isoformat(),
        },
        context={"business": business},
        partial=True,
    )

    assert serializer.is_valid(), serializer.errors
    updated_booking = serializer.save()

    assert captured_call["booking"] == booking
    assert captured_call["business"] == business
    assert captured_call["master"] == master
    assert captured_call["start_time"] == serializer.validated_data["start_time"]
    assert updated_booking.start_time == serializer.validated_data["start_time"]


@pytest.mark.django_db
def test_booking_status_update_serializer_uses_business_from_context(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
    )
    captured_call = {}

    def fake_update_booking_status(*, booking, business, status):
        captured_call.update(
            {
                "booking": booking,
                "business": business,
                "status": status,
            }
        )
        booking.status = status
        return booking

    monkeypatch.setattr(
        "apps.api.serializers.update_booking_status",
        fake_update_booking_status,
    )

    serializer = BookingStatusUpdateSerializer(
        instance=booking,
        data={"status": Booking.Status.CONFIRMED},
        context={"business": business},
        partial=True,
    )

    assert serializer.is_valid(), serializer.errors
    updated_booking = serializer.save()

    assert captured_call["booking"] == booking
    assert captured_call["business"] == business
    assert captured_call["status"] == Booking.Status.CONFIRMED
    assert updated_booking.status == Booking.Status.CONFIRMED


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
        f"/api/v1/businesses/{business.id}/bookings/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert bookings_response.status_code == 200
    payload = bookings_response.json()
    assert payload["count"] == 1
    assert payload["next"] is None
    assert payload["previous"] is None
    assert payload["results"][0]["id"] == booking.id


@pytest.mark.django_db
def test_bookings_api_denies_access_to_foreign_business_scope(
    client,
    owner_user,
    business_membership,
    another_business,
):
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]

    response = client.get(
        f"/api/v1/businesses/{another_business.id}/bookings/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_bookings_api_creates_booking_in_business_scope(
    client,
    owner_user,
    business,
    business_membership,
    client_profile,
    master,
    service,
):
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]
    start_time = timezone.now() + timedelta(days=1)

    response = client.post(
        f"/api/v1/businesses/{business.id}/bookings/",
        data=json.dumps(
            {
                "business": 999999,
                "client": client_profile.id,
                "master": master.id,
                "service": service.id,
                "start_time": start_time.isoformat(),
                "client_data": {"name": client_profile.name},
                "notes": "Window seat",
            }
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["business_id"] == business.id
    assert payload["client_id"] == client_profile.id
    assert payload["master_id"] == master.id
    assert payload["service_id"] == service.id
    assert payload["notes"] == "Window seat"


@pytest.mark.django_db
def test_booking_detail_api_returns_booking_within_business_scope(
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
        notes="Detail test",
    )
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]

    response = client.get(
        f"/api/v1/businesses/{business.id}/bookings/{booking.id}/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 200
    assert response.json()["id"] == booking.id
    assert response.json()["notes"] == "Detail test"


@pytest.mark.django_db
def test_booking_detail_api_returns_404_for_foreign_booking_pk_in_same_scope(
    client,
    owner_user,
    business,
    business_membership,
    another_business,
):
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign client",
        phone="+77070000011",
    )
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign master",
        specialization="Foreign",
        working_hours={"mon": {"start": "09:00", "end": "18:00"}},
    )
    foreign_service = Service.objects.create(
        business=another_business,
        name="Foreign service",
        price=Decimal("50.00"),
        duration=timedelta(minutes=30),
    )
    foreign_booking = Booking.objects.create(
        business=another_business,
        client=foreign_client,
        master=foreign_master,
        service=foreign_service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": foreign_client.name},
    )
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]

    response = client.get(
        f"/api/v1/businesses/{business.id}/bookings/{foreign_booking.id}/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_booking_reschedule_api_updates_booking_within_business_scope(
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
    )
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]
    new_start_time = timezone.now() + timedelta(days=2)

    response = client.patch(
        f"/api/v1/businesses/{business.id}/bookings/{booking.id}/reschedule/",
        data=json.dumps(
            {
                "master": master.id,
                "start_time": new_start_time.isoformat(),
            }
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    booking.refresh_from_db()

    assert response.status_code == 200
    assert timezone.localtime(booking.start_time).isoformat() == response.json()["start_time"]
    assert booking.master_id == master.id


@pytest.mark.django_db
def test_booking_status_api_updates_status_within_business_scope(
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
        status=Booking.Status.PENDING,
    )
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]

    response = client.patch(
        f"/api/v1/businesses/{business.id}/bookings/{booking.id}/status/",
        data=json.dumps({"status": Booking.Status.CONFIRMED}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    booking.refresh_from_db()

    assert response.status_code == 200
    assert booking.status == Booking.Status.CONFIRMED
    assert response.json()["status"] == Booking.Status.CONFIRMED


@pytest.mark.django_db
def test_booking_reschedule_api_returns_404_for_foreign_booking_pk_in_same_scope(
    client,
    owner_user,
    business,
    business_membership,
    another_business,
):
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign client",
        phone="+77070000012",
    )
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign master",
        specialization="Foreign",
        working_hours={"mon": {"start": "09:00", "end": "18:00"}},
    )
    foreign_service = Service.objects.create(
        business=another_business,
        name="Foreign service",
        price=Decimal("50.00"),
        duration=timedelta(minutes=30),
    )
    foreign_booking = Booking.objects.create(
        business=another_business,
        client=foreign_client,
        master=foreign_master,
        service=foreign_service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": foreign_client.name},
    )
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {"username": "owner", "password": "StrongPass123!"}
        ),
        content_type="application/json",
    )
    access_token = token_response.json()["access"]

    response = client.patch(
        f"/api/v1/businesses/{business.id}/bookings/{foreign_booking.id}/reschedule/",
        data=json.dumps(
            {
                "start_time": (timezone.now() + timedelta(days=2)).isoformat(),
            }
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 404


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
        lambda **kwargs: StubAIManager(reply="Принято"),
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

    service = Service(
        business=business,
        category=foreign_category,
        name="Consultation",
        price=Decimal("10.00"),
        duration=timedelta(minutes=30),
    )

    with pytest.raises(ValidationError):
        service.full_clean()


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
@override_settings(
    INTERNAL_ALERT_WEBHOOK_URL="https://alerts.example.test",
    OUTBOUND_ALERT_FAILED_THRESHOLD=2,
    OUTBOUND_ALERT_DEAD_LETTER_THRESHOLD=1,
    OUTBOUND_ALERT_LOOKBACK_MINUTES=60,
)
def test_process_outbound_health_alerts_sends_internal_alert(
    business,
    client_profile,
    master,
    service,
    monkeypatch,
):
    alert_transport = InternalAlertAcceptingTransport()
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: alert_transport,
    )
    monkeypatch.setattr(
        "apps.bookings.tasks.claim_outbound_alert_cooldown",
        lambda **kwargs: True,
    )

    base_time = timezone.now() - timedelta(minutes=10)
    failed_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        booking=Booking.objects.create(
            business=business,
            client=client_profile,
            master=master,
            service=service,
            start_time=timezone.now() + timedelta(days=1),
            client_data={"name": client_profile.name},
            status=Booking.Status.CONFIRMED,
        ),
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="failed-1",
        status=OutboundMessage.Status.FAILED,
    )
    second_failed_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="telegram",
        recipient="12345",
        message_type="follow_up",
        text="failed-2",
        status=OutboundMessage.Status.FAILED,
    )
    dead_letter_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="follow_up",
        text="dead-letter",
        status=OutboundMessage.Status.DEAD_LETTER,
        dead_lettered_at=timezone.now() - timedelta(minutes=5),
    )
    OutboundMessage.objects.filter(pk=failed_message.pk).update(updated_at=base_time)
    OutboundMessage.objects.filter(pk=second_failed_message.pk).update(
        updated_at=base_time
    )

    result = process_outbound_health_alerts()

    assert result["alerts_sent"] == 1
    assert len(alert_transport.calls) == 1
    assert alert_transport.calls[0]["metadata"]["failed_count"] == 2
    assert alert_transport.calls[0]["metadata"]["dead_letter_count"] == 1
    assert AuditLog.objects.filter(
        business=business,
        event_type="outbound_health_alert_sent",
    ).exists()
    dead_letter_message.refresh_from_db()
    assert dead_letter_message.dead_lettered_at is not None


@pytest.mark.django_db
@override_settings(
    INTERNAL_ALERT_WEBHOOK_URL="https://alerts.example.test",
    OUTBOUND_ALERT_FAILED_THRESHOLD=1,
    OUTBOUND_ALERT_DEAD_LETTER_THRESHOLD=1,
)
def test_process_outbound_health_alerts_respects_redis_cooldown(
    business,
    client_profile,
    monkeypatch,
):
    alert_transport = InternalAlertAcceptingTransport()
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: alert_transport,
    )
    monkeypatch.setattr(
        "apps.bookings.tasks.claim_outbound_alert_cooldown",
        lambda **kwargs: False,
    )

    outbound_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="failed",
        status=OutboundMessage.Status.FAILED,
    )
    OutboundMessage.objects.filter(pk=outbound_message.pk).update(
        updated_at=timezone.now() - timedelta(minutes=5)
    )

    result = process_outbound_health_alerts()

    assert result["alerts_sent"] == 0
    assert result["alerts_skipped"] == 1
    assert alert_transport.calls == []
    assert not AuditLog.objects.filter(
        business=business,
        event_type="outbound_health_alert_sent",
    ).exists()


@pytest.mark.django_db
@override_settings(
    INTERNAL_ALERT_WEBHOOK_URL="https://alerts.example.test",
    OUTBOUND_ALERT_FAILED_THRESHOLD=1,
    OUTBOUND_ALERT_DEAD_LETTER_THRESHOLD=1,
    OUTBOUND_ALERT_LOOKBACK_MINUTES=60,
)
def test_process_outbound_health_alerts_uses_delivery_timestamps_not_created_at(
    business,
    client_profile,
    monkeypatch,
):
    alert_transport = InternalAlertAcceptingTransport()
    monkeypatch.setattr(
        "apps.bookings.tasks.get_transport_for_channel",
        lambda channel: alert_transport,
    )
    monkeypatch.setattr(
        "apps.bookings.tasks.claim_outbound_alert_cooldown",
        lambda **kwargs: True,
    )

    stale_failed = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="old-created-new-failed",
        status=OutboundMessage.Status.FAILED,
    )
    stale_dead_letter = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="telegram",
        recipient="12345",
        message_type="follow_up",
        text="old-created-new-dead-letter",
        status=OutboundMessage.Status.DEAD_LETTER,
        dead_lettered_at=timezone.now() - timedelta(minutes=10),
    )
    old_timestamp = timezone.now() - timedelta(hours=3)
    OutboundMessage.objects.filter(pk__in=[stale_failed.pk, stale_dead_letter.pk]).update(
        created_at=old_timestamp
    )
    OutboundMessage.objects.filter(pk=stale_failed.pk).update(
        updated_at=timezone.now() - timedelta(minutes=10)
    )

    result = process_outbound_health_alerts()

    assert result["alerts_sent"] == 1
    assert len(alert_transport.calls) == 1
    assert alert_transport.calls[0]["metadata"]["failed_count"] == 1
    assert alert_transport.calls[0]["metadata"]["dead_letter_count"] == 1


@pytest.mark.django_db
def test_business_admin_scopes_queryset_to_membership(
    business,
    another_business,
    owner_user,
):
    BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
    )
    BusinessMembership.objects.create(
        user=owner_user,
        business=another_business,
        role=BusinessMembership.Role.STAFF,
    )
    request = APIRequestFactory().get("/secure-admin/bookings/business/")
    request.user = owner_user

    queryset = BusinessAdmin(Business, AdminSite()).get_queryset(request)

    assert list(queryset) == [business]


@pytest.mark.django_db
def test_booking_admin_scopes_queryset_and_permissions(
    business,
    another_business,
    owner_user,
    client_profile,
    master,
    service,
):
    request = APIRequestFactory().get("/secure-admin/bookings/booking/")
    request.user = owner_user
    BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
    )

    own_booking = Booking.objects.create(
        business=business,
        client=client_profile,
        master=master,
        service=service,
        start_time=timezone.now() + timedelta(days=1),
        client_data={"name": client_profile.name},
    )
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign",
        phone="+77070000077",
    )
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign Master",
        specialization="Stylist",
    )
    foreign_service = Service.objects.create(
        business=another_business,
        name="Foreign service",
        price=Decimal("20.00"),
        duration=timedelta(minutes=30),
    )
    foreign_booking = Booking.objects.create(
        business=another_business,
        client=foreign_client,
        master=foreign_master,
        service=foreign_service,
        start_time=timezone.now() + timedelta(days=2),
        client_data={"name": foreign_client.name},
    )

    admin_instance = BookingAdmin(Booking, AdminSite())
    queryset = admin_instance.get_queryset(request)

    assert list(queryset) == [own_booking]
    assert admin_instance.has_view_permission(request, own_booking) is True
    assert admin_instance.has_view_permission(request, foreign_booking) is False


@pytest.mark.django_db
def test_service_admin_limits_business_foreign_key_choices(
    business,
    another_business,
    owner_user,
):
    BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
    )
    own_category = Category.objects.create(
        business=business,
        name="Own category",
    )
    Category.objects.create(
        business=another_business,
        name="Foreign category",
    )
    request = APIRequestFactory().get("/secure-admin/bookings/service/add/")
    request.user = owner_user

    form_field = ServiceAdmin(Service, AdminSite()).formfield_for_foreignkey(
        Service._meta.get_field("category"),
        request,
    )

    assert list(form_field.queryset) == [own_category]


@pytest.mark.django_db
def test_booking_admin_mark_confirmed_action_updates_status_and_audit(
    business,
    owner_user,
    client_profile,
    master,
    service,
):
    BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
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
    request = APIRequestFactory().post("/secure-admin/bookings/booking/")
    request.user = owner_user

    messages_sent = []
    admin_instance = BookingAdmin(Booking, AdminSite())
    admin_instance.message_user = (
        lambda request, message, level=messages.INFO: messages_sent.append(
            (message, level)
        )
    )

    admin_instance.mark_confirmed(request, Booking.objects.filter(pk=booking.pk))
    booking.refresh_from_db()

    assert booking.status == Booking.Status.CONFIRMED
    assert AuditLog.objects.filter(
        booking=booking,
        event_type="admin_booking_status_action",
    ).exists()
    assert messages_sent[0][0] == "1 booking(s) marked as confirmed."


@pytest.mark.django_db
def test_outbound_message_admin_retry_only_dispatches_failed_messages(
    business,
    owner_user,
    client_profile,
    monkeypatch,
):
    BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
    )
    failed_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="follow_up",
        text="retry me",
        status=OutboundMessage.Status.FAILED,
    )
    delivered_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="already delivered",
        status=OutboundMessage.Status.DELIVERED,
    )
    request = APIRequestFactory().post("/secure-admin/bookings/outboundmessage/")
    request.user = owner_user
    dispatched_ids = []
    messages_sent = []

    monkeypatch.setattr(
        "apps.bookings.tasks.dispatch_outbound_delivery",
        lambda outbound_message_id: dispatched_ids.append(outbound_message_id),
    )

    admin_instance = OutboundMessageAdmin(OutboundMessage, AdminSite())
    admin_instance.message_user = (
        lambda request, message, level=messages.INFO: messages_sent.append(
            (message, level)
        )
    )

    admin_instance.retry_selected_messages(
        request,
        OutboundMessage.objects.filter(pk__in=[failed_message.pk, delivered_message.pk]),
    )

    assert dispatched_ids == [failed_message.id]
    assert AuditLog.objects.filter(
        outbound_message=failed_message,
        event_type="outbound_retry_requested",
    ).exists()
    assert messages_sent[0][0] == (
        "Queued retry for 1 outbound message(s). "
        "Skipped 1 non-failed message(s)."
    )


@pytest.mark.django_db
def test_outbound_message_admin_resend_resets_terminal_messages(
    business,
    owner_user,
    client_profile,
    monkeypatch,
):
    BusinessMembership.objects.create(
        user=owner_user,
        business=business,
        role=BusinessMembership.Role.OWNER,
    )
    outbound_message = OutboundMessage.objects.create(
        business=business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="follow_up",
        text="resend me",
        status=OutboundMessage.Status.DEAD_LETTER,
        attempts=3,
        error_code="timeout",
        last_error="provider timeout",
        provider_message_id="provider-old",
        provider_response={"old": True},
        submitted_at=timezone.now() - timedelta(hours=1),
        dead_lettered_at=timezone.now() - timedelta(minutes=5),
    )
    request = APIRequestFactory().post("/secure-admin/bookings/outboundmessage/")
    request.user = owner_user
    dispatched_ids = []

    monkeypatch.setattr(
        "apps.bookings.tasks.dispatch_outbound_delivery",
        lambda outbound_message_id: dispatched_ids.append(outbound_message_id),
    )

    admin_instance = OutboundMessageAdmin(OutboundMessage, AdminSite())
    admin_instance.message_user = lambda *args, **kwargs: None
    admin_instance.resend_selected_messages(
        request,
        OutboundMessage.objects.filter(pk=outbound_message.pk),
    )
    outbound_message.refresh_from_db()

    assert dispatched_ids == [outbound_message.id]
    assert outbound_message.status == OutboundMessage.Status.QUEUED
    assert outbound_message.attempts == 0
    assert outbound_message.error_code == ""
    assert outbound_message.last_error == ""
    assert outbound_message.provider_message_id == ""
    assert outbound_message.provider_response == {}
    assert outbound_message.submitted_at is None
    assert outbound_message.dead_lettered_at is None
    assert AuditLog.objects.filter(
        outbound_message=outbound_message,
        event_type="outbound_resend_requested",
    ).exists()


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


@pytest.mark.django_db
def test_business_detail_api_returns_scoped_business(
    client,
    business,
    business_membership,
):
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/",
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": business.id,
        "name": business.name,
        "brand_name": business.brand_name,
        "city": business.city,
        "address": business.address,
        "working_hours": business.working_hours,
        "timezone_name": business.timezone_name,
        "is_active": business.is_active,
    }


@pytest.mark.django_db
def test_business_detail_api_rejects_foreign_business_scope(
    client,
    business_membership,
    another_business,
):
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{another_business.id}/",
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_master_list_api_returns_only_active_business_masters(
    client,
    business,
    business_membership,
    another_business,
):
    active_master = Master.objects.create(
        business=business,
        full_name="Active Master",
        specialization="Barber",
        is_active=True,
    )
    Master.objects.create(
        business=business,
        full_name="Inactive Master",
        specialization="Stylist",
        is_active=False,
    )
    Master.objects.create(
        business=another_business,
        full_name="Foreign Master",
        specialization="Colorist",
        is_active=True,
    )
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/masters/",
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": active_master.id,
            "full_name": active_master.full_name,
            "specialization": active_master.specialization,
            "working_hours": active_master.working_hours,
            "is_active": True,
        }
    ]


@pytest.mark.django_db
def test_service_list_api_serializes_category_and_filters_inactive(
    client,
    business,
    business_membership,
):
    category = Category.objects.create(
        business=business,
        name="Cuts",
    )
    active_service = Service.objects.create(
        business=business,
        category=category,
        name="Buzz Cut",
        price=Decimal("15.00"),
        duration=timedelta(minutes=30),
        buffer_time=timedelta(minutes=10),
        is_active=True,
    )
    Service.objects.create(
        business=business,
        category=category,
        name="Hidden Service",
        price=Decimal("25.00"),
        duration=timedelta(minutes=45),
        is_active=False,
    )
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/services/",
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": active_service.id,
            "name": active_service.name,
            "category_id": category.id,
            "category_name": category.name,
            "price": "15.00",
            "duration": "00:30:00",
            "buffer_time": "00:10:00",
            "is_active": True,
        }
    ]


@pytest.mark.django_db
def test_client_list_api_filters_by_business_and_search(
    client,
    business,
    business_membership,
    another_business,
):
    matching_client = Client.objects.create(
        business=business,
        name="Aruzhan",
        phone="+77070001122",
        whatsapp_id="wa-match",
        telegram_id="tg-match",
        allow_follow_up=True,
        is_active=True,
    )
    Client.objects.create(
        business=business,
        name="Inactive Client",
        phone="+77070001123",
        is_active=False,
    )
    Client.objects.create(
        business=another_business,
        name="Foreign Match",
        phone="+77070001124",
        whatsapp_id="wa-match",
        is_active=True,
    )
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/clients/?search=wa-match",
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["count"] == 1
    assert payload["next"] is None
    assert payload["previous"] is None
    assert payload["results"][0]["id"] == matching_client.id
    assert payload["results"][0]["name"] == matching_client.name
    assert payload["results"][0]["phone"] == str(matching_client.phone)
    assert payload["results"][0]["telegram_id"] == matching_client.telegram_id
    assert payload["results"][0]["whatsapp_id"] == matching_client.whatsapp_id
    assert payload["results"][0]["allow_follow_up"] is True
    assert payload["results"][0]["is_active"] is True
    assert "created_at" in payload["results"][0]


@pytest.mark.django_db
def test_client_detail_api_returns_scoped_client(
    client,
    business_membership,
    client_profile,
):
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{client_profile.business_id}/clients/{client_profile.id}/"
    )

    assert response.status_code == 200
    assert response.json()["id"] == client_profile.id
    assert response.json()["name"] == client_profile.name
    assert response.json()["phone"] == str(client_profile.phone)
    assert response.json()["whatsapp_id"] == client_profile.whatsapp_id
    assert response.json()["ai_failure_count"] == client_profile.ai_failure_count
    assert "created_at" in response.json()
    assert "updated_at" in response.json()


@pytest.mark.django_db
def test_client_detail_api_returns_404_for_foreign_client_pk(
    client,
    business,
    business_membership,
    another_business,
):
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign Client",
        phone="+77070009999",
    )
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/clients/{foreign_client.id}/"
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_availability_api_returns_slots_for_business(
    client,
    business,
    business_membership,
    master,
    service,
):
    client.force_login(business_membership.user)
    days_until_monday = (7 - timezone.localdate().weekday()) % 7
    monday = timezone.localdate() + timedelta(days=days_until_monday)

    response = client.get(
        f"/api/v1/businesses/{business.id}/availability/",
        {
            "date": monday.isoformat(),
            "service_id": service.id,
            "master_id": master.id,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert {
        "start_time",
        "end_time",
        "master_id",
        "master_name",
    } <= set(payload[0].keys())
    assert payload[0]["master_id"] == master.id
    assert payload[0]["master_name"] == master.full_name


@pytest.mark.django_db
def test_availability_api_rejects_foreign_master_id(
    client,
    business,
    business_membership,
    another_business,
    service,
):
    foreign_master = Master.objects.create(
        business=another_business,
        full_name="Foreign Master",
        specialization="Stylist",
        is_active=True,
    )
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/availability/",
        {
            "date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            "service_id": service.id,
            "master_id": foreign_master.id,
        },
    )

    assert response.status_code == 400
    assert "Master does not belong to the selected business." in str(response.json())


@pytest.mark.django_db
def test_availability_api_validates_required_query_params(
    client,
    business,
    business_membership,
):
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business.id}/availability/",
        {"date": "not-a-date"},
    )

    assert response.status_code == 400
    assert "date" in response.json()
    assert "service_id" in response.json()


@pytest.mark.django_db
def test_availability_api_rejects_foreign_business_scope(
    client,
    business_membership,
    another_business,
    service,
):
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{another_business.id}/availability/",
        {
            "date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            "service_id": service.id,
        },
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_outbound_message_list_api_returns_paginated_results(
    client,
    business_membership,
    client_profile,
):
    outbound_message = OutboundMessage.objects.create(
        business=business_membership.business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="paging works",
        status=OutboundMessage.Status.FAILED,
    )
    client.force_login(business_membership.user)

    response = client.get(
        f"/api/v1/businesses/{business_membership.business_id}/outbound-messages/"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["next"] is None
    assert payload["previous"] is None
    assert payload["results"][0]["id"] == outbound_message.id
    assert payload["results"][0]["status"] == OutboundMessage.Status.FAILED


@pytest.mark.django_db
def test_outbound_retry_api_retries_failed_message(
    client,
    business_membership,
    client_profile,
    monkeypatch,
):
    outbound_message = OutboundMessage.objects.create(
        business=business_membership.business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="follow_up",
        text="retry me",
        status=OutboundMessage.Status.FAILED,
    )
    client.force_login(business_membership.user)
    monkeypatch.setattr(
        "apps.bookings.tasks.dispatch_outbound_delivery",
        lambda outbound_message_id: {
            "outbound_message_id": outbound_message_id,
            "status": OutboundMessage.Status.QUEUED,
            "delivery_task_id": "retry-task-1",
        },
    )

    response = client.post(
        f"/api/v1/businesses/{business_membership.business_id}/outbound-messages/{outbound_message.id}/retry/"
    )

    assert response.status_code == 200
    assert response.json()["id"] == outbound_message.id
    assert response.json()["status"] == OutboundMessage.Status.FAILED
    assert response.json()["delivery_status"] == OutboundMessage.Status.QUEUED
    assert response.json()["delivery_task_id"] == "retry-task-1"
    assert AuditLog.objects.filter(
        outbound_message=outbound_message,
        event_type="outbound_retry_requested",
    ).exists()


@pytest.mark.django_db
def test_outbound_retry_api_rejects_non_failed_message(
    client,
    business_membership,
    client_profile,
):
    outbound_message = OutboundMessage.objects.create(
        business=business_membership.business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="delivered already",
        status=OutboundMessage.Status.DELIVERED,
    )
    client.force_login(business_membership.user)

    response = client.post(
        f"/api/v1/businesses/{business_membership.business_id}/outbound-messages/{outbound_message.id}/retry/"
    )

    assert response.status_code == 400
    assert "Only failed outbound messages can be retried." in str(response.json())


@pytest.mark.django_db
def test_outbound_retry_api_returns_404_for_foreign_message_pk(
    client,
    business,
    business_membership,
    another_business,
):
    foreign_client = Client.objects.create(
        business=another_business,
        name="Foreign Client",
        phone="+77070008888",
    )
    foreign_message = OutboundMessage.objects.create(
        business=another_business,
        client=foreign_client,
        channel="whatsapp",
        recipient="+77070008888",
        message_type="follow_up",
        text="foreign retry",
        status=OutboundMessage.Status.FAILED,
    )
    client.force_login(business_membership.user)

    response = client.post(
        f"/api/v1/businesses/{business.id}/outbound-messages/{foreign_message.id}/retry/"
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_outbound_resend_api_resets_terminal_message_and_queues_delivery(
    client,
    business_membership,
    client_profile,
    monkeypatch,
):
    outbound_message = OutboundMessage.objects.create(
        business=business_membership.business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="follow_up",
        text="resend me",
        status=OutboundMessage.Status.DEAD_LETTER,
        attempts=3,
        error_code="timeout",
        last_error="provider timeout",
        provider_message_id="provider-old",
        provider_response={"old": True},
        submitted_at=timezone.now() - timedelta(hours=1),
        dead_lettered_at=timezone.now() - timedelta(minutes=10),
    )
    client.force_login(business_membership.user)
    monkeypatch.setattr(
        "apps.bookings.tasks.dispatch_outbound_delivery",
        lambda outbound_message_id: {
            "outbound_message_id": outbound_message_id,
            "status": OutboundMessage.Status.QUEUED,
            "delivery_task_id": "resend-task-1",
        },
    )

    response = client.post(
        f"/api/v1/businesses/{business_membership.business_id}/outbound-messages/{outbound_message.id}/resend/"
    )
    outbound_message.refresh_from_db()

    assert response.status_code == 200
    assert response.json()["status"] == OutboundMessage.Status.QUEUED
    assert response.json()["delivery_status"] == OutboundMessage.Status.QUEUED
    assert response.json()["delivery_task_id"] == "resend-task-1"
    assert outbound_message.attempts == 0
    assert outbound_message.error_code == ""
    assert outbound_message.last_error == ""
    assert outbound_message.provider_message_id == ""
    assert outbound_message.provider_response == {}
    assert outbound_message.submitted_at is None
    assert outbound_message.dead_lettered_at is None
    assert AuditLog.objects.filter(
        outbound_message=outbound_message,
        event_type="outbound_resend_requested",
    ).exists()


@pytest.mark.django_db
def test_outbound_resend_api_rejects_delivered_message(
    client,
    business_membership,
    client_profile,
):
    outbound_message = OutboundMessage.objects.create(
        business=business_membership.business,
        client=client_profile,
        channel="whatsapp",
        recipient="+77071234567",
        message_type="reminder",
        text="already delivered",
        status=OutboundMessage.Status.DELIVERED,
    )
    client.force_login(business_membership.user)

    response = client.post(
        f"/api/v1/businesses/{business_membership.business_id}/outbound-messages/{outbound_message.id}/resend/"
    )

    assert response.status_code == 400
    assert (
        "Only failed, dead-letter, or cancelled messages can be resent."
        in str(response.json())
    )


@pytest.mark.django_db
def test_api_schema_endpoint_returns_openapi_document(client):
    response = client.get("/api/v1/schema/?format=json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["openapi"].startswith("3.")
    assert payload["info"]["title"] == "AI-Admin Pro API"


@pytest.mark.django_db
def test_api_swagger_ui_endpoint_is_available(client):
    response = client.get("/api/v1/schema/swagger-ui/")

    assert response.status_code == 200
    assert "swagger-ui" in response.content.decode().lower()


@override_settings(CORS_ALLOWED_ORIGINS=["http://localhost:3000"])
@pytest.mark.django_db
def test_health_endpoint_allows_configured_cors_origin(client):
    response = client.get(
        "/api/v1/health/",
        HTTP_ORIGIN="http://localhost:3000",
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "http://localhost:3000"
    )
