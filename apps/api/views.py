from django.contrib.auth import get_user_model
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import BusinessMembership
from apps.bookings.models import Booking, OutboundMessage

from .mixins import BusinessScopedQuerysetMixin
from .permissions import BusinessAccessPermission
from .serializers import (
    BookingCreateSerializer,
    BookingReadSerializer,
    BookingRescheduleSerializer,
    BookingStatusUpdateSerializer,
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
    permission_classes = [IsAuthenticated]
    serializer_class = BusinessMembershipSerializer

    def get_queryset(self):
        return (
            BusinessMembership.objects.select_related("business")
            .filter(user=self.request.user, is_active=True)
            .order_by("business__name")
        )


class BookingListCreateView(
    BusinessScopedQuerysetMixin,
    generics.ListCreateAPIView,
):
    permission_classes = [
        IsAuthenticated,
        BusinessAccessPermission(BusinessMembership.Role.STAFF),
    ]
    queryset = Booking.objects.select_related(
        "business",
        "client",
        "master",
        "service",
    ).order_by("start_time")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return BookingCreateSerializer
        return BookingReadSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["business"] = self.business
        return context

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = serializer.save()
        response_serializer = BookingReadSerializer(
            booking,
            context=self.get_serializer_context(),
        )
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
        )


class BookingBaseView(BusinessScopedQuerysetMixin, generics.GenericAPIView):
    permission_classes = [
        IsAuthenticated,
        BusinessAccessPermission(BusinessMembership.Role.STAFF),
    ]
    queryset = Booking.objects.select_related(
        "business",
        "client",
        "master",
        "service",
    ).order_by("start_time")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["business"] = self.business
        return context


class BookingDetailView(BookingBaseView, generics.RetrieveAPIView):
    serializer_class = BookingReadSerializer


class BookingRescheduleView(BookingBaseView):
    serializer_class = BookingRescheduleSerializer

    def patch(self, request, *args, **kwargs):
        booking = self.get_object()
        serializer = self.get_serializer(
            booking,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        updated_booking = serializer.save()
        response_serializer = BookingReadSerializer(
            updated_booking,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data)


class BookingStatusUpdateView(BookingBaseView):
    serializer_class = BookingStatusUpdateSerializer

    def patch(self, request, *args, **kwargs):
        booking = self.get_object()
        serializer = self.get_serializer(
            booking,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        updated_booking = serializer.save()
        response_serializer = BookingReadSerializer(
            updated_booking,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data)


class OutboundMessageListView(BusinessScopedQuerysetMixin, generics.ListAPIView):
    permission_classes = [
        IsAuthenticated,
        BusinessAccessPermission(BusinessMembership.Role.STAFF),
    ]
    serializer_class = OutboundMessageSerializer
    queryset = OutboundMessage.objects.select_related(
        "business",
        "booking",
        "client",
    ).order_by("-created_at")

