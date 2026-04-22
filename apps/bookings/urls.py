from django.urls import path

from .views import (
    green_api_webhook,
    healthcheck,
    messenger_webhook,
    outbound_delivery_webhook,
    telegram_webhook,
)


app_name = "bookings"

urlpatterns = [
    path("webhooks/messenger/", messenger_webhook, name="messenger_webhook"),
    path(
        "webhooks/telegram/<str:secret>/",
        telegram_webhook,
        name="telegram_webhook",
    ),
    path(
        "webhooks/green-api/",
        green_api_webhook,
        name="green_api_webhook",
    ),
    path(
        "webhooks/outbound-delivery/",
        outbound_delivery_webhook,
        name="outbound_delivery_webhook",
    ),
    path("health/", healthcheck, name="healthcheck"),
]
