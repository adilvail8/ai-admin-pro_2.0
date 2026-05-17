from datetime import timedelta

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Max, OuterRef, Q, Subquery, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.module_loading import import_string
from unfold.admin import ModelAdmin
from unfold.decorators import display

from apps.accounts.models import BusinessMembership

from .audit import create_audit_log
from .conversation_threads import (
    get_or_create_conversation_thread,
    pause_bot_for_human_reply,
    set_thread_mode,
)
from .models import (
    AIInteractionLog,
    AuditLog,
    Booking,
    Business,
    Category,
    Client,
    ConversationMessage,
    ConversationThread,
    InboundEvent,
    Master,
    MasterUnavailability,
    OutboundMessage,
    Service,
    WEEKDAY_KEYS,
)
from .services import create_appointment, update_booking_status
from .widgets import WorkingHoursWidget
from .tasks import (
    dispatch_outbound_delivery,
    get_client_channel,
    get_client_recipient,
    request_outbound_resend,
    request_outbound_retry,
)


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

TECHNICAL_AUDIT_EVENT_TYPES = {
    "outbound_submitted",
    "outbound_reply_queued",
}

USER_ROLE = ConversationMessage.Role.USER
REPLY_ROLES = [
    ConversationMessage.Role.ASSISTANT,
    ConversationMessage.Role.TOOL,
    ConversationMessage.Role.SYSTEM,
    "human",
]


def _get_request_business_ids(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return []
    if user.is_superuser:
        return None
    return list(
        BusinessMembership.objects.filter(
            user=user,
            is_active=True,
            role__in=ADMIN_ROLES,
        ).values_list("business_id", flat=True)
    )


def get_request_business_memberships(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or user.is_superuser:
        return BusinessMembership.objects.none()
    return (
        BusinessMembership.objects.select_related("business")
        .filter(
            user=user,
            is_active=True,
            role__in=ADMIN_ROLES,
        )
        .order_by("business__name")
    )


def is_business_owner_mode(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_authenticated", False)
        and not getattr(user, "is_superuser", False)
        and get_request_business_memberships(request).exists()
    )


def is_single_business_owner_mode(request):
    business_ids = _get_request_business_ids(request)
    return bool(
        business_ids
        and business_ids is not None
        and not getattr(getattr(request, "user", None), "is_superuser", False)
        and len(business_ids) == 1
    )


def get_single_business_id(request):
    business_ids = _get_request_business_ids(request)
    if (
        business_ids
        and business_ids is not None
        and not getattr(getattr(request, "user", None), "is_superuser", False)
        and len(business_ids) == 1
    ):
        return business_ids[0]
    return None


def get_primary_business(request):
    membership = get_request_business_memberships(request).first()
    return membership.business if membership else None


class OutboundMessageReplyForm(forms.ModelForm):
    class Meta:
        model = OutboundMessage
        fields = ("client", "booking", "channel", "text")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel"].choices = (
            ("telegram", "Telegram"),
            ("whatsapp", "WhatsApp"),
        )

    def clean(self):
        cleaned_data = super().clean()
        client = cleaned_data.get("client")
        booking = cleaned_data.get("booking")
        channel = cleaned_data.get("channel")
        if client is None:
            raise ValidationError({"client": "Выберите клиента."})
        if booking is not None:
            if booking.client_id != client.id:
                raise ValidationError(
                    {"booking": "Запись должна принадлежать выбранному клиенту."}
                )
            if booking.business_id != client.business_id:
                raise ValidationError(
                    {"booking": "Запись и клиент должны относиться к одному салону."}
                )
        if channel not in {"telegram", "whatsapp"}:
            raise ValidationError({"channel": "Выберите канал ответа."})
        if channel == "telegram" and not client.telegram_id:
            raise ValidationError(
                {"channel": "У клиента нет Telegram для ответа."}
            )
        if channel == "whatsapp" and not (client.whatsapp_id or client.phone):
            raise ValidationError(
                {"channel": "У клиента нет WhatsApp или телефона для ответа."}
            )
        return cleaned_data


class OwnerInboxReplyForm(forms.Form):
    client_id = forms.IntegerField(widget=forms.HiddenInput)
    channel = forms.ChoiceField(
        choices=(
            ("telegram", "Telegram"),
            ("whatsapp", "WhatsApp"),
        ),
        widget=forms.HiddenInput,
    )
    text = forms.CharField(
        label="Сообщение",
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "Напишите ответ клиенту...",
                "class": "owner-inbox-reply-textarea",
            }
        ),
    )


def site_header_callback(request):
    if not is_business_owner_mode(request):
        return "AI Admin Pro"
    business = get_primary_business(request)
    return business.display_brand_name if business else "AI Admin Pro"


def site_title_callback(request):
    if not is_business_owner_mode(request):
        return "AI Admin Pro"
    business = get_primary_business(request)
    if business is None:
        return "AI Admin Pro"
    return f"{business.display_brand_name} | кабинет салона"


def site_subheader_callback(request):
    if not is_business_owner_mode(request):
        return "Интеграторская панель"
    business = get_primary_business(request)
    if business is None:
        return "Кабинет салона"
    return f"{business.city} · кабинет салона"


def _normalize_admin_text(value):
    if not isinstance(value, str):
        return value
    if not any(marker in value for marker in ("Ð", "Ñ", "Ã", "Ä", "à", "Р", "Ў", "вЂ", "�")):
        return value
    for encoding in ("latin1", "cp1251"):
        try:
            candidate = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not any(marker in candidate for marker in ("Ð", "Ñ", "Ã", "Ä", "à", "Р", "Ў", "вЂ", "�")):
            return candidate
    return value


def _normalize_sidebar_navigation(navigation):
    normalized = []
    for group in navigation:
        fixed_group = dict(group)
        fixed_group["title"] = _normalize_admin_text(group.get("title"))
        fixed_items = []
        for item in group.get("items", []):
            fixed_item = dict(item)
            fixed_item["title"] = _normalize_admin_text(item.get("title"))
            fixed_items.append(fixed_item)
        fixed_group["items"] = fixed_items
        normalized.append(fixed_group)
    return normalized


def _polish_sidebar_titles(navigation, request):
    owner_mode = is_business_owner_mode(request)
    item_titles = {
        "bookings/booking": "Бронирования",
        "bookings/client": "Клиенты",
        "bookings/outboundmessage": "Сообщения клиентам" if owner_mode else "Исходящие сообщения",
        "bookings/inboundevent": "Входящие события",
        "bookings/business": "Настройки салона" if owner_mode else "Бизнесы",
        "bookings/master": "Мастера",
        "bookings/service": "Услуги",
        "bookings/category": "Категории",
        "bookings/auditlog": "Аудит",
        "bookings/aiinteractionlog": "AI Логи",
        "auth/user": "Пользователи",
    }
    for group in navigation:
        for item in group.get("items", []):
            if item.get("preserve_title"):
                continue
            link = str(item.get("link") or "")
            for marker, title in item_titles.items():
                if marker in link:
                    item["title"] = title
                    break
    return navigation


def _resolve_sidebar_badges(navigation, request):
    resolved = []
    for group in navigation:
        fixed_group = dict(group)
        group_badge = fixed_group.get("badge")
        if isinstance(group_badge, str) and "." in group_badge:
            badge_value = import_string(group_badge)(request)
            if badge_value:
                fixed_group["badge"] = str(badge_value)
            else:
                fixed_group.pop("badge", None)
                fixed_group.pop("badge_callback", None)
        fixed_items = []
        for item in fixed_group.get("items", []):
            fixed_item = dict(item)
            item_badge = fixed_item.get("badge")
            if isinstance(item_badge, str) and "." in item_badge:
                badge_value = import_string(item_badge)(request)
                if badge_value:
                    fixed_item["badge"] = str(badge_value)
                else:
                    fixed_item.pop("badge", None)
                    fixed_item.pop("badge_callback", None)
            fixed_items.append(fixed_item)
        fixed_group["items"] = fixed_items
        resolved.append(fixed_group)
    return resolved


def canonical_site_title_callback(request):
    return _normalize_admin_text(site_title_callback(request))


def canonical_site_subheader_callback(request):
    return _normalize_admin_text(site_subheader_callback(request))


def canonical_sidebar_navigation(request):
    navigation = _normalize_sidebar_navigation(get_clean_sidebar_navigation(request))
    navigation = _polish_sidebar_titles(navigation, request)
    return _resolve_sidebar_badges(navigation, request)


def get_clean_sidebar_navigation(request):
    if not is_business_owner_mode(request):
        return [
            {
                "title": "Записи",
                "icon": "calendar_month",
                "items": [
                    {
                        "title": "Бронирования",
                        "icon": "event",
                        "link": reverse("admin:bookings_booking_changelist"),
                        "badge": "apps.bookings.admin.booking_needs_attention_count",
                    },
                    {
                        "title": "Клиенты",
                        "icon": "people",
                        "link": reverse("admin:bookings_client_changelist"),
                    },
                ],
            },
            {
                "title": "Коммуникации",
                "icon": "chat",
                "items": [
                    {
                        "title": "Диалоги",
                        "icon": "forum",
                        "link": reverse("admin:bookings_conversationmessage_inbox"),
                    },
                    {
                        "title": "Сообщения",
                        "icon": "send",
                        "link": reverse("admin:bookings_outboundmessage_changelist"),
                        "badge": "apps.bookings.admin.failed_messages_count",
                    },
                    {
                        "title": "Аудит",
                        "icon": "history",
                        "link": reverse("admin:bookings_auditlog_changelist"),
                    },
                ],
            },
            {
                "title": "Справочники",
                "icon": "settings",
                "items": [
                    {
                        "title": "Бизнесы",
                        "icon": "store",
                        "link": reverse("admin:bookings_business_changelist"),
                    },
                    {
                        "title": "Мастера",
                        "icon": "person",
                        "link": reverse("admin:bookings_master_changelist"),
                    },
                    {
                        "title": "Отпуска мастеров",
                        "icon": "event_busy",
                        "link": reverse(
                            "admin:bookings_masterunavailability_changelist"
                        ),
                        "preserve_title": True,
                    },
                    {
                        "title": "Услуги",
                        "icon": "spa",
                        "link": reverse("admin:bookings_service_changelist"),
                    },
                    {
                        "title": "Категории",
                        "icon": "category",
                        "link": reverse("admin:bookings_category_changelist"),
                    },
                ],
            },
            {
                "title": "Система",
                "icon": "admin_panel_settings",
                "items": [
                    {
                        "title": "AI логи",
                        "icon": "psychology",
                        "link": reverse("admin:bookings_aiinteractionlog_changelist"),
                    },
                    {
                        "title": "Пользователи",
                        "icon": "manage_accounts",
                        "link": reverse("admin:auth_user_changelist"),
                    },
                ],
            },
        ]

    return [
        {
            "title": "Управление",
            "icon": "content_cut",
            "items": [
                {
                    "title": "Бронирования",
                    "icon": "event",
                    "link": reverse("admin:bookings_booking_changelist"),
                    "badge": "apps.bookings.admin.booking_needs_attention_count",
                },
                {
                    "title": "Клиенты",
                    "icon": "people",
                    "link": reverse("admin:bookings_client_changelist"),
                },
                {
                    "title": "Мастера",
                    "icon": "person",
                    "link": reverse("admin:bookings_master_changelist"),
                },
                {
                    "title": "Отпуска мастеров",
                    "icon": "event_busy",
                    "link": reverse(
                        "admin:bookings_masterunavailability_changelist"
                    ),
                    "preserve_title": True,
                },
                {
                    "title": "Услуги",
                    "icon": "spa",
                    "link": reverse("admin:bookings_service_changelist"),
                },
                {
                    "title": "Категории",
                    "icon": "category",
                    "link": reverse("admin:bookings_category_changelist"),
                },
                {
                    "title": "Настройки салона",
                    "icon": "store",
                    "link": _build_owner_business_link(request),
                },
            ],
        },
        {
            "title": "Переписка",
            "icon": "chat",
            "items": [
                {
                    "title": "Диалоги",
                    "icon": "forum",
                    "link": reverse("admin:bookings_conversationmessage_inbox"),
                },
            ],
        },
        {
            "title": "Аналитика",
            "icon": "insights",
            "items": [
                {
                    "title": "Сводка по салону",
                    "icon": "bar_chart",
                    "link": reverse("admin:bookings_business_analytics"),
                    "preserve_title": True,
                },
            ],
        },
    ]


def owner_admin_styles(request):
    return "/static/bookings/css/owner_admin.css"


def _build_owner_business_link(request):
    business = get_primary_business(request)
    if business is None:
        return reverse("admin:bookings_business_changelist")
    return reverse("admin:bookings_business_change", args=[business.pk])


def get_sidebar_navigation(request):
    """Alias for backwards compatibility.

    Single source of truth is get_clean_sidebar_navigation().
    """
    return get_clean_sidebar_navigation(request)


def booking_needs_attention_count(request):
    queryset = Booking.objects.filter(status=Booking.Status.NEEDS_ATTENTION)
    business_ids = _get_request_business_ids(request)
    if business_ids is not None:
        queryset = queryset.filter(business_id__in=business_ids)
    count = queryset.count()
    return str(count) if count else ""


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
    return str(count) if count else ""


def _dialog_needs_attention(last_user_message, last_reply_message):
    return bool(
        last_user_message
        and (
            last_reply_message is None
            or last_user_message.created_at > last_reply_message.created_at
        )
    )


def _count_stale_unanswered_dialogs(messages_queryset, stale_threshold):
    count = 0
    client_ids = messages_queryset.values_list("client_id", flat=True).distinct()
    for client_id in client_ids:
        last_user_message = (
            messages_queryset.filter(client_id=client_id, role=USER_ROLE)
            .order_by("-created_at", "-id")
            .first()
        )
        if not last_user_message or last_user_message.created_at > stale_threshold:
            continue
        last_reply_message = (
            messages_queryset.filter(client_id=client_id, role__in=REPLY_ROLES)
            .order_by("-created_at", "-id")
            .first()
        )
        if _dialog_needs_attention(last_user_message, last_reply_message):
            count += 1
    return count


def dashboard_callback(request, context):
    business_ids = _get_request_business_ids(request)
    now = timezone.now()
    today = timezone.localdate()
    last_24h = now - timedelta(hours=24)
    stale_threshold = now - timedelta(hours=2)

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

    if is_business_owner_mode(request):
        business = get_primary_business(request)
        owner_bookings = Booking.objects.filter(business=business) if business else Booking.objects.none()
        owner_messages = (
            ConversationMessage.objects.filter(business=business)
            if business
            else ConversationMessage.objects.none()
        )
        today_bookings = owner_bookings.filter(start_time__date=today)
        upcoming_bookings = owner_bookings.filter(
            start_time__gte=now,
            status=Booking.Status.CONFIRMED,
        ).select_related("client", "master", "service").order_by("start_time")[:5]
        new_messages_24h = owner_messages.filter(
            role=USER_ROLE,
            created_at__gte=last_24h,
        ).count()
        stale_dialogs = _count_stale_unanswered_dialogs(
            owner_messages,
            stale_threshold,
        )

        context["owner_dashboard"] = {
            "business": business,
            "bookings_today": today_bookings.filter(
                status__in=[Booking.Status.CONFIRMED, Booking.Status.PENDING],
            ).count(),
            "new_messages_24h": new_messages_24h,
            "stale_dialogs": stale_dialogs,
            "cards": [
                {
                    "label": "\u0417\u0430\u043f\u0438\u0441\u0438 \u0441\u0435\u0433\u043e\u0434\u043d\u044f",
                    "value": today_bookings.filter(
                        status__in=[
                            Booking.Status.CONFIRMED,
                            Booking.Status.PENDING,
                        ],
                    ).count(),
                    "icon": "event_available",
                    "tone": "green",
                    "href": reverse("admin:bookings_booking_changelist"),
                },
                {
                    "label": "\u041d\u043e\u0432\u044b\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f \u0437\u0430 24\u0447",
                    "value": new_messages_24h,
                    "icon": "mark_unread_chat_alt",
                    "tone": "red",
                    "href": reverse("admin:bookings_conversationmessage_inbox"),
                },
                {
                    "label": "\u0411\u0435\u0437 \u043e\u0442\u0432\u0435\u0442\u0430 2\u0447+",
                    "value": stale_dialogs,
                    "icon": "schedule",
                    "tone": "amber",
                    "href": f"{reverse('admin:bookings_conversationmessage_inbox')}?status=attention",
                },
            ],
            "quick_actions": [
                {
                    "label": "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0434\u0438\u0430\u043b\u043e\u0433\u0438",
                    "icon": "forum",
                    "href": reverse("admin:bookings_conversationmessage_inbox"),
                },
                {
                    "label": "\u041d\u043e\u0432\u0430\u044f \u0437\u0430\u043f\u0438\u0441\u044c",
                    "icon": "add_circle",
                    "href": reverse("admin:bookings_booking_add"),
                },
                {
                    "label": "\u0423\u0441\u043b\u0443\u0433\u0438",
                    "icon": "content_cut",
                    "href": reverse("admin:bookings_service_changelist"),
                },
            ],
            "upcoming_bookings": upcoming_bookings,
        }
    return context


class TenantScopedAdminMixin:
    business_filter_field = "business"
    business_related_fields = ()
    owner_hidden_list_columns = ("business",)
    owner_hidden_filters = ("business",)

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

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or [])
        single_business_id = get_single_business_id(request)
        if (
            single_business_id is not None
            and self.business_filter_field != "pk"
            and self.business_filter_field not in exclude
        ):
            exclude.append(self.business_filter_field)
        return exclude

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

    def get_list_display(self, request):
        list_display = list(super().get_list_display(request))
        if is_single_business_owner_mode(request):
            list_display = [
                item for item in list_display if item not in self.owner_hidden_list_columns
            ]
        return list_display

    def get_list_filter(self, request):
        list_filter = list(super().get_list_filter(request))
        if is_single_business_owner_mode(request):
            list_filter = [
                item for item in list_filter if item not in self.owner_hidden_filters
            ]
        return list_filter

    def save_model(self, request, obj, form, change):
        single_business_id = get_single_business_id(request)
        if (
            single_business_id is not None
            and self.business_filter_field != "pk"
            and hasattr(obj, f"{self.business_filter_field}_id")
            and getattr(obj, f"{self.business_filter_field}_id", None) is None
        ):
            setattr(obj, f"{self.business_filter_field}_id", single_business_id)
        return super().save_model(request, obj, form, change)


@admin.register(Business)
class BusinessAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_filter_field = "pk"
    list_display = ("name", "timezone_name", "colored_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    fieldsets = (
        (
            "Основные данные",
            {"fields": ("name", "brand_name", "address", "city", "slug")},
        ),
        (
            "Расписание",
            {"fields": ("working_hours", "timezone_name")},
        ),
        (
            "AI-настройки",
            {"fields": ("ai_settings", "ai_rules", "knowledge_base")},
        ),
        (
            "Политика бота",
            {
                "fields": ("cancellation_policy_hours",),
                "description": (
                    "Поведение бота при запросах от клиентов. "
                    "Порог отмены: за сколько часов до начала клиент "
                    "может отменить запись через бота без оператора."
                ),
            },
        ),
        (
            "Статус",
            {"fields": ("is_active",)},
        ),
    )

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

    def get_urls(self):
        return [
            path(
                "analytics/",
                self.admin_site.admin_view(self.analytics_view),
                name="bookings_business_analytics",
            ),
            *super().get_urls(),
        ]

    def analytics_view(self, request):
        """Owner analytics dashboard — bookings, conversion, revenue."""
        if not request.user.is_authenticated or not request.user.is_staff:
            return redirect(f"{reverse('admin:login')}?next={request.path}")

        # Period selector: today / 7d / 30d. Default 30 days.
        period = request.GET.get("period", "30d")
        if period not in {"today", "7d", "30d"}:
            period = "30d"

        # Tenant scoping — owners see only their businesses; superusers see all
        # (or one selected — future enhancement).
        business_ids = self.get_admin_business_ids(request)
        businesses = Business.objects.filter(is_active=True)
        if business_ids is not None:
            businesses = businesses.filter(pk__in=business_ids)

        # Period window anchored on Booking.start_time — "когда происходит запись"
        # is more product-meaningful for an owner than "когда её создали". For the
        # "today" tab we keep the whole day so upcoming hours stay visible.
        now = timezone.now()
        today = timezone.localdate()
        if period == "today":
            period_filter = Q(start_time__date=today)
        elif period == "7d":
            period_filter = Q(start_time__gte=now - timedelta(days=7), start_time__lte=now)
        else:
            period_filter = Q(start_time__gte=now - timedelta(days=30), start_time__lte=now)

        bookings_qs = (
            Booking.objects.filter(business__in=businesses)
            .filter(period_filter)
        )
        total_bookings = bookings_qs.count()
        confirmed_count = bookings_qs.filter(status=Booking.Status.CONFIRMED).count()
        cancelled_count = bookings_qs.filter(status=Booking.Status.CANCELLED).count()
        revenue = (
            bookings_qs.filter(status=Booking.Status.CONFIRMED)
            .aggregate(total=Sum("service__price"))["total"]
            or 0
        )

        def pct(numer, denom):
            return round((numer / denom) * 100) if denom else 0

        kpi_cards = [
            {
                "label": "Всего записей",
                "value": total_bookings,
                "subtitle": "за период",
                "tone": "neutral",
            },
            {
                "label": "Подтверждено",
                "value": f"{pct(confirmed_count, total_bookings)}%",
                "subtitle": f"{confirmed_count} из {total_bookings}",
                "tone": "green",
            },
            {
                "label": "Отменено",
                "value": f"{pct(cancelled_count, total_bookings)}%",
                "subtitle": f"{cancelled_count} из {total_bookings}",
                "tone": "red",
            },
            {
                "label": "Выручка",
                "value": f"{int(revenue):,} ₸".replace(",", " "),
                "subtitle": "по подтверждённым",
                "tone": "amber",
            },
        ]

        # Top services — by booking count, with summed revenue. Sum on
        # service__price effectively multiplies by row count (price is
        # constant per service join), giving total revenue.
        top_services = list(
            bookings_qs.values("service__name")
            .annotate(
                count=Count("id"),
                revenue=Sum("service__price"),
            )
            .order_by("-count")[:5]
        )
        for row in top_services:
            row["revenue"] = int(row["revenue"] or 0)

        # Master workload — totals + how many became CONFIRMED. Helps the
        # owner spot a master with high traffic but low conversion.
        master_workload = list(
            bookings_qs.values("master__full_name")
            .annotate(
                count=Count("id"),
                confirmed=Count(
                    "id", filter=Q(status=Booking.Status.CONFIRMED)
                ),
            )
            .order_by("-count")[:5]
        )

        # Channel breakdown — messages from clients (USER role) per channel.
        # Anchored on created_at because messages don't have a future date.
        if period == "today":
            messages_filter = Q(created_at__date=today)
        elif period == "7d":
            messages_filter = Q(
                created_at__gte=now - timedelta(days=7),
                created_at__lte=now,
            )
        else:
            messages_filter = Q(
                created_at__gte=now - timedelta(days=30),
                created_at__lte=now,
            )
        channel_breakdown = list(
            ConversationMessage.objects.filter(
                business__in=businesses,
                role=ConversationMessage.Role.USER,
            )
            .filter(messages_filter)
            .values("channel")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        # Pretty-print channel labels for display.
        channel_labels = dict(ConversationMessage.Channel.choices)
        for row in channel_breakdown:
            row["channel_label"] = channel_labels.get(
                row["channel"], row["channel"]
            )

        # Conversion — unique clients who messaged vs unique clients who got
        # a booking_created audit. Both anchored on the same period window
        # via created_at, so the ratio answers "how many of those who talked
        # to the bot ended up with a booking in the same window".
        unique_clients_messaged = (
            ConversationMessage.objects.filter(
                business__in=businesses,
                role=ConversationMessage.Role.USER,
            )
            .filter(messages_filter)
            .values("client_id")
            .distinct()
            .count()
        )
        unique_clients_booked = (
            AuditLog.objects.filter(
                business__in=businesses,
                event_type="booking_created",
            )
            .filter(messages_filter)
            .exclude(client__isnull=True)
            .values("client_id")
            .distinct()
            .count()
        )
        conversion_rate = pct(unique_clients_booked, unique_clients_messaged)

        # Repeat clients — hybrid metric. The "period" count answers
        # "how many regulars came back in the selected window"; the
        # "lifetime" count answers "how big is our loyal base overall".
        # Owner sees both at once so the period selector still drives the
        # primary number while the lifetime context stays visible.
        repeat_clients_period = (
            bookings_qs.filter(status=Booking.Status.CONFIRMED)
            .values("client_id")
            .annotate(n=Count("id"))
            .filter(n__gte=2)
            .count()
        )
        repeat_clients_lifetime = (
            Booking.objects.filter(
                business__in=businesses,
                status=Booking.Status.CONFIRMED,
            )
            .values("client_id")
            .annotate(n=Count("id"))
            .filter(n__gte=2)
            .count()
        )

        context = {
            **self.admin_site.each_context(request),
            "title": "Аналитика салона",
            "opts": self.model._meta,
            "selected_period": period,
            "period_options": [
                ("today", "Сегодня"),
                ("7d", "7 дней"),
                ("30d", "30 дней"),
            ],
            "businesses": list(businesses),
            "kpi_cards": kpi_cards,
            "top_services": top_services,
            "master_workload": master_workload,
            "channel_breakdown": channel_breakdown,
            "conversion": {
                "messaged": unique_clients_messaged,
                "booked": unique_clients_booked,
                "rate": conversion_rate,
            },
            "repeat_clients": {
                "period": repeat_clients_period,
                "lifetime": repeat_clients_lifetime,
            },
        }
        return TemplateResponse(
            request,
            "admin/bookings/business/analytics.html",
            context,
        )


@admin.register(Category)
class CategoryAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = ("name", "business", "colored_active", "created_at")
    list_filter = ("business", "is_active")
    search_fields = ("name", "description")

    @display(description="Активна", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


DEFAULT_NEW_MASTER_WORKING_HOURS = {
    "mon": {"start": "09:00", "end": "18:00"},
    "tue": {"start": "09:00", "end": "18:00"},
    "wed": {"start": "09:00", "end": "18:00"},
    "thu": {"start": "09:00", "end": "18:00"},
    "fri": {"start": "09:00", "end": "18:00"},
}

_WEEKDAY_SHORT_LABELS = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}


def _summarize_working_hours(working_hours) -> str:
    """Compress a weekly schedule dict into a compact human-readable string.

    Walks the canonical Mon→Sun order, groups consecutive days with
    identical start/end times into a single range, and renders each group
    as either "Пн 10:00-18:00" (single day) or "Пн-Пт 10:00-18:00"
    (range). Days without an entry (or with incomplete data) are treated
    as days off and break the current range.

    Real-world example:
        {"mon": "10-20", "tue": "10-20", ..., "fri": "10-21", "sat": "10-21", "sun": "11-19"}
        → "Пн-Чт 10:00-20:00, Пт-Сб 10:00-21:00, Вс 11:00-19:00"
    """
    if not isinstance(working_hours, dict) or not working_hours:
        return "—"

    groups = []
    current = None  # tuple (start_key, end_key, (start_time, end_time))
    for key in WEEKDAY_KEYS:
        day = working_hours.get(key)
        if not isinstance(day, dict):
            if current is not None:
                groups.append(current)
                current = None
            continue
        start = (day.get("start") or "").strip()
        end = (day.get("end") or "").strip()
        if not start or not end:
            if current is not None:
                groups.append(current)
                current = None
            continue
        hours = (start, end)
        if current is not None and current[2] == hours:
            current = (current[0], key, hours)
        else:
            if current is not None:
                groups.append(current)
            current = (key, key, hours)
    if current is not None:
        groups.append(current)

    if not groups:
        return "—"

    parts = []
    for start_key, end_key, (start, end) in groups:
        if start_key == end_key:
            day_label = _WEEKDAY_SHORT_LABELS[start_key]
        else:
            day_label = (
                f"{_WEEKDAY_SHORT_LABELS[start_key]}-"
                f"{_WEEKDAY_SHORT_LABELS[end_key]}"
            )
        parts.append(f"{day_label} {start}-{end}")
    return ", ".join(parts)


class MasterForm(forms.ModelForm):
    class Meta:
        model = Master
        fields = "__all__"
        widgets = {
            "working_hours": WorkingHoursWidget(),
        }


@admin.register(Master)
class MasterAdmin(TenantScopedAdminMixin, ModelAdmin):
    form = MasterForm
    list_display = (
        "full_name_display",
        "business",
        "specialization_display",
        "schedule_summary",
        "colored_active",
    )
    list_filter = ("business", "is_active")
    search_fields = ("full_name", "specialization")
    ordering = ("full_name",)

    @display(description="Активен", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active

    @display(description="Мастер", ordering="full_name")
    def full_name_display(self, obj):
        return obj.full_name

    @display(description="Специализация", ordering="specialization")
    def specialization_display(self, obj):
        return obj.specialization or "—"

    @display(description="Расписание")
    def schedule_summary(self, obj):
        return _summarize_working_hours(obj.working_hours)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("working_hours", DEFAULT_NEW_MASTER_WORKING_HOURS)
        return initial


@admin.register(MasterUnavailability)
class MasterUnavailabilityAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("master",)
    date_hierarchy = "start_time"
    list_display = (
        "master",
        "business",
        "start_time",
        "end_time",
        "reason",
        "colored_active",
    )
    list_filter = ("business", "master", "is_active")
    list_select_related = ("business", "master")
    search_fields = ("master__full_name", "reason")
    ordering = ("start_time", "master__full_name")

    @display(description="Активно", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active


def _format_price_kzt(price) -> str:
    """Decimal/int price → '9 000 ₸' with thin-space thousand separator."""
    if price is None:
        return "—"
    try:
        amount = int(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{amount:,} ₸".replace(",", " ")


def _format_duration_ru(duration) -> str:
    """timedelta → '45 мин' / '1 ч' / '1 ч 30 мин' / '—' for zero/None."""
    if duration is None:
        return "—"
    total_minutes = int(duration.total_seconds() // 60)
    if total_minutes <= 0:
        return "—"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


class TenantCategoryListFilter(admin.SimpleListFilter):
    """Service-category filter scoped to the request's businesses.

    Same rationale as TenantMasterListFilter: Django's default would
    show every Category row site-wide (foreign tenants visible in the
    sidebar), and Category.__str__ appends a "(Business name)" suffix
    that's noise inside a single-tenant view.
    """

    title = "Категория"
    parameter_name = "category"

    def lookups(self, request, model_admin):
        queryset = Category.objects.select_related("business").filter(is_active=True)
        business_ids = _get_request_business_ids(request)
        if business_ids is not None:
            queryset = queryset.filter(business_id__in=business_ids)
        is_super = bool(getattr(request.user, "is_superuser", False))
        return [
            (
                category.id,
                f"{category.name} ({category.business.name})"
                if is_super
                else category.name,
            )
            for category in queryset.order_by("name")
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value:
            return queryset.filter(category_id=value)
        return queryset


@admin.register(Service)
class ServiceAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("category",)
    list_display = (
        "name_display",
        "category_display",
        "business",
        "price_display",
        "duration_display",
        "buffer_display",
        "colored_active",
    )
    list_filter = ("business", TenantCategoryListFilter, "is_active")
    search_fields = ("name", "category__name")
    ordering = ("name",)

    @display(description="Активна", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active

    @display(description="Услуга", ordering="name")
    def name_display(self, obj):
        return obj.name

    @display(description="Категория", ordering="category__name")
    def category_display(self, obj):
        return obj.category.name if obj.category_id else "—"

    @display(description="Цена", ordering="price")
    def price_display(self, obj):
        return _format_price_kzt(obj.price)

    @display(description="Длительность", ordering="duration")
    def duration_display(self, obj):
        return _format_duration_ru(obj.duration)

    @display(description="Буфер", ordering="buffer_time")
    def buffer_display(self, obj):
        return _format_duration_ru(obj.buffer_time)


@admin.register(Client)
class ClientAdmin(TenantScopedAdminMixin, ModelAdmin):
    list_display = (
        "name_display",
        "business",
        "phone_display",
        "channel_display",
        "dialogs_link",
        "reply_link",
        "ai_failure_count_display",
        "colored_active",
    )
    list_filter = ("business", "is_active")
    search_fields = ("name", "phone", "telegram_id", "whatsapp_id")
    ordering = ("name", "phone")

    @display(description="Активен", label={True: "success", False: "danger"})
    def colored_active(self, obj):
        return obj.is_active

    @display(description="Клиент", ordering="name")
    def name_display(self, obj):
        # Fall back to phone when the name is missing — same shape as
        # Client.__str__ but goes through here so the column gets a
        # Russian header without changing the model contract.
        return obj.name or (str(obj.phone) if obj.phone else "—")

    @display(description="Телефон", ordering="phone")
    def phone_display(self, obj):
        return str(obj.phone) if obj.phone else "—"

    @display(description="Канал")
    def channel_display(self, obj):
        # Show which messenger(s) the client actually used. telegram_id
        # / whatsapp_id are populated when the bot first sees a message
        # from that channel — empty means the client has never used it.
        tags = []
        if obj.telegram_id:
            tags.append("TG")
        if obj.whatsapp_id:
            tags.append("WA")
        return " / ".join(tags) if tags else "—"

    @display(description="Сбои AI", ordering="ai_failure_count")
    def ai_failure_count_display(self, obj):
        # Hide zeros so a column scan highlights only clients with
        # actual failures.
        count = obj.ai_failure_count or 0
        return str(count) if count else ""

    @display(description="Диалог")
    def dialogs_link(self, obj):
        url = f"{reverse('admin:bookings_conversationmessage_inbox')}?client={obj.id}"
        return format_html('<a href="{}">Открыть</a>', url)

    @display(description="Ответ")
    def reply_link(self, obj):
        url = f"{reverse('admin:bookings_conversationmessage_inbox')}?client={obj.id}"
        return format_html('<a href="{}">Написать</a>', url)


class TenantMasterListFilter(admin.SimpleListFilter):
    """Master filter that scopes the dropdown to the request's businesses.

    Django's default RelatedFieldListFilter shows every Master row in the
    database — for an owner that means foreign salons' master names leak
    into the filter sidebar (info disclosure), and Master.__str__ adds a
    "(Business name)" suffix that's pure noise inside a single-tenant view.

    This filter:
    - Restricts the dropdown to masters from the user's businesses.
    - Renders plain ``full_name`` for owners; for super_admins (who see
      multiple tenants) keeps the business suffix so duplicates stay
      distinguishable.
    """

    title = "Мастер"
    parameter_name = "master"

    def lookups(self, request, model_admin):
        queryset = Master.objects.select_related("business").filter(is_active=True)
        business_ids = _get_request_business_ids(request)
        if business_ids is not None:
            queryset = queryset.filter(business_id__in=business_ids)
        is_super = bool(getattr(request.user, "is_superuser", False))
        return [
            (
                master.id,
                f"{master.full_name} ({master.business.name})"
                if is_super
                else master.full_name,
            )
            for master in queryset.order_by("full_name")
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value:
            return queryset.filter(master_id=value)
        return queryset


class BookingAdminCreateForm(forms.ModelForm):
    """Add-form для ручной записи через owner-панель.

    Только нужные поля; статус по умолчанию — CONFIRMED (запись по телефону
    уже подтверждена администратором). Запись пишется через
    services.create_appointment, поэтому здесь не дублируем guard'ы.
    """

    class Meta:
        model = Booking
        fields = ("client", "master", "service", "start_time", "status", "notes")
        widgets = {
            "start_time": admin.widgets.AdminSplitDateTime,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get("status"):
            self.initial["status"] = Booking.Status.CONFIRMED
        self.fields["status"].choices = [
            (Booking.Status.CONFIRMED, "Подтверждена"),
            (Booking.Status.PENDING, "Ожидает подтверждения"),
        ]
        self.fields["notes"].required = False


@admin.register(Booking)
class BookingAdmin(TenantScopedAdminMixin, ModelAdmin):
    business_related_fields = ("client", "master", "service")
    actions = ("mark_confirmed", "mark_cancelled", "mark_no_show")
    date_hierarchy = "start_time"
    ordering = ("-start_time",)
    list_display = (
        "id",
        "colored_status",
        "client_display",
        "master_display",
        "service_display",
        "start_time_display",
        "end_time_display",
        "business",
    )
    list_display_links = ("id", "client_display")
    list_filter = ("status", "business", TenantMasterListFilter)
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

    def has_add_permission(self, request):
        if request.user.is_superuser:
            return True
        return bool(self.get_admin_business_ids(request))

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (
                    "Новая запись",
                    {
                        "fields": (
                            "client",
                            "master",
                            "service",
                            "start_time",
                            "status",
                            "notes",
                        )
                    },
                ),
            )
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return super().get_readonly_fields(request, obj)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = BookingAdminCreateForm
        return super().get_form(request, obj, **kwargs)

    def _resolve_creation_business(self, request, form):
        """Owner — business из его membership; superadmin — единственного
        ему доступного, либо None если он мульти-tenant (тогда ошибка)."""
        business = form.cleaned_data.get("business")
        if business is not None:
            return business
        primary = get_primary_business(request)
        if primary is not None:
            return primary
        # Superadmin без membership: пытаемся вывести business из выбранных FK.
        for field_name in ("client", "master", "service"):
            related = form.cleaned_data.get(field_name)
            if related is not None and getattr(related, "business_id", None):
                return related.business
        return None

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)

        business = self._resolve_creation_business(request, form)
        if business is None:
            raise ValidationError(
                "Не удалось определить салон для новой записи."
            )

        client_data = {
            "source": "manual_admin",
            "created_by_user_id": request.user.id,
            "created_by_username": request.user.get_username(),
        }
        booking = create_appointment(
            business=business,
            master=form.cleaned_data["master"],
            service=form.cleaned_data["service"],
            client=form.cleaned_data["client"],
            start_time=form.cleaned_data["start_time"],
            client_data=client_data,
            status=form.cleaned_data.get("status") or Booking.Status.CONFIRMED,
            notes=form.cleaned_data.get("notes", "") or "",
        )
        # Подменяем атрибуты unsaved-obj на свежесозданный booking, чтобы
        # стандартный admin pipeline (LogEntry, response_add) отработал
        # корректно без повторного obj.save().
        obj.pk = booking.pk
        obj.id = booking.pk
        obj.business = business
        obj.business_id = business.id
        obj.end_time = booking.end_time
        obj.service_duration = booking.service_duration
        obj.service_buffer_time = booking.service_buffer_time
        obj.client_data = booking.client_data
        obj.created_at = booking.created_at
        obj.updated_at = booking.updated_at
        obj._state.adding = False

        create_audit_log(
            business=business,
            client=booking.client,
            booking=booking,
            actor_type="human",
            event_type="admin_booking_manual_create",
            channel="admin",
            payload={
                "admin_user_id": request.user.id,
                "admin_username": request.user.get_username(),
                "start_time": booking.start_time.isoformat(),
                "status": booking.status,
            },
        )

    @display(description="Статус", label=BOOKING_STATUS_LABELS)
    def colored_status(self, obj):
        return obj.status

    @display(description="Клиент", ordering="client__name")
    def client_display(self, obj):
        # Bypass Client.__str__ to drop the redundant phone-fallback noise
        # in admin lists — name first, phone only when name is empty.
        return obj.client.name or (str(obj.client.phone) if obj.client.phone else "—")

    @display(description="Мастер", ordering="master__full_name")
    def master_display(self, obj):
        # Master.__str__ appends "(Business name)" which is noise for an
        # owner already inside their own tenant context. Show plain name.
        return obj.master.full_name if obj.master_id else "—"

    @display(description="Услуга", ordering="service__name")
    def service_display(self, obj):
        # Same reason as master_display — strip the business-name suffix
        # baked into Service.__str__.
        return obj.service.name if obj.service_id else "—"

    @display(description="Начало", ordering="start_time")
    def start_time_display(self, obj):
        return obj.start_time

    @display(description="Конец", ordering="end_time")
    def end_time_display(self, obj):
        return obj.end_time

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
    list_display = (
        "created_at",
        "client",
        "channel",
        "role",
        "short_content",
        "reply_link",
        "business",
    )
    list_display_links = ("created_at", "client")
    list_filter = ("business", "channel", "role")
    search_fields = ("client__phone", "content")

    def has_module_permission(self, request):
        if is_business_owner_mode(request):
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if is_business_owner_mode(request):
            return False
        return super().has_view_permission(request, obj=obj)

    def has_add_permission(self, request):
        return False

    def changelist_view(self, request, extra_context=None):
        if is_business_owner_mode(request):
            return redirect(reverse("admin:bookings_conversationmessage_inbox"))
        return super().changelist_view(request, extra_context=extra_context)

    @display(description="Сообщение")
    def short_content(self, obj):
        content = (obj.content or "").strip().replace("\n", " ")
        return content if len(content) <= 90 else f"{content[:87]}..."

    @display(description="Ответ")
    def reply_link(self, obj):
        url = (
            f"{reverse('admin:bookings_outboundmessage_add')}"
            f"?client={obj.client_id}&channel={obj.channel}"
        )
        return format_html('<a href="{}">Ответить</a>', url)


    def get_urls(self):
        return [
            path(
                "inbox/",
                self.admin_site.admin_view(self.inbox_view),
                name="bookings_conversationmessage_inbox",
            ),
            path(
                "inbox/set-thread-mode/",
                self.admin_site.admin_view(self.set_thread_mode_view),
                name="bookings_conversationmessage_set_thread_mode",
            ),
            *super().get_urls(),
        ]

    def get_inbox_client_queryset(self, request):
        queryset = Client.objects.select_related("business")
        business_ids = self.get_admin_business_ids(request)
        if business_ids is not None:
            queryset = queryset.filter(business_id__in=business_ids)
        _last_msg_qs = ConversationMessage.objects.filter(
            client=OuterRef("pk")
        ).order_by("-created_at", "-id")
        return queryset.annotate(
            last_message_at=Max("conversation_messages__created_at"),
            message_count=Count("conversation_messages"),
            last_message_channel=Subquery(_last_msg_qs.values("channel")[:1]),
            last_message_content=Subquery(_last_msg_qs.values("content")[:1]),
            last_user_message_at=Max(
                "conversation_messages__created_at",
                filter=Q(conversation_messages__role=USER_ROLE),
            ),
            last_reply_message_at=Max(
                "conversation_messages__created_at",
                filter=Q(conversation_messages__role__in=REPLY_ROLES),
            ),
        ).filter(message_count__gt=0)

    def get_selected_inbox_client(self, request, clients_queryset):
        client_id = request.GET.get("client") or request.POST.get("client_id")
        if client_id:
            try:
                return clients_queryset.get(pk=client_id)
            except (Client.DoesNotExist, ValueError):
                return None
        return clients_queryset.order_by("-last_message_at", "name").first()

    def get_selected_inbox_channel(self, request, *, selected_client=None, dialogs=None):
        requested_channel = request.GET.get("channel") or request.POST.get("channel")
        valid_channels = {choice[0] for choice in ConversationMessage.Channel.choices}
        if requested_channel in valid_channels:
            return requested_channel
        if selected_client is not None and dialogs:
            for dialog in dialogs:
                if (
                    dialog["client"].pk == selected_client.pk
                    and dialog.get("channel") in valid_channels
                ):
                    return dialog["channel"]
        selected_channel = (
            get_client_channel(selected_client) if selected_client else "telegram"
        )
        if selected_channel not in valid_channels:
            return ConversationMessage.Channel.TELEGRAM
        return selected_channel

    def get_inbox_dialogs(self, clients_queryset, *, status_filter: str = "all"):
        dialogs = []
        now = timezone.now()
        stale_threshold = now - timedelta(hours=2)
        active_threshold = now - timedelta(days=7)
        for client in clients_queryset.order_by("-last_message_at", "name")[:60]:
            lum_at = client.last_user_message_at
            lrm_at = client.last_reply_message_at
            needs_attention = bool(lum_at and (lrm_at is None or lum_at > lrm_at))
            is_stale = bool(needs_attention and lum_at and lum_at <= stale_threshold)
            is_active = bool(client.last_message_at and client.last_message_at >= active_threshold)
            if status_filter == "active" and not is_active:
                continue
            if status_filter == "attention" and not is_stale:
                continue
            channel = client.last_message_channel or get_client_channel(client)
            last_message = (
                type("_Msg", (), {
                    "content": client.last_message_content,
                    "channel": channel,
                })()
                if client.last_message_at
                else None
            )
            dialogs.append(
                {
                    "client": client,
                    "last_message": last_message,
                    "channel": channel,
                    "message_count": client.message_count,
                    "last_message_at": client.last_message_at,
                    "needs_attention": needs_attention,
                    "is_stale": is_stale,
                }
            )
        return dialogs

    def set_thread_mode_view(self, request):
        if request.method != "POST":
            return HttpResponseBadRequest("POST required.")

        client_id = request.POST.get("client_id")
        channel = request.POST.get("channel")
        mode = request.POST.get("mode")
        valid_channels = {choice[0] for choice in ConversationMessage.Channel.choices}
        allowed_modes = {
            ConversationThread.Mode.BOT_ACTIVE,
            ConversationThread.Mode.HUMAN_TAKEOVER,
        }
        if channel not in valid_channels or mode not in allowed_modes:
            return HttpResponseBadRequest("Invalid thread mode request.")

        clients_queryset = self.get_inbox_client_queryset(request)
        try:
            selected_client = clients_queryset.get(pk=client_id)
        except (Client.DoesNotExist, ValueError):
            return HttpResponseBadRequest("Client not found.")

        thread = get_or_create_conversation_thread(
            business=selected_client.business,
            client=selected_client,
            channel=channel,
        )
        set_thread_mode(thread, mode)
        return redirect(
            f"{reverse('admin:bookings_conversationmessage_inbox')}"
            f"?client={selected_client.pk}&channel={channel}"
        )

    def send_owner_inbox_reply(self, request, *, client, channel: str, text: str):
        if channel == "unknown":
            raise ValidationError("У клиента нет канала для ответа.")
        if channel == "telegram" and not client.telegram_id:
            raise ValidationError("У клиента нет Telegram для ответа.")
        if channel == "whatsapp" and not (client.whatsapp_id or client.phone):
            raise ValidationError("У клиента нет WhatsApp или телефона для ответа.")

        outbound_message = OutboundMessage.objects.create(
            business=client.business,
            client=client,
            channel=channel,
            recipient=get_client_recipient(client, channel),
            message_type="manual_reply",
            text=text,
        )
        ConversationMessage.objects.create(
            business=client.business,
            client=client,
            channel=channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=text,
        )
        thread = get_or_create_conversation_thread(
            business=client.business,
            client=client,
            channel=channel,
        )
        pause_bot_for_human_reply(thread)
        create_audit_log(
            business=client.business,
            client=client,
            outbound_message=outbound_message,
            actor_type="human",
            event_type="manual_reply_sent",
            channel=channel,
            payload={
                "source": "owner_inbox",
                "actor_id": request.user.id,
                "actor_name": request.user.get_username(),
            },
        )
        return dispatch_outbound_delivery(outbound_message.id)

    def inbox_view(self, request):
        status_filter = request.GET.get("status") or "all"
        if status_filter not in {"all", "active", "attention"}:
            status_filter = "all"
        clients_queryset = self.get_inbox_client_queryset(request)
        dialogs = self.get_inbox_dialogs(clients_queryset, status_filter=status_filter)
        selected_client = self.get_selected_inbox_client(request, clients_queryset)
        if selected_client is None and dialogs:
            selected_client = dialogs[0]["client"]
        elif selected_client is not None and not any(
            dialog["client"].pk == selected_client.pk for dialog in dialogs
        ):
            selected_client = dialogs[0]["client"] if dialogs else None

        if request.method == "POST":
            form = OwnerInboxReplyForm(request.POST)
            if form.is_valid():
                selected_client = self.get_selected_inbox_client(request, clients_queryset)
                if selected_client is None:
                    messages.error(request, "Клиент не найден или недоступен.")
                else:
                    try:
                        dispatch_result = self.send_owner_inbox_reply(
                            request,
                            client=selected_client,
                            channel=form.cleaned_data["channel"],
                            text=form.cleaned_data["text"].strip(),
                        )
                    except ValidationError as exc:
                        messages.error(request, "; ".join(exc.messages))
                    else:
                        status = dispatch_result.get("status", OutboundMessage.Status.QUEUED)
                        messages.success(request, f"Ответ отправлен. Статус: {status}.")
                        return redirect(
                            f"{reverse('admin:bookings_conversationmessage_inbox')}"
                            f"?client={selected_client.pk}"
                            f"&channel={form.cleaned_data['channel']}"
                        )
            else:
                messages.error(request, "Напишите текст ответа.")

        selected_channel = self.get_selected_inbox_channel(
            request,
            selected_client=selected_client,
            dialogs=dialogs,
        )
        form = OwnerInboxReplyForm(
            initial={
                "client_id": selected_client.pk if selected_client else "",
                "channel": selected_channel,
            }
        )
        messages_queryset = ConversationMessage.objects.none()
        latest_booking = None
        conversation_thread = None
        available_channels = []
        if selected_client is not None:
            conversation_thread = get_or_create_conversation_thread(
                business=selected_client.business,
                client=selected_client,
                channel=selected_channel,
            )
            available_channels = list(
                ConversationMessage.objects.filter(
                    business=selected_client.business,
                    client=selected_client,
                )
                .order_by("channel")
                .values_list("channel", flat=True)
                .distinct()
            )
            messages_queryset = ConversationMessage.objects.filter(
                client=selected_client,
                business=selected_client.business,
                channel=selected_channel,
            ).order_by("created_at", "id")
            latest_booking = (
                Booking.objects.select_related("service", "master")
                .filter(client=selected_client, business=selected_client.business)
                .order_by("-start_time", "-id")
                .first()
            )

        context = {
            **self.admin_site.each_context(request),
            "title": "Диалоги",
            "opts": self.model._meta,
            "dialogs": dialogs,
            "selected_client": selected_client,
            "selected_channel": selected_channel,
            "available_channels": available_channels,
            "conversation_messages": list(messages_queryset)[-120:],
            "latest_booking": latest_booking,
            "conversation_thread": conversation_thread,
            "status_filter": status_filter,
            "reply_form": form,
            "inbox_url": reverse("admin:bookings_conversationmessage_inbox"),
            "set_thread_mode_url": reverse(
                "admin:bookings_conversationmessage_set_thread_mode"
            ),
            "table_url": reverse("admin:bookings_conversationmessage_changelist"),
        }
        return TemplateResponse(
            request,
            "admin/bookings/conversationmessage/inbox.html",
            context,
        )


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

    def has_module_permission(self, request):
        if is_business_owner_mode(request):
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if is_business_owner_mode(request):
            return False
        return super().has_view_permission(request, obj=obj)

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

    def has_module_permission(self, request):
        if is_business_owner_mode(request):
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if is_business_owner_mode(request):
            return False
        return super().has_view_permission(request, obj=obj)

    def has_add_permission(self, request):
        if is_business_owner_mode(request):
            return False
        if request.user.is_superuser:
            return True
        return bool(self.get_admin_business_ids(request))
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

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = OutboundMessageReplyForm
        return super().get_form(request, obj, **kwargs)

    def has_add_permission(self, request):
        if is_business_owner_mode(request):
            return False
        if request.user.is_superuser:
            return True
        return bool(self.get_admin_business_ids(request))

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (
                    "Ответ клиенту",
                    {
                        "fields": (
                            "client",
                            "booking",
                            "channel",
                            "text",
                        )
                    },
                ),
            )
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return super().get_readonly_fields(request, obj)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        client_id = request.GET.get("client")
        booking_id = request.GET.get("booking")
        channel = request.GET.get("channel")
        if client_id:
            initial["client"] = client_id
        if booking_id:
            initial["booking"] = booking_id
        if channel in {"telegram", "whatsapp"}:
            initial["channel"] = channel
        return initial

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)

        if obj.client_id is None:
            raise ValidationError("Нужно выбрать клиента.")

        if obj.booking_id and obj.booking.client_id != obj.client_id:
            raise ValidationError("Запись должна принадлежать выбранному клиенту.")

        obj.business_id = obj.client.business_id
        if not obj.channel:
            obj.channel = get_client_channel(obj.client)
        if obj.channel == "unknown":
            raise ValidationError(
                "У клиента нет канала для ответа. Нужен Telegram, WhatsApp или телефон."
            )
        if obj.channel == "telegram" and not obj.client.telegram_id:
            raise ValidationError("У клиента нет Telegram для ответа.")
        if obj.channel == "whatsapp" and not (obj.client.whatsapp_id or obj.client.phone):
            raise ValidationError("У клиента нет WhatsApp или телефона для ответа.")
        obj.recipient = get_client_recipient(obj.client, obj.channel)
        obj.message_type = "manual_reply"

        super().save_model(request, obj, form, change)

        ConversationMessage.objects.create(
            business=obj.business,
            client=obj.client,
            channel=obj.channel,
            role=ConversationMessage.Role.ASSISTANT,
            content=obj.text,
        )
        create_audit_log(
            business=obj.business,
            client=obj.client,
            booking=obj.booking,
            outbound_message=obj,
            actor_type="human",
            event_type="manual_reply_sent",
            channel=obj.channel,
            payload={
                "source": "admin",
                "actor_id": request.user.id,
                "actor_name": request.user.get_username(),
            },
        )
        dispatch_result = dispatch_outbound_delivery(obj.id)
        status = dispatch_result.get("status", OutboundMessage.Status.QUEUED)
        self.message_user(
            request,
            f"Сообщение отправлено в очередь. Статус: {status}.",
            level=messages.SUCCESS,
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
    change_list_template = "admin/bookings/auditlog/change_list.html"
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

    def has_module_permission(self, request):
        if is_business_owner_mode(request):
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if is_business_owner_mode(request):
            return False
        return super().has_view_permission(request, obj=obj)

    def changelist_view(self, request, extra_context=None):
        show_technical = request.GET.get("show_technical") == "1"
        request.show_technical_audit_events = show_technical
        if "show_technical" in request.GET:
            request.GET = request.GET.copy()
            request.GET.pop("show_technical", None)
            request.META["QUERY_STRING"] = request.GET.urlencode()
        extra_context = extra_context or {}
        extra_context["show_technical"] = show_technical
        return super().changelist_view(request, extra_context=extra_context)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        has_explicit_event_filter = any(
            key.startswith("event_type") for key in request.GET.keys()
        )
        if (
            getattr(request, "show_technical_audit_events", False)
            or request.GET.get("show_technical") == "1"
            or has_explicit_event_filter
        ):
            return queryset
        return queryset.exclude(event_type__in=TECHNICAL_AUDIT_EVENT_TYPES)


BILLING_WINDOW_DAYS = 30
# OpenAI gpt-4o-mini pricing (per 1M tokens, USD) — keep here as a single
# source of truth. When pricing changes or a new model joins, update this
# table; the summary view will pick it up immediately.
AI_PRICING_USD_PER_MILLION = {
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "default": {"prompt": 0.15, "completion": 0.60},
}
USD_TO_KZT = 450


def _estimate_cost_kzt(prompt_tokens: int, completion_tokens: int, model_name: str = "") -> float:
    pricing = AI_PRICING_USD_PER_MILLION.get(
        model_name or "default", AI_PRICING_USD_PER_MILLION["default"]
    )
    prompt_cost_usd = (prompt_tokens or 0) * pricing["prompt"] / 1_000_000
    completion_cost_usd = (completion_tokens or 0) * pricing["completion"] / 1_000_000
    return (prompt_cost_usd + completion_cost_usd) * USD_TO_KZT


@admin.register(AIInteractionLog)
class AIInteractionLogAdmin(TenantScopedAdminMixin, ModelAdmin):
    change_list_template = "admin/bookings/aiinteractionlog/change_list.html"
    list_display = (
        "id",
        "business",
        "model_name",
        "colored_status",
        "prompt_tokens",
        "completion_tokens",
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

    def changelist_view(self, request, extra_context=None):
        window_start = timezone.now() - timedelta(days=BILLING_WINDOW_DAYS)
        queryset = self.get_queryset(request).filter(created_at__gte=window_start)

        per_business = list(
            queryset.values("business_id", "business__name")
            .annotate(
                calls=Count("id"),
                prompt_total=Sum("prompt_tokens"),
                completion_total=Sum("completion_tokens"),
            )
            .order_by("business__name")
        )
        for row in per_business:
            row["prompt_total"] = row["prompt_total"] or 0
            row["completion_total"] = row["completion_total"] or 0
            row["estimated_cost_kzt"] = _estimate_cost_kzt(
                row["prompt_total"], row["completion_total"]
            )

        totals = {
            "calls": sum(r["calls"] for r in per_business),
            "prompt": sum(r["prompt_total"] for r in per_business),
            "completion": sum(r["completion_total"] for r in per_business),
        }
        totals["cost_kzt"] = _estimate_cost_kzt(totals["prompt"], totals["completion"])

        extra_context = extra_context or {}
        extra_context.update(
            {
                "billing_window_days": BILLING_WINDOW_DAYS,
                "billing_per_business": per_business,
                "billing_totals": totals,
                "billing_show_breakdown": request.user.is_superuser and len(per_business) > 1,
            }
        )
        return super().changelist_view(request, extra_context=extra_context)
