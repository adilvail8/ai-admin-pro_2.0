from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    BookingDetailView,
    BookingListCreateView,
    BookingRescheduleView,
    BookingStatusUpdateView,
    MeView,
    MembershipListView,
    OutboundMessageListView,
)


app_name = "api"

urlpatterns = [
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("memberships/", MembershipListView.as_view(), name="memberships"),
    path(
        "businesses/<int:business_id>/bookings/",
        BookingListCreateView.as_view(),
        name="business_bookings",
    ),
    path(
        "businesses/<int:business_id>/bookings/<int:pk>/",
        BookingDetailView.as_view(),
        name="business_booking_detail",
    ),
    path(
        "businesses/<int:business_id>/bookings/<int:pk>/reschedule/",
        BookingRescheduleView.as_view(),
        name="business_booking_reschedule",
    ),
    path(
        "businesses/<int:business_id>/bookings/<int:pk>/status/",
        BookingStatusUpdateView.as_view(),
        name="business_booking_status",
    ),
    path(
        "businesses/<int:business_id>/outbound-messages/",
        OutboundMessageListView.as_view(),
        name="business_outbound_messages",
    ),
]
