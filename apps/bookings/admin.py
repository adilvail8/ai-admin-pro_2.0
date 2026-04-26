from django.contrib import admin
from django.contrib import messages
from django.utils import timezone
from unfold.admin import ModelAdmin
from unfold.decorators import display

from apps.accounts.models import BusinessMembership

from .audit import create_audit_log
from .models import (
    AIInteractionLog,
    AuditLog,
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
from .services import update_booking_status
from .tasks import request_outbound_resend, request_outbound_retry


ADMIN_ROLES = {
    BusinessMembership.Role.OWNER,
    BusinessMembership.Role.ADMIN,
}

BOOKING_STATUS_LABELS = {
    Booking.Status.CONFIRMED: "success",
    Booking.Status.PENDING: "warning",
    Booking.Status.CANCELLED: "danger",
    Booking.Status.NO_SHOW: "danger",
    Booking.Status.NEEDS_ATTENTION: "warning",
}

OUTBOUND_STATUS_LABELS = {
    OutboundMessage.Status.QUEUED: "info",
    OutboundMessage.Status.SUBMITTED: "info",
    OutboundMessage.Status.DELIVERED: "success",
    OutboundMessage.Status.FAILED: "danger",
    OutboundMessage.Status.CANCELLED: "warning",
    OutboundMessage.Status.DEAD_LETTER: "danger",
}


def _get_request_business_ids(request):
    if request.user.is_superuser:
        return None
    return list(
        BusinessMembership.objects.filter(
            user=request.user,
            is_active=True,
            role__in=ADMIN_ROLES,
        ).values_list("business_id", flat=True)
    )


def booking_needs_attention_count(request):
    queryset = Booking.objects.filter(status=Booking.Status.NEEDS_ATTENTION)
    business_ids = _get_request_business_ids(request)
    if business_ids is not None:
        queryset = queryset.filter(business_id__in=business_ids)
    count = queryset.count()
    return str(count) if count else None


def failed_messages_count(request):
    queryset = OutboundMessage.objects.filter(
        status__in=[
            OutboundMessage.Status.FAILED,
            OutboundMessage.Status.DEAD_LETTER,
        ]
    )
    business_ids = _get_request_business_ids(request)
    if business_ids is not None:
        queryset = queryset.filter(business_id__in=business_ids)
    count = queryset.count()
    return str(count) if count else None


def dashboard_callback(request, context):
    business_ids = _get_request_business_ids(request)
    today = timezone.localdate()

    bookings = Booking.objects.filter(start_time__date=today)
    failed_messages = OutboundMessage.objects.filter(
        status__in=[
            OutboundMessage.Status.FAILED,
            OutboundMessage.Status.DEAD_LETTER,
        ]
    )

    if business_ids is not None:
        bookings = bookings.filter(business_id__in=business_ids)
        failed_messages = failed_messages.filter(business_id__in=business_ids)

    context["today_bookings"] = bookings.filter(
        status=Booking.Status.CONFIRMED
    ).count()
    context["needs_attention_bookings"] = bookings.filter(
        status=Booking.Status.NEEDS_ATTENTION
    ).count()
    context["failed_messages"] = failed_messages.count()
    return context


class TenantScopedAdminMixin:
    business_filter_field = "business"
    business_related_fields = ()

    def get_admin_business_ids(self, request):
        return _get_request_business_ids(request)

    def get_business_queryset(self, request):
        business_ids = self.get_admin_business_ids(request)
        queryset = Business.objects.all()
        if business_ids is None:
            return queryset
        return queryset.filter(pk__in=business_ids)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        business_ids = self.get_admin_business_ids(request)
        if business_ids is None:
            return queryset
        return queryset.filter(**{f"{self.business_filter_field}_id__in": business_ids})

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        return bool(self.get_admin_business_ids(request))

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        business_ids = self.get_admin_business_ids(request)
        if not business_ids:
            return False
        if obj is None:
            return True
        return self.get_object_business_id(obj) in business_ids

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj=obj)

    def has_delete_permission(self, request, obj=None):
        return self.has_view_permission(request, obj=obj)

    def get_object_business_id(self, obj):
        return getattr(obj, f"{self.business_filter_field}_id", None)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field_name = db_field.name
        if field_name == self.business_filter_field:
            kwargs["queryset"] = self.get_business_queryset(request)
        elif field_name in self.business_related_fields:
            related_model = db_field.remote_field.model
            queryset = related_model.objects.filter(
                business__in=self.get_business_queryset(request)
            )
            if hasattr(related_model, "is_active"):
                queryset = queryset.filter(is_active=True)
            kwargs["queryset"] = queryset
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Business)
class BusinessAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_filter_field = "pk"
    list_display = ("name", "timezone_name", "colored_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")

    def get_queryset(self, request):
        queryset = super(TenantScopedAdminMixin, self).get_queryset(request)
        business_ids = self.get_admin_business_ids(request)
        if business_ids is None:
            return queryset
        return queryset.filter(pk__in=business_ids)

    def get_object_business_id(self, obj):
        return obj.pk

    @display(description="Активен", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


@admin.register(Category)
class CategoryAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = ("name", "business", "colored_active", "created_at")
    list_filter = ("business", "is_active")
    search_fields = ("name", "description")

    @display(description="Активна", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


@admin.register(Master)
class MasterAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = ("full_name", "business", "specialization", "colored_active")
    list_filter = ("business", "is_active")
    search_fields = ("full_name", "specialization")

    @display(description="Активен", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


@admin.register(Service)
class ServiceAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("category",)
    list_display = (
        "name",
        "category",
        "business",
        "price",
        "duration",
        "buffer_time",
        "colored_active",
    )
    list_filter = ("business", "category", "is_active")
    search_fields = ("name", "category__name")

    @display(description="Активна", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


@admin.register(Client)
class ClientAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = (
        "name",
        "business",
        "phone",
        "telegram_id",
        "whatsapp_id",
        "ai_failure_count",
        "colored_active",
    )
    list_filter = ("business", "is_active")
    search_fields = ("name", "phone", "telegram_id", "whatsapp_id")

    @display(description="Активен", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


@admin.register(Booking)
class BookingAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("client", "master", "service")
    actions = ("mark_confirmed", "mark_cancelled", "mark_no_show")
    date_hierarchy = "start_time"
    list_display = (
        "id",
        "colored_status",
        "client",
        "master",
        "service",
        "start_time",
        "end_time",
        "business",
    )
    list_display_links = ("id", "client")
    list_filter = ("status", "business", "master")
    search_fields = (
        "client__name",
        "client__phone",
        "master__full_name",
        "service__name",
    )
    readonly_fields = (
        "id",
        "business",
        "client",
        "master",
        "service",
        "start_time",
        "end_time",
        "service_duration",
        "service_buffer_time",
        "colored_status",
        "notes",
        "client_data",
        "follow_up_sent_at",
        "reminder_sent_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            "Запись",
            {
                "fields": (
                    "id",
                    "colored_status",
                    ("start_time", "end_time"),
                    ("service_duration", "service_buffer_time"),
                )
            },
        ),
        (
            "Участники",
            {
                "fields": (
                    "business",
                    "client",
                    "master",
                    "service",
                )
            },
        ),
        (
            "Детали",
            {
                "fields": (
                    "notes",
                    "client_data",
                )
            },
        ),
        (
            "Уведомления",
            {
                "fields": (
                    "reminder_sent_at",
                    "follow_up_sent_at",
                )
            },
        ),
        (
            "Служебное",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @display(description="Статус", label=BOOKING_STATUS_LABELS)
    def colored_status(self, obj):
        return obj.status

    def _apply_status_action(self, request, queryset, *, target_status: str, label: str):
        updated = 0
        for booking in queryset.select_related("business", "client"):
            update_booking_status(
                booking=booking,
                business=booking.business,
                status=target_status,
            )
            create_audit_log(
                business=booking.business,
                client=booking.client,
                booking=booking,
                actor_type="human",
                event_type="admin_booking_status_action",
                channel="admin",
                payload={
                    "target_status": target_status,
                    "admin_user_id": request.user.id,
                    "admin_username": request.user.get_username(),
                },
            )
            updated += 1
        self.message_user(
            request,
            f"{updated} booking(s) marked as {label}.",
            level=messages.SUCCESS,
        )

    @admin.action(description="Подтвердить выбранные записи")
    def mark_confirmed(self, request, queryset):
        self._apply_status_action(
            request,
            queryset,
            target_status=Booking.Status.CONFIRMED,
            label="confirmed",
        )

    @admin.action(description="Отменить выбранные записи")
    def mark_cancelled(self, request, queryset):
        self._apply_status_action(
            request,
            queryset,
            target_status=Booking.Status.CANCELLED,
            label="cancelled",
        )

    @admin.action(description="Отметить как неявку")
    def mark_no_show(self, request, queryset):
        self._apply_status_action(
            request,
            queryset,
            target_status=Booking.Status.NO_SHOW,
            label="no-show",
        )


@admin.register(ConversationMessage)
class ConversationMessageAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("client",)
    list_display = ("id", "business", "client", "channel", "role", "created_at")
    list_filter = ("business", "channel", "role")
    search_fields = ("client__phone", "content")


@admin.register(InboundEvent)
class InboundEventAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = (
        "id",
        "business",
        "channel",
        "provider_event_id",
        "colored_status",
        "received_at",
    )
    list_filter = ("business", "channel", "status")
    search_fields = ("provider_event_id",)

    @display(
        description="Статус",
        label={
            InboundEvent.Status.RECEIVED: "info",
            InboundEvent.Status.PROCESSED: "success",
            InboundEvent.Status.FAILED: "danger",
        },
    )
    def colored_status(self, obj):
        return obj.status


@admin.register(OutboundMessage)
class OutboundMessageAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("client", "booking")
    actions = ("retry_selected_messages", "resend_selected_messages")
    list_display = (
        "id",
        "colored_status",
        "channel",
        "message_type",
        "client",
        "recipient",
        "attempts",
        "error_code",
        "submitted_at",
        "business",
    )
    list_display_links = ("id", "client")
    list_filter = ("status", "channel", "message_type", "business")
    search_fields = (
        "client__phone",
        "client__name",
        "provider_message_id",
        "recipient",
    )
    readonly_fields = (
        "id",
        "business",
        "client",
        "booking",
        "channel",
        "recipient",
        "message_type",
        "colored_status",
        "text",
        "attempts",
        "error_code",
        "last_error",
        "provider_message_id",
        "provider_response",
        "submitted_at",
        "delivered_at",
        "dead_lettered_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            "Сообщение",
            {
                "fields": (
                    "id",
                    "colored_status",
                    ("channel", "message_type"),
                    ("business", "client"),
                    "booking",
                    "recipient",
                    "text",
                )
            },
        ),
        (
            "Доставка",
            {
                "fields": (
                    "attempts",
                    ("submitted_at", "delivered_at"),
                    "dead_lettered_at",
                    "provider_message_id",
                )
            },
        ),
        (
            "Ошибки",
            {
                "fields": (
                    "error_code",
                    "last_error",
                    "provider_response",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Служебное",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @display(description="Статус", label=OUTBOUND_STATUS_LABELS)
    def colored_status(self, obj):
        return obj.status

    @admin.action(description="Повторить доставку (только FAILED)")
    def retry_selected_messages(self, request, queryset):
        eligible_messages = queryset.filter(status=OutboundMessage.Status.FAILED)
        dispatched = 0
        for msg in eligible_messages.select_related(
            "business",
            "client",
            "booking",
        ):
            request_outbound_retry(
                outbound_message=msg,
                actor_type="human",
                actor_id=request.user.id,
                actor_name=request.user.get_username(),
            )
            dispatched += 1
        skipped = queryset.count() - dispatched
        self.message_user(
            request,
            f"Поставлено в очередь: {dispatched}. Пропущено: {skipped}.",
            level=messages.SUCCESS if dispatched else messages.WARNING,
        )

    @admin.action(description="Переотправить (FAILED / DEAD_LETTER / CANCELLED)")
    def resend_selected_messages(self, request, queryset):
        eligible_statuses = {
            OutboundMessage.Status.FAILED,
            OutboundMessage.Status.DEAD_LETTER,
            OutboundMessage.Status.CANCELLED,
        }
        dispatched = 0
        for msg in queryset.filter(
            status__in=eligible_statuses
        ).select_related("business", "client", "booking"):
            request_outbound_resend(
                outbound_message=msg,
                actor_type="human",
                actor_id=request.user.id,
                actor_name=request.user.get_username(),
            )
            dispatched += 1
        skipped = queryset.count() - dispatched
        self.message_user(
            request,
            f"Переотправлено: {dispatched}. Пропущено: {skipped}.",
            level=messages.SUCCESS if dispatched else messages.WARNING,
        )


@admin.register(AuditLog)
class AuditLogAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("client", "booking", "outbound_message")
    list_display = (
        "id",
        "business",
        "event_type",
        "actor_type",
        "channel",
        "client",
        "booking",
        "created_at",
    )
    list_filter = ("business", "event_type", "actor_type", "channel")
    search_fields = ("event_type", "client__phone", "booking__id")


@admin.register(AIInteractionLog)
class AIInteractionLogAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = (
        "id",
        "business",
        "model_name",
        "colored_status",
        "created_at",
    )
    list_filter = ("business", "status", "model_name")
    search_fields = ("response_text", "error_message")

    @display(
        description="Статус",
        label={
            AIInteractionLog.Status.SUCCESS: "success",
            AIInteractionLog.Status.FAILED: "danger",
        },
    )
    def colored_status(self, obj):
        return obj.status
