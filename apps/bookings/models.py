from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


WEEKDAY_KEYS = (
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Business(TimeStampedModel):
    class Mode(models.TextChoices):
        ALTEGIO = "ALTEGIO", _("Altegio / YClients")
        STANDALONE = "STANDALONE", _("Standalone")

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    mode = models.CharField(
        max_length=20,
        choices=Mode.choices,
        default=Mode.STANDALONE,
    )
    api_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("CRM credentials and sync configuration."),
    )
    ai_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "System prompt, model name, temperature and tool settings."
        ),
    )
    knowledge_base = models.TextField(
        blank=True,
        help_text=_("Business-specific context for the AI assistant."),
    )
    timezone_name = models.CharField(max_length=64, default="UTC")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        verbose_name = _("business")
        verbose_name_plural = _("businesses")

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.mode == self.Mode.ALTEGIO and not self.api_config:
            raise ValidationError(
                {
                    "api_config": _(
                        "API configuration is required in ALTEGIO mode."
                    )
                }
            )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def is_integration_mode(self):
        return self.mode == self.Mode.ALTEGIO

    @property
    def is_standalone_mode(self):
        return self.mode == self.Mode.STANDALONE

    def get_ai_setting(self, key, default=None):
        return self.ai_settings.get(key, default)


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


class Service(TimeStampedModel):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="services",
    )
    name = models.CharField(max_length=255)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    duration = models.DurationField()
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
                check=Q(duration__gt=timedelta()),
                name="service_duration_positive",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.business.name})"


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
    client_data = models.JSONField(default=dict, blank=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
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
                check=Q(end_time__gt=F("start_time")),
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
        self.calculate_end_time()
        self.full_clean()
        return super().save(*args, **kwargs)

    def calculate_end_time(self):
        if self.start_time and self.service_id:
            self.end_time = self.start_time + self.service.duration
        return self.end_time

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

    @staticmethod
    def make_aware_datetime(target_date: date, target_time: time):
        value = datetime.combine(target_date, target_time)
        return timezone.make_aware(value, timezone.get_current_timezone())

