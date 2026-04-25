from django.contrib.auth import get_user_model
from datetime import date
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import BusinessMembership
from apps.bookings.models import (
    Booking,
    Business,
    Client,
    Master,
    OutboundMessage,
    Service,
)
from apps.bookings.services import create_appointment
from apps.bookings.services import reschedule_appointment, update_booking_status


User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "is_staff")


class BusinessSerializer(serializers.ModelSerializer):
    class Meta:
        model = Business
        fields = (
            "id",
            "name",
            "brand_name",
            "city",
            "address",
            "working_hours",
            "timezone_name",
            "is_active",
        )


class BusinessDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Business
        fields = (
            "id",
            "name",
            "brand_name",
            "city",
            "address",
            "working_hours",
            "timezone_name",
            "is_active",
        )
        read_only_fields = fields


class BusinessMembershipSerializer(serializers.ModelSerializer):
    business = BusinessSerializer()

    class Meta:
        model = BusinessMembership
        fields = ("id", "role", "is_active", "business")


class BookingReadSerializer(serializers.ModelSerializer):
    business_id = serializers.IntegerField(source="business.id", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)
    client_id = serializers.IntegerField(source="client.id", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    master_id = serializers.IntegerField(source="master.id", read_only=True)
    master_name = serializers.CharField(source="master.full_name", read_only=True)
    service_id = serializers.IntegerField(source="service.id", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = Booking
        fields = (
            "id",
            "business_id",
            "business_name",
            "client_id",
            "client_name",
            "master_id",
            "master_name",
            "service_id",
            "service_name",
            "start_time",
            "end_time",
            "status",
            "notes",
            "client_data",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class MasterListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Master
        fields = (
            "id",
            "full_name",
            "specialization",
            "working_hours",
            "is_active",
        )
        read_only_fields = fields


class ServiceListSerializer(serializers.ModelSerializer):
    category_id = serializers.IntegerField(source="category.id", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Service
        fields = (
            "id",
            "name",
            "category_id",
            "category_name",
            "price",
            "duration",
            "buffer_time",
            "is_active",
        )
        read_only_fields = fields


class ClientListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = (
            "id",
            "name",
            "phone",
            "telegram_id",
            "whatsapp_id",
            "allow_follow_up",
            "is_active",
            "created_at",
        )
        read_only_fields = fields


class ClientDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = (
            "id",
            "name",
            "phone",
            "external_id",
            "telegram_id",
            "whatsapp_id",
            "ai_failure_count",
            "allow_follow_up",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AvailabilityQuerySerializer(serializers.Serializer):
    date = serializers.DateField()
    service_id = serializers.IntegerField(min_value=1)
    master_id = serializers.IntegerField(min_value=1, required=False)


class AvailabilitySlotSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    master_id = serializers.IntegerField()
    master_name = serializers.CharField()


class BookingWriteTenantScopeMixin:
    TENANT_SCOPED_FIELDS = {
        "client": Client,
        "master": Master,
        "service": Service,
    }

    def apply_tenant_scope(self):
        business = self.context["business"]

        for field_name, model in self.TENANT_SCOPED_FIELDS.items():
            if field_name not in self.fields:
                continue
            self.fields[field_name].queryset = model.objects.filter(
                business=business,
                is_active=True,
            )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_tenant_scope()

    @property
    def business(self):
        return self.context["business"]


class BookingStartTimeValidationMixin:
    def validate_start_time(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError(
                "Cannot create a booking in the past."
            )
        return value


class BookingCreateSerializer(
    BookingWriteTenantScopeMixin,
    BookingStartTimeValidationMixin,
    serializers.Serializer,
):
    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.none())
    master = serializers.PrimaryKeyRelatedField(queryset=Master.objects.none())
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.none())
    start_time = serializers.DateTimeField()
    status = serializers.ChoiceField(
        choices=Booking.Status.choices,
        required=False,
        default=Booking.Status.PENDING,
    )
    client_data = serializers.JSONField(required=False, default=dict)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def create(self, validated_data):
        return create_appointment(
            business=self.business,
            client=validated_data["client"],
            master=validated_data["master"],
            service=validated_data["service"],
            start_time=validated_data["start_time"],
            client_data=validated_data.get("client_data", {}),
            status=validated_data.get("status", Booking.Status.PENDING),
            notes=validated_data.get("notes", ""),
        )


class BookingRescheduleSerializer(
    BookingWriteTenantScopeMixin,
    BookingStartTimeValidationMixin,
    serializers.Serializer,
):
    master = serializers.PrimaryKeyRelatedField(
        queryset=Master.objects.none(),
        required=False,
    )
    start_time = serializers.DateTimeField()

    def update(self, instance, validated_data):
        return reschedule_appointment(
            booking=instance,
            business=self.business,
            master=validated_data.get("master", instance.master),
            start_time=validated_data["start_time"],
        )


class BookingStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Booking.Status.choices)

    def update(self, instance, validated_data):
        return update_booking_status(
            booking=instance,
            business=self.context["business"],
            status=validated_data["status"],
        )


class BookingSerializer(BookingReadSerializer):
    """Compatibility alias until API views move to BookingReadSerializer."""


class OutboundMessageSerializer(serializers.ModelSerializer):
    business_name = serializers.CharField(source="business.name", read_only=True)

    class Meta:
        model = OutboundMessage
        fields = (
            "id",
            "business",
            "business_name",
            "channel",
            "recipient",
            "message_type",
            "status",
            "provider_message_id",
            "error_code",
            "attempts",
            "submitted_at",
            "delivered_at",
            "dead_lettered_at",
            "created_at",
        )

