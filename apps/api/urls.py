from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    BookingListView,
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
    path("bookings/", BookingListView.as_view(), name="bookings"),
    path("outbound-messages/", OutboundMessageListView.as_view(), name="outbound_messages"),
]
