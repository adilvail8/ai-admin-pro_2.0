import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def backfill_conversation_threads(apps, schema_editor):
    ConversationMessage = apps.get_model("bookings", "ConversationMessage")
    ConversationThread = apps.get_model("bookings", "ConversationThread")

    existing_keys = set(
        ConversationThread.objects.values_list(
            "business_id",
            "client_id",
            "channel",
        )
    )
    now = timezone.now()
    threads = []
    for business_id, client_id, channel in (
        ConversationMessage.objects.order_by()
        .values_list("business_id", "client_id", "channel")
        .distinct()
    ):
        key = (business_id, client_id, channel)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        threads.append(
            ConversationThread(
                business_id=business_id,
                client_id=client_id,
                channel=channel,
                mode="bot_active",
                created_at=now,
                updated_at=now,
            )
        )

    if threads:
        ConversationThread.objects.bulk_create(threads, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0012_alter_client_phone_nullable"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConversationThread",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "channel",
                    models.CharField(
                        choices=[("telegram", "Telegram"), ("whatsapp", "WhatsApp")],
                        max_length=20,
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("bot_active", "Bot active"),
                            ("human_takeover", "Human takeover"),
                            ("bot_paused_until", "Bot paused until"),
                        ],
                        default="bot_active",
                        max_length=32,
                    ),
                ),
                ("bot_paused_until", models.DateTimeField(blank=True, null=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation_threads",
                        to="bookings.business",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation_threads",
                        to="bookings.client",
                    ),
                ),
            ],
            options={
                "verbose_name": "conversation thread",
                "verbose_name_plural": "conversation threads",
                "ordering": ("business_id", "client_id", "channel"),
            },
        ),
        migrations.AddConstraint(
            model_name="conversationthread",
            constraint=models.UniqueConstraint(
                fields=("business", "client", "channel"),
                name="uniq_conversation_thread_per_client_channel",
            ),
        ),
        migrations.AddIndex(
            model_name="conversationthread",
            index=models.Index(
                fields=["business", "client", "channel", "mode"],
                name="bookings_thread_mode_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="conversationthread",
            index=models.Index(
                fields=["bot_paused_until"],
                name="bookings_thread_paused_idx",
            ),
        ),
        migrations.RunPython(
            backfill_conversation_threads,
            migrations.RunPython.noop,
        ),
    ]
