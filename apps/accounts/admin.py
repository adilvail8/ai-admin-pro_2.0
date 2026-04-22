from django.contrib import admin

from .models import BusinessMembership


@admin.register(BusinessMembership)
class BusinessMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "business", "role", "is_active", "updated_at")
    list_filter = ("role", "is_active", "business")
    search_fields = ("user__username", "user__email", "business__name")

