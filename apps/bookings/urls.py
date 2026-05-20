from django.urls import path

from .health_checks import OperationalHealthCheckView
from .views import (
    green_api_webhook,
    healthcheck,
    messenger_webhook,
    outbound_delivery_webhook,
    telegram_webhook,
    whatsapp_webhook,
)


app_name = "bookings"

urlpatterns = [
    path("webhooks/messenger/", messenger_webhook, name="messenger_webhook"),
    path(
        "webhooks/telegram/<int:business_id>/<str:secret>/",
        telegram_webhook,
        name="telegram_webhook",
    ),
    path(
        "webhooks/green-api/",
        green_api_webhook,
        name="green_api_webhook",
    ),
    path(
        "webhooks/whatsapp/<int:business_id>/",
        whatsapp_webhook,
        name="whatsapp_webhook",
    ),
    path(
        "webhooks/outbound-delivery/",
        outbound_delivery_webhook,
        name="outbound_delivery_webhook",
    ),
    path("health/", healthcheck, name="healthcheck"),
    path(
        "health/detailed/",
        OperationalHealthCheckView.as_view(),
        name="healthcheck_detailed",
    ),
]
