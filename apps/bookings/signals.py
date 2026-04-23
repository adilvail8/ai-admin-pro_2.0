from django.apps import apps
from django.db.utils import OperationalError, ProgrammingError
from django.db.models.signals import post_migrate
from django.dispatch import receiver


REMINDER_TASK_NAME = "bookings-process-pending-reminders"


@receiver(post_migrate)
def ensure_booking_periodic_tasks(sender, **kwargs):
    if sender.name != "apps.bookings":
        return
    if not apps.is_installed("django_celery_beat"):
        return

    try:
        interval_model = apps.get_model("django_celery_beat", "IntervalSchedule")
        periodic_task_model = apps.get_model("django_celery_beat", "PeriodicTask")
    except LookupError:
        return

    try:
        schedule, _ = interval_model.objects.get_or_create(
            every=1,
            period=interval_model.MINUTES,
        )
        periodic_task_model.objects.update_or_create(
            name=REMINDER_TASK_NAME,
            defaults={
                "interval": schedule,
                "task": "apps.bookings.tasks.process_pending_reminders",
                "enabled": True,
                "description": (
                    "Scans bookings every minute and queues reminders/follow-ups."
                ),
            },
        )
    except (OperationalError, ProgrammingError):
        # Beat tables may not be ready during partial migrate states.
        return
