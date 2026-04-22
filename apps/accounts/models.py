from django.conf import settings
from django.core.exceptions import ValidationError
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
        ordering = ("business_id", "user_id")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "business"),
                name="uniq_user_business_membership",
            ),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_role = self.role
        self._original_is_active = self.is_active

    def __str__(self):
        return f"{self.user} -> {self.business} ({self.role})"

    @property
    def is_active_owner(self):
        return self.role == self.Role.OWNER and self.is_active

    def validate_last_owner_guard(self):
        if not self.pk:
            return

        was_active_owner = (
            self._original_role == self.Role.OWNER and self._original_is_active
        )
        if not was_active_owner or self.is_active_owner:
            return

        other_active_owners_exist = BusinessMembership.objects.filter(
            business_id=self.business_id,
            role=self.Role.OWNER,
            is_active=True,
        ).exclude(pk=self.pk).exists()
        if not other_active_owners_exist:
            raise ValidationError(
                "Cannot remove or deactivate the last active owner."
            )

    def clean(self):
        super().clean()
        self.validate_last_owner_guard()

    def save(self, *args, **kwargs):
        self.full_clean()
        saved_instance = super().save(*args, **kwargs)
        self._original_role = self.role
        self._original_is_active = self.is_active
        return saved_instance

    def delete(self, *args, **kwargs):
        if self.is_active_owner:
            other_active_owners_exist = BusinessMembership.objects.filter(
                business_id=self.business_id,
                role=self.Role.OWNER,
                is_active=True,
            ).exclude(pk=self.pk).exists()
            if not other_active_owners_exist:
                raise ValidationError(
                    "Cannot delete the last active owner."
                )
        return super().delete(*args, **kwargs)

