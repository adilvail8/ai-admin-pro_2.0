from django.contrib import admin
from django.contrib import messages

from apps.accounts.models import BusinessMembership

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
from .audit import create_audit_log
from .services import update_booking_status
from .tasks import request_outbound_resend, request_outbound_retry


ADMIN_ROLES = {
    BusinessMembership.Role.OWNER,
    BusinessMembership.Role.ADMIN,
}


class TenantScopedAdminMixin:
    business_filter_field = "business"
    business_related_fields = ()

    def get_admin_business_ids(self, request):
        if request.user.is_superuser:
            return None
        return list(
            BusinessMembership.objects.filter(
                user=request.user,
                is_active=True,
                role__in=ADMIN_ROLES,
            ).values_list("business_id", flat=True)
        )

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
class BusinessAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    business_filter_field = "pk"
    list_display = ("name", "timezone_name", "is_active", "created_at")
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


@admin.register(Category)
class CategoryAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "business", "is_active", "created_at")
    list_filter = ("business", "is_active")
    search_fields = ("name", "description")


@admin.register(Master)
class MasterAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("full_name", "business", "specialization", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("full_name", "specialization")


@admin.register(Service)
class ServiceAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    business_related_fields = ("category",)
    list_display = (
        "name",
        "category",
        "business",
        "price",
        "duration",
        "buffer_time",
        "is_active",
    )
    list_filter = ("business", "category", "is_active")
    search_fields = ("name", "category__name")


@admin.register(Client)
class ClientAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "business",
        "phone",
        "telegram_id",
        "whatsapp_id",
        "ai_failure_count",
    )
    list_filter = ("business", "is_active")
    search_fields = ("name", "phone", "telegram_id", "whatsapp_id")


@admin.register(Booking)
class BookingAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    business_related_fields = ("client", "master", "service")
    actions = ("mark_confirmed", "mark_cancelled", "mark_no_show")
    list_display = (
        "id",
        "business",
        "client",
        "master",
        "service",
        "start_time",
        "status",
    )
    list_filter = ("business", "status")
    search_fields = ("master__full_name", "service__name", "client__phone")

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

    @admin.action(description="Mark selected bookings as confirmed")
    def mark_confirmed(self, request, queryset):
        self._apply_status_action(
            request,
            queryset,
            target_status=Booking.Status.CONFIRMED,
            label="confirmed",
        )

    @admin.action(description="Mark selected bookings as cancelled")
    def mark_cancelled(self, request, queryset):
        self._apply_status_action(
            request,
            queryset,
            target_status=Booking.Status.CANCELLED,
            label="cancelled",
        )

    @admin.action(description="Mark selected bookings as no-show")
    def mark_no_show(self, request, queryset):
        self._apply_status_action(
            request,
            queryset,
            target_status=Booking.Status.NO_SHOW,
            label="no-show",
        )


@admin.register(ConversationMessage)
class ConversationMessageAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    business_related_fields = ("client",)
    list_display = ("id", "business", "client", "channel", "role", "created_at")
    list_filter = ("business", "channel", "role")
    search_fields = ("client__phone", "content")


@admin.register(InboundEvent)
class InboundEventAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "business",
        "channel",
        "provider_event_id",
        "status",
        "received_at",
    )
    list_filter = ("business", "channel", "status")
    search_fields = ("provider_event_id",)


@admin.register(OutboundMessage)
class OutboundMessageAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    business_related_fields = ("client", "booking")
    actions = ("retry_selected_messages", "resend_selected_messages")
    list_display = (
        "id",
        "business",
        "client",
        "channel",
        "recipient",
        "message_type",
        "status",
        "attempts",
        "error_code",
        "provider_message_id",
        "submitted_at",
        "delivered_at",
        "dead_lettered_at",
    )
    list_filter = ("business", "channel", "message_type", "status")
    search_fields = ("client__phone", "text", "provider_message_id")

    @admin.action(description="Retry selected failed outbound messages")
    def retry_selected_messages(self, request, queryset):
        eligible_messages = queryset.filter(status=OutboundMessage.Status.FAILED)
        dispatched = 0
        for outbound_message in eligible_messages.select_related(
            "business",
            "client",
            "booking",
        ):
            request_outbound_retry(
                outbound_message=outbound_message,
                actor_type="human",
                actor_id=request.user.id,
                actor_name=request.user.get_username(),
            )
            dispatched += 1
        skipped = queryset.count() - dispatched
        self.message_user(
            request,
            (
                f"Queued retry for {dispatched} outbound message(s). "
                f"Skipped {skipped} non-failed message(s)."
            ),
            level=messages.SUCCESS if dispatched else messages.WARNING,
        )

    @admin.action(description="Resend selected outbound messages")
    def resend_selected_messages(self, request, queryset):
        eligible_statuses = {
            OutboundMessage.Status.FAILED,
            OutboundMessage.Status.DEAD_LETTER,
            OutboundMessage.Status.CANCELLED,
        }
        dispatched = 0
        for outbound_message in queryset.filter(
            status__in=eligible_statuses
        ).select_related("business", "client", "booking"):
            request_outbound_resend(
                outbound_message=outbound_message,
                actor_type="human",
                actor_id=request.user.id,
                actor_name=request.user.get_username(),
            )
            dispatched += 1
        skipped = queryset.count() - dispatched
        self.message_user(
            request,
            (
                f"Queued resend for {dispatched} outbound message(s). "
                f"Skipped {skipped} message(s) that are already in-flight or delivered."
            ),
            level=messages.SUCCESS if dispatched else messages.WARNING,
        )


@admin.register(AuditLog)
class AuditLogAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
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
class AIInteractionLogAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "business",
        "model_name",
        "status",
        "created_at",
    )
    list_filter = ("business", "status", "model_name")
    search_fields = ("response_text", "error_message")
