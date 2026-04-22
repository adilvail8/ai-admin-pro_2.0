from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class BusinessMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", _("Owner")
        ADMIN = "admin", _("Admin")
        STAFF = "staff", _("Staff")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="business_memberships",
    )
    business = models.ForeignKey(
        "bookings.Business",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STAFF,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("business__name", "user__username")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "business"),
                name="uniq_user_business_membership",
            ),
        ]

    def __str__(self):
        return f"{self.user} -> {self.business} ({self.role})"

