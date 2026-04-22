from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.models import BusinessMembership
from apps.bookings.models import Booking, Business, OutboundMessage


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


class BusinessMembershipSerializer(serializers.ModelSerializer):
    business = BusinessSerializer()

    class Meta:
        model = BusinessMembership
        fields = ("id", "role", "is_active", "business")


class BookingSerializer(serializers.ModelSerializer):
    business_name = serializers.CharField(source="business.name", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    master_name = serializers.CharField(source="master.full_name", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = Booking
        fields = (
            "id",
            "business",
            "business_name",
            "client",
            "client_name",
            "master",
            "master_name",
            "service",
            "service_name",
            "start_time",
            "end_time",
            "status",
            "created_at",
        )


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

