from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from phonenumber_field.modelfields import PhoneNumberField


WEEKDAY_KEYS = (
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
)
MAX_CONVERSATION_MESSAGES = 15


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Business(TimeStampedModel):
    name = models.CharField(max_length=255)
    brand_name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, default="Алматы")
    working_hours = models.CharField(max_length=255, blank=True, default="")
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    ai_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "System prompt, model name, temperature and tool settings."
        ),
    )
    ai_rules = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Business-specific AI rules and restrictions."
        ),
    )
    knowledge_base = models.TextField(
        blank=True,
        help_text=_("Business-specific context for the AI assistant."),
    )
    timezone_name = models.CharField(max_length=64, default="Asia/Almaty")
    cancellation_policy_hours = models.PositiveSmallIntegerField(
        default=2,
        help_text=_(
            "Минимальный запас часов до начала записи, при котором клиент "
            "может отменить её через бота. Если до записи остаётся меньше "
            "этого порога — бот эскалирует запрос на администратора. "
            "0 = разрешать отмену в любое время."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = _("business")
        verbose_name_plural = _("businesses")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.build_unique_slug()
        return super().save(*args, **kwargs)

    def build_unique_slug(self):
        base_slug = slugify(self.name, allow_unicode=True) or "business"
        candidate = base_slug
        counter = 1
        while Business.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
            candidate = f"{base_slug}-{counter}"
            counter += 1
        return candidate

    def get_ai_setting(self, key, default=None):
        return self.ai_settings.get(key, default)

    @property
    def display_brand_name(self):
        return self.brand_name or self.name

    def get_ai_rules_list(self):
        if isinstance(self.ai_rules, dict):
            rules = self.ai_rules.get("rules", [])
            if isinstance(rules, list):
                return [str(rule).strip() for rule in rules if str(rule).strip()]
        if isinstance(self.ai_rules, list):
            return [str(rule).strip() for rule in self.ai_rules if str(rule).strip()]
        return []


class Master(TimeStampedModel):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="masters",
    )
    full_name = models.CharField(max_length=255)
    specialization = models.CharField(max_length=255)
    working_hours = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Weekly schedule by weekday, for example "
            "{'mon': {'start': '09:00', 'end': '18:00'}}."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("full_name",)
        verbose_name = _("master")
        verbose_name_plural = _("masters")
        constraints = [
            models.UniqueConstraint(
                fields=("business", "full_name"),
                name="uniq_master_name_per_business",
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.business.name})"

    def get_daily_schedule(self, target_date: date):
        weekday_index = target_date.weekday()
        weekday_key = WEEKDAY_KEYS[weekday_index]
        return self.working_hours.get(weekday_key) or self.working_hours.get(
            str(weekday_index)
        )


class Category(TimeStampedModel):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = _("category")
        verbose_name_plural = _("categories")
        constraints = [
            models.UniqueConstraint(
                fields=("business", "name"),
                name="uniq_category_name_per_business",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.business.name})"


class Service(TimeStampedModel):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="services",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        related_name="services",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    duration = models.DurationField()
    buffer_time = models.DurationField(default=timedelta())
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = _("service")
        verbose_name_plural = _("services")
        constraints = [
            models.UniqueConstraint(
                fields=("business", "name"),
                name="uniq_service_name_per_business",
            ),
            models.CheckConstraint(
                condition=Q(duration__gt=timedelta()),
                name="service_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(buffer_time__gte=timedelta()),
                name="service_buffer_time_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.business.name})"

    def clean(self):
        super().clean()
        if self.category_id and self.business_id != self.category.business_id:
            raise ValidationError(
                {"category": _("Category must belong to the same business.")}
            )


class BookingQuerySet(models.QuerySet):
    def active(self):
        return self.exclude(status=Booking.Status.CANCELLED)

    def overlaps(self, start_time: datetime, end_time: datetime):
        return self.filter(
            start_time__lt=end_time,
            end_time__gt=start_time,
        )


class Booking(TimeStampedModel):
    class Status(models.TextChoices):
        CONFIRMED = "confirmed", _("Confirmed")
        PENDING = "pending", _("Pending")
        CANCELLED = "cancelled", _("Cancelled")
        NO_SHOW = "no_show", _("No show")
        NEEDS_ATTENTION = "needs_attention", _("Needs attention")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="bookings",
    )
    master = models.ForeignKey(
        Master,
        on_delete=models.CASCADE,
        related_name="bookings",
    )
    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    client = models.ForeignKey(
        "Client",
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    service_duration = models.DurationField(
        null=True,
        blank=True,
        editable=False,
    )
    service_buffer_time = models.DurationField(
        null=True,
        blank=True,
        editable=False,
    )
    client_data = models.JSONField(default=dict, blank=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    follow_up_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        ordering = ("start_time",)
        verbose_name = _("booking")
        verbose_name_plural = _("bookings")
        indexes = [
            models.Index(fields=("business", "start_time")),
            models.Index(fields=("master", "start_time", "status")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="booking_end_after_start",
            ),
        ]

    def __str__(self):
        return (
            f"{self.master.full_name} | {self.service.name} | "
            f"{timezone.localtime(self.start_time):%Y-%m-%d %H:%M}"
        )

    def clean(self):
        super().clean()
        self.sync_business_relations()
        self.sync_service_duration()
        self.calculate_end_time()

        if not self.start_time:
            return

        if timezone.is_naive(self.start_time):
            raise ValidationError(
                {"start_time": _("start_time must be timezone-aware.")}
            )

        if not self.pk and self.start_time < timezone.now():
            raise ValidationError(
                {"start_time": _("Cannot create a booking in the past.")}
            )

        if self.has_overlap():
            raise ValidationError(
                _("Selected time slot overlaps with another booking.")
            )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        self.sync_business_relations()
        requires_slot_sync = self.requires_slot_sync(update_fields)
        if requires_slot_sync:
            self.sync_service_duration()
            self.calculate_end_time()
            self.validate_domain_constraints()
            if update_fields is not None:
                kwargs["update_fields"] = list(
                    set(update_fields)
                    | {"business", "service_duration", "service_buffer_time", "end_time"}
                )
        saved_instance = super().save(*args, **kwargs)
        self._original_service_id = self.service_id
        return saved_instance

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_service_id = self.service_id

    def calculate_end_time(self):
        if self.start_time and self.service_duration is not None:
            self.end_time = self.start_time + self.total_slot_duration
        return self.end_time

    def validate_domain_constraints(self):
        if not self.start_time:
            return

        if timezone.is_naive(self.start_time):
            raise ValidationError(
                {"start_time": _("start_time must be timezone-aware.")}
            )

        if not self.pk and self.start_time < timezone.now():
            raise ValidationError(
                {"start_time": _("Cannot create a booking in the past.")}
            )

        if self.has_overlap():
            raise ValidationError(
                _("Selected time slot overlaps with another booking.")
            )

    def sync_service_duration(self):
        should_refresh_snapshot = any(
            [
                self.service_id and self.service_duration is None,
                self.service_id and self.service_buffer_time is None,
                self.service_id and not self.pk,
                self.service_id and self.service_id != self._original_service_id,
            ]
        )
        if should_refresh_snapshot:
            self.service_duration = self.service.duration
            self.service_buffer_time = self.service.buffer_time

    def requires_slot_sync(self, update_fields):
        if update_fields is None or not self.pk:
            return True
        return bool(
            {
                "business",
                "business_id",
                "client",
                "client_id",
                "master",
                "master_id",
                "service",
                "service_id",
                "start_time",
            }
            & set(update_fields)
        )

    def sync_business_relations(self):
        if self.master_id and self.business_id != self.master.business_id:
            self.business = self.master.business

        if self.service_id and self.business_id != self.service.business_id:
            raise ValidationError(
                {"service": _("Service must belong to the same business.")}
            )

        if self.master_id and self.service_id:
            if self.master.business_id != self.service.business_id:
                raise ValidationError(
                    _("Master and service must belong to the same business.")
                )
        if (
            self.service_id
            and self.service.category_id
            and self.service.category.business_id != self.service.business_id
        ):
            raise ValidationError(
                {"service": _("Service category must belong to the same business.")}
            )
        if self.client_id and self.business_id != self.client.business_id:
            raise ValidationError(
                {"client": _("Client must belong to the same business.")}
            )

    def has_overlap(self):
        if self.status == self.Status.CANCELLED:
            return False

        if not all([self.master_id, self.start_time, self.end_time]):
            return False

        return (
            Booking.objects.active()
            .filter(master=self.master)
            .overlaps(self.start_time, self.end_time)
            .exclude(pk=self.pk)
            .exists()
        )

    @property
    def total_slot_duration(self):
        return (
            (self.service_duration or timedelta())
            + (self.service_buffer_time or timedelta())
        )

    @property
    def work_end_time(self):
        if not self.start_time or self.service_duration is None:
            return None
        return self.start_time + self.service_duration

    @staticmethod
    def make_aware_datetime(target_date: date, target_time: time):
        value = datetime.combine(target_date, target_time)
        return timezone.make_aware(value, timezone.get_current_timezone())


class Client(TimeStampedModel):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="clients",
    )
    name = models.CharField(max_length=100, blank=True)
    phone = PhoneNumberField(region="KZ", blank=True, null=True)
    external_id = models.CharField(max_length=100, blank=True)
    telegram_id = models.CharField(max_length=50, blank=True, null=True)
    whatsapp_id = models.CharField(max_length=50, blank=True, null=True)
    ai_failure_count = models.PositiveSmallIntegerField(default=0)
    allow_follow_up = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name", "phone")
        verbose_name = _("client")
        verbose_name_plural = _("clients")
        constraints = [
            models.UniqueConstraint(
                fields=("business", "phone"),
                name="uniq_client_phone_per_business",
            ),
        ]

    def __str__(self):
        return self.name or str(self.phone)


class InboundEvent(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        FAILED = "failed", _("Failed")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="inbound_events",
    )
    channel = models.CharField(max_length=32)
    provider_event_id = models.CharField(max_length=255)
    payload = models.JSONField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.RECEIVED,
    )
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-received_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("business", "channel", "provider_event_id"),
                name="uniq_inbound_event",
            ),
        ]

    def __str__(self):
        return f"{self.channel}:{self.provider_event_id}"


class OutboundMessage(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SUBMITTED = "submitted", _("Submitted")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")
        DEAD_LETTER = "dead_letter", _("Dead letter")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="outbound_messages",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="outbound_messages",
        null=True,
        blank=True,
    )
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name="outbound_messages",
        null=True,
        blank=True,
    )
    channel = models.CharField(max_length=32)
    recipient = models.CharField(max_length=255, blank=True, default="")
    message_type = models.CharField(max_length=32)
    text = models.TextField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    provider_response = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    submitted_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=("booking", "message_type", "status")),
            models.Index(fields=("status", "created_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("booking", "message_type"),
                condition=Q(
                    booking__isnull=False,
                    message_type__in=("reminder", "follow_up"),
                ),
                name="uniq_booking_scheduled_outbound",
            ),
        ]


class AuditLog(TimeStampedModel):
    class ActorType(models.TextChoices):
        SYSTEM = "system", _("System")
        AI = "ai", _("AI")
        HUMAN = "human", _("Human")
        PROVIDER = "provider", _("Provider")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="audit_logs",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    outbound_message = models.ForeignKey(
        OutboundMessage,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )
    actor_type = models.CharField(
        max_length=20,
        choices=ActorType.choices,
        default=ActorType.SYSTEM,
    )
    event_type = models.CharField(max_length=64)
    channel = models.CharField(max_length=32, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("business", "event_type", "created_at")),
            models.Index(fields=("outbound_message", "created_at")),
            models.Index(fields=("booking", "created_at")),
        ]


class AIInteractionLog(TimeStampedModel):
    class Status(models.TextChoices):
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="ai_interaction_logs",
    )
    request_messages = models.JSONField(default=list, blank=True)
    response_text = models.TextField(blank=True)
    summary_text = models.TextField(blank=True)
    model_name = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SUCCESS,
    )
    error_message = models.TextField(blank=True)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("business", "created_at")),
            models.Index(fields=("status", "created_at")),
        ]


class ConversationMessage(TimeStampedModel):
    class Channel(models.TextChoices):
        TELEGRAM = "telegram", _("Telegram")
        WHATSAPP = "whatsapp", _("WhatsApp")

    class Role(models.TextChoices):
        SYSTEM = "system", _("System")
        USER = "user", _("User")
        ASSISTANT = "assistant", _("Assistant")
        TOOL = "tool", _("Tool")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="conversation_messages",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="conversation_messages",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()

    class Meta:
        ordering = ("created_at", "id")
        verbose_name = _("conversation message")
        verbose_name_plural = _("conversation messages")
        indexes = [
            models.Index(fields=("business", "client", "channel", "created_at")),
        ]

    def clean(self):
        super().clean()
        if self.client_id and self.business_id != self.client.business_id:
            raise ValidationError(
                {"client": _("Client must belong to the same business.")}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def prune_history(cls, *, business_id: int, client_id: int, channel: str):
        message_ids = list(
            cls.objects.filter(
                business_id=business_id,
                client_id=client_id,
                channel=channel,
            )
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        )
        if len(message_ids) <= MAX_CONVERSATION_MESSAGES + 5:
            return

        stale_message_ids = list(
            message_ids[MAX_CONVERSATION_MESSAGES:]
        )
        if stale_message_ids:
            cls.objects.filter(
                id__in=stale_message_ids
            ).delete()


class ConversationThread(TimeStampedModel):
    class Mode(models.TextChoices):
        BOT_ACTIVE = "bot_active", _("Bot active")
        HUMAN_TAKEOVER = "human_takeover", _("Human takeover")
        BOT_PAUSED_UNTIL = "bot_paused_until", _("Bot paused until")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="conversation_threads",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="conversation_threads",
    )
    channel = models.CharField(
        max_length=20,
        choices=ConversationMessage.Channel.choices,
    )
    mode = models.CharField(
        max_length=32,
        choices=Mode.choices,
        default=Mode.BOT_ACTIVE,
    )
    bot_paused_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("business_id", "client_id", "channel")
        verbose_name = _("conversation thread")
        verbose_name_plural = _("conversation threads")
        constraints = [
            models.UniqueConstraint(
                fields=("business", "client", "channel"),
                name="uniq_conversation_thread_per_client_channel",
            ),
        ]
        indexes = [
            models.Index(
                fields=("business", "client", "channel", "mode"),
                name="bookings_thread_mode_idx",
            ),
            models.Index(
                fields=("bot_paused_until",),
                name="bookings_thread_paused_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.business_id}:{self.client_id}:{self.channel}"
            f" [{self.mode}]"
        )

    def clean(self):
        super().clean()
        if self.client_id and self.business_id != self.client.business_id:
            raise ValidationError(
                {"client": _("Client must belong to the same business.")}
            )
        if self.mode != self.Mode.BOT_PAUSED_UNTIL and self.bot_paused_until:
            raise ValidationError(
                {
                    "bot_paused_until": _(
                        "Paused-until timestamp is only valid for paused mode."
                    )
                }
            )
        if self.mode == self.Mode.BOT_PAUSED_UNTIL and not self.bot_paused_until:
            raise ValidationError(
                {"bot_paused_until": _("Paused mode requires a timestamp.")}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)



class BookingSession(TimeStampedModel):
    SESSION_TTL = timedelta(minutes=60)

    class State(models.TextChoices):
        IDLE = "idle", _("Idle")
        AWAITING_DATE = "awaiting_date", _("Awaiting date")
        AWAITING_SLOT_CHOICE = "awaiting_slot_choice", _("Awaiting slot choice")
        AWAITING_CONFIRMATION = "awaiting_confirmation", _("Awaiting confirmation")
        CANCEL_CHOOSING = "cancel_choosing", _("Cancellation: choosing booking")
        CANCEL_CONFIRMING = "cancel_confirming", _("Cancellation: confirming booking")
        RESCHEDULE_CHOOSING = "reschedule_choosing", _("Reschedule: choosing booking")

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="booking_sessions",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="booking_sessions",
    )
    channel = models.CharField(
        max_length=20,
        choices=ConversationMessage.Channel.choices,
    )
    state = models.CharField(
        max_length=32,
        choices=State.choices,
        default=State.IDLE,
    )
    service = models.ForeignKey(
        Service,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_sessions",
    )
    master = models.ForeignKey(
        Master,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_sessions",
    )
    target_date = models.DateField(null=True, blank=True)
    selected_start_time = models.DateTimeField(null=True, blank=True)
    selected_end_time = models.DateTimeField(null=True, blank=True)
    slot_options = models.JSONField(default=list, blank=True)
    context = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("business_id", "client_id", "channel")
        verbose_name = _("booking session")
        verbose_name_plural = _("booking sessions")
        constraints = [
            models.UniqueConstraint(
                fields=("business", "client", "channel"),
                name="uniq_booking_session_per_client_channel",
            ),
        ]
        indexes = [
            models.Index(fields=("expires_at",)),
            models.Index(fields=("business", "client", "channel", "state")),
        ]

    def __str__(self):
        return (
            f"{self.business_id}:{self.client_id}:{self.channel}"
            f" [{self.state}]"
        )

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def touch_expiration(self):
        self.expires_at = timezone.now() + self.SESSION_TTL

    def reset(self, *, keep_expiration: bool = False):
        self.state = self.State.IDLE
        self.service = None
        self.master = None
        self.target_date = None
        self.selected_start_time = None
        self.selected_end_time = None
        self.slot_options = []
        self.context = {}
        if not keep_expiration:
            self.touch_expiration()
        return self

    def clean(self):
        super().clean()
        if self.client_id and self.business_id != self.client.business_id:
            raise ValidationError(
                {"client": _("Client must belong to the same business.")}
            )
        if self.service_id and self.business_id != self.service.business_id:
            raise ValidationError(
                {"service": _("Service must belong to the same business.")}
            )
        if self.master_id and self.business_id != self.master.business_id:
            raise ValidationError(
                {"master": _("Master must belong to the same business.")}
            )

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.touch_expiration()
        self.full_clean()
        return super().save(*args, **kwargs)
