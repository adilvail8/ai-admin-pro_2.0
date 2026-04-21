from django.contrib import admin

from .models import Booking, Business, Client, ConversationMessage, Master, Service


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "timezone_name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")


@admin.register(Master)
class MasterAdmin(admin.ModelAdmin):
    list_display = ("full_name", "business", "specialization", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("full_name", "specialization")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "business",
        "price",
        "duration",
        "buffer_time",
        "is_active",
    )
    list_filter = ("business", "is_active")
    search_fields = ("name",)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
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
class BookingAdmin(admin.ModelAdmin):
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


@admin.register(ConversationMessage)
class ConversationMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "client", "channel", "role", "created_at")
    list_filter = ("business", "channel", "role")
    search_fields = ("client__phone", "content")
