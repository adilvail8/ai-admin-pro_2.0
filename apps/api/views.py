from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import BusinessMembership
from apps.bookings.models import Booking, Client, Master, OutboundMessage, Service
from apps.bookings.services import get_available_slots, serialize_slot
from apps.bookings.tasks import request_outbound_resend, request_outbound_retry

from .mixins import BusinessContextMixin, BusinessScopedQuerysetMixin
from .permissions import BusinessAccessPermission
from .serializers import (
    AvailabilityQuerySerializer,
    AvailabilitySlotSerializer,
    BookingCreateSerializer,
    BookingReadSerializer,
    BookingRescheduleSerializer,
    BookingStatusUpdateSerializer,
    BusinessDetailSerializer,
    BusinessMembershipSerializer,
    ClientDetailSerializer,
    ClientListSerializer,
    MasterListSerializer,
    OutboundMessageSerializer,
    ServiceListSerializer,
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
    pagination_class = None

    def get_queryset(self):
        return (
            BusinessMembership.objects.select_related("business")
            .filter(user=self.request.user, is_active=True)
            .order_by("business__name")
        )


class BusinessDetailView(BusinessContextMixin, APIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]

    def get(self, request, *args, **kwargs):
        serializer = BusinessDetailSerializer(self.business)
        return Response(serializer.data)


class MasterListView(BusinessScopedQuerysetMixin, generics.ListAPIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
    serializer_class = MasterListSerializer
    pagination_class = None
    queryset = Master.objects.filter(is_active=True).order_by("full_name")


class ServiceListView(BusinessScopedQuerysetMixin, generics.ListAPIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
    serializer_class = ServiceListSerializer
    pagination_class = None
    queryset = (
        Service.objects.select_related("category")
        .filter(is_active=True)
        .order_by("name")
    )


class AvailabilityView(BusinessContextMixin, APIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]

    def get(self, request, *args, **kwargs):
        query_serializer = AvailabilityQuerySerializer(
            data=request.query_params
        )
        query_serializer.is_valid(raise_exception=True)
        try:
            slots = get_available_slots(
                self.business,
                target_date=query_serializer.validated_data["date"],
                service_id=query_serializer.validated_data["service_id"],
                master_id=query_serializer.validated_data.get("master_id"),
            )
        except DjangoValidationError as error:
            raise ValidationError(error.messages)

        response_serializer = AvailabilitySlotSerializer(
            [serialize_slot(slot) for slot in slots],
            many=True,
        )
        return Response(response_serializer.data)


class BookingListCreateView(
    BusinessScopedQuerysetMixin,
    generics.ListCreateAPIView,
):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
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
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
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
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
    serializer_class = OutboundMessageSerializer
    queryset = OutboundMessage.objects.select_related(
        "business",
        "booking",
        "client",
    ).order_by("-created_at")


class OutboundMessageBaseView(BusinessScopedQuerysetMixin, generics.GenericAPIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
    serializer_class = OutboundMessageSerializer
    queryset = OutboundMessage.objects.select_related(
        "business",
        "booking",
        "client",
    ).order_by("-created_at")

    def build_action_response(self, outbound_message, delivery_result):
        outbound_message.refresh_from_db()
        payload = OutboundMessageSerializer(outbound_message).data
        payload["delivery_status"] = delivery_result["status"]
        if delivery_result.get("delivery_task_id"):
            payload["delivery_task_id"] = delivery_result["delivery_task_id"]
        if delivery_result.get("retry_eta"):
            payload["retry_eta"] = delivery_result["retry_eta"]
        return Response(payload)


class OutboundMessageRetryView(OutboundMessageBaseView):
    def post(self, request, *args, **kwargs):
        outbound_message = self.get_object()
        try:
            updated_message, delivery_result = request_outbound_retry(
                outbound_message=outbound_message,
                actor_type="human",
                actor_id=request.user.id,
                actor_name=request.user.get_username(),
            )
        except DjangoValidationError as error:
            raise ValidationError(error.messages)
        return self.build_action_response(updated_message, delivery_result)


class OutboundMessageResendView(OutboundMessageBaseView):
    def post(self, request, *args, **kwargs):
        outbound_message = self.get_object()
        try:
            updated_message, delivery_result = request_outbound_resend(
                outbound_message=outbound_message,
                actor_type="human",
                actor_id=request.user.id,
                actor_name=request.user.get_username(),
            )
        except DjangoValidationError as error:
            raise ValidationError(error.messages)
        return self.build_action_response(updated_message, delivery_result)


class ClientListView(BusinessScopedQuerysetMixin, generics.ListAPIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
    serializer_class = ClientListSerializer
    queryset = Client.objects.filter(is_active=True).order_by("-created_at")

    def get_queryset(self):
        queryset = super().get_queryset()
        search = self.request.query_params.get("search", "").strip()
        phone = self.request.query_params.get("phone", "").strip()

        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(phone__icontains=search)
                | Q(telegram_id__icontains=search)
                | Q(whatsapp_id__icontains=search)
            )
        if phone:
            queryset = queryset.filter(phone__icontains=phone)

        return queryset


class ClientDetailView(BusinessScopedQuerysetMixin, generics.RetrieveAPIView):
    permission_classes = [BusinessAccessPermission(BusinessMembership.Role.STAFF)]
    serializer_class = ClientDetailSerializer
    queryset = Client.objects.order_by("-created_at")

