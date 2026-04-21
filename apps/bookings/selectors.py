from django.db.models import QuerySet

from .models import Booking, Business


def get_business_bookings(*, business: Business) -> QuerySet[Booking]:
    return (
        Booking.objects.filter(business=business)
        .select_related("master", "service")
        .order_by("start_time")
    )

