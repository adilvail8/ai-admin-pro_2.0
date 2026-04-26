from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AvailabilityView,
    BookingDetailView,
    BookingListCreateView,
    BookingRescheduleView,
    BookingStatusUpdateView,
    BusinessDetailView,
    ClientDetailView,
    ClientListView,
    MasterListView,
    MeView,
    MembershipListView,
    OutboundMessageListView,
    OutboundMessageResendView,
    OutboundMessageRetryView,
    ServiceListView,
)


app_name = "api"

urlpatterns = [
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("memberships/", MembershipListView.as_view(), name="memberships"),
    path(
        "businesses/<int:business_id>/",
        BusinessDetailView.as_view(),
        name="business_detail",
    ),
    path(
        "businesses/<int:business_id>/masters/",
        MasterListView.as_view(),
        name="business_masters",
    ),
    path(
        "businesses/<int:business_id>/services/",
        ServiceListView.as_view(),
        name="business_services",
    ),
    path(
        "businesses/<int:business_id>/availability/",
        AvailabilityView.as_view(),
        name="business_availability",
    ),
    path(
        "businesses/<int:business_id>/bookings/",
        BookingListCreateView.as_view(),
        name="business_bookings",
    ),
    path(
        "businesses/<int:business_id>/clients/",
        ClientListView.as_view(),
        name="business_clients",
    ),
    path(
        "businesses/<int:business_id>/clients/<int:pk>/",
        ClientDetailView.as_view(),
        name="business_client_detail",
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
    path(
        "businesses/<int:business_id>/outbound-messages/<int:pk>/retry/",
        OutboundMessageRetryView.as_view(),
        name="business_outbound_message_retry",
    ),
    path(
        "businesses/<int:business_id>/outbound-messages/<int:pk>/resend/",
        OutboundMessageResendView.as_view(),
        name="business_outbound_message_resend",
    ),
]
