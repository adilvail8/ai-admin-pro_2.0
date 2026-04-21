from django.contrib import admin

from .models import Booking, Business, Master, Service


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "mode", "is_active", "created_at")
    list_filter = ("mode", "is_active")
    search_fields = ("name", "slug")


@admin.register(Master)
class MasterAdmin(admin.ModelAdmin):
    list_display = ("full_name", "business", "specialization", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("full_name", "specialization")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "price", "duration", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("name",)


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "master", "service", "start_time", "status")
    list_filter = ("business", "status")
    search_fields = ("master__full_name", "service__name")

