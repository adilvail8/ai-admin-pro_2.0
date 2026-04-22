from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("bookings", "0006_business_address_business_brand_name_business_city_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusinessMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("owner", "Owner"),
                            ("admin", "Admin"),
                            ("staff", "Staff"),
                        ],
                        default="staff",
                        max_length=20,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="memberships",
                        to="bookings.business",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="business_memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("business__name", "user__username"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "business"),
                        name="uniq_user_business_membership",
                    ),
                ],
            },
        ),
    ]

