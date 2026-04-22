from django.contrib.auth import get_user_model
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import BusinessMembership
from apps.bookings.models import Booking, OutboundMessage

from .permissions import HasBusinessAccess
from .serializers import (
    BookingSerializer,
    BusinessMembershipSerializer,
    OutboundMessageSerializer,
    UserSerializer,
)


User = get_user_model()


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class MembershipListView(generics.ListAPIView):
    permission_classes = [HasBusinessAccess]
    serializer_class = BusinessMembershipSerializer

    def get_queryset(self):
        return (
            BusinessMembership.objects.select_related("business")
            .filter(user=self.request.user, is_active=True)
            .order_by("business__name")
        )


class BusinessScopedQuerysetMixin:
    permission_classes = [HasBusinessAccess]

    def get_business_ids(self):
        return list(
            BusinessMembership.objects.filter(
                user=self.request.user,
                is_active=True,
            ).values_list("business_id", flat=True)
        )


class BookingListView(BusinessScopedQuerysetMixin, generics.ListAPIView):
    serializer_class = BookingSerializer

    def get_queryset(self):
        queryset = (
            Booking.objects.select_related("business", "client", "master", "service")
            .filter(business_id__in=self.get_business_ids())
            .order_by("start_time")
        )
        business_id = self.request.query_params.get("business_id")
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        return queryset


class OutboundMessageListView(BusinessScopedQuerysetMixin, generics.ListAPIView):
    serializer_class = OutboundMessageSerializer

    def get_queryset(self):
        queryset = (
            OutboundMessage.objects.select_related("business", "booking", "client")
            .filter(business_id__in=self.get_business_ids())
            .order_by("-created_at")
        )
        business_id = self.request.query_params.get("business_id")
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        return queryset

