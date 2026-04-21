from django.db.models import QuerySet

from .models import Booking, Client


def get_business_bookings(*, business_id: int) -> QuerySet[Booking]:
    return (
        Booking.objects.filter(business_id=business_id)
        .select_related("master", "service", "client")
        .order_by("start_time")
    )


def get_business_clients(*, business_id: int) -> QuerySet[Client]:
    return (
        Client.objects.filter(business_id=business_id, is_active=True)
        .order_by("name", "phone")
    )
