from datetime import time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, Business, Category, Client, Master, Service


WEEK_SCHEDULE = {
    "mon": {"start": "10:00", "end": "20:00"},
    "tue": {"start": "10:00", "end": "20:00"},
    "wed": {"start": "10:00", "end": "20:00"},
    "thu": {"start": "10:00", "end": "20:00"},
    "fri": {"start": "10:00", "end": "20:00"},
    "sat": {"start": "10:00", "end": "18:00"},
    "sun": {"start": "11:00", "end": "17:00"},
}


class Command(BaseCommand):
    help = "Create or refresh a realistic demo salon with masters, services, clients, and sample bookings."

    def handle(self, *args, **options):
        with transaction.atomic():
            business, business_created = Business.objects.update_or_create(
                slug="demo-salon",
                defaults={
                    "name": "Aura Beauty Lab",
                    "brand_name": "Aura Beauty Lab",
                    "city": "Almaty",
                    "address": "Abylai Khan 68, 2nd floor",
                    "working_hours": "Mon-Fri 10:00-20:00, Sat 10:00-18:00, Sun 11:00-17:00",
                    "timezone_name": "Asia/Almaty",
                    "is_active": True,
                    "knowledge_base": (
                        "Aura Beauty Lab is a modern beauty salon in Almaty. "
                        "We offer hair, brow, lash, and nail services. "
                        "Clients usually book in Russian, but answers can also be given in Kazakh. "
                        "We ask clients to arrive 5-10 minutes early and confirm the service, date, "
                        "and preferred master before finalizing a booking."
                    ),
                    "ai_settings": {
                        "temperature": 0.2,
                        "tone": "warm_professional",
                        "language": "ru",
                    },
                    "ai_rules": {
                        "rules": [
                            "Always confirm service, date, and time before creating a booking.",
                            "If a preferred slot is unavailable, offer the nearest alternatives.",
                            "Keep replies concise and salon-friendly.",
                        ]
                    },
                },
            )

            categories = {
                "Hair": "Haircuts, styling, and coloring.",
                "Brows & Lashes": "Brows, lashes, and express face beauty services.",
                "Nails": "Manicure and nail care.",
            }
            category_map = {}
            for name, description in categories.items():
                category, _ = Category.objects.update_or_create(
                    business=business,
                    name=name,
                    defaults={
                        "description": description,
                        "is_active": True,
                    },
                )
                category_map[name] = category

            masters = [
                {
                    "full_name": "Aruzhan Saparova",
                    "specialization": "Hair stylist",
                },
                {
                    "full_name": "Madina Ospanova",
                    "specialization": "Brow & lash artist",
                },
                {
                    "full_name": "Dana Kairatkyzy",
                    "specialization": "Nail artist",
                },
            ]
            master_map = {}
            for payload in masters:
                master, _ = Master.objects.update_or_create(
                    business=business,
                    full_name=payload["full_name"],
                    defaults={
                        "specialization": payload["specialization"],
                        "working_hours": WEEK_SCHEDULE,
                        "is_active": True,
                    },
                )
                master_map[payload["full_name"]] = master

            services = [
                ("Hair", "Women's Haircut", Decimal("12000.00"), 90, 15),
                ("Hair", "Men's Haircut", Decimal("8000.00"), 60, 15),
                ("Hair", "Hair Coloring", Decimal("25000.00"), 180, 30),
                ("Brows & Lashes", "Brow Shape + Tint", Decimal("7000.00"), 45, 10),
                ("Brows & Lashes", "Lash Lift", Decimal("11000.00"), 75, 15),
                ("Brows & Lashes", "Express Makeup", Decimal("15000.00"), 60, 15),
                ("Nails", "Manicure + Gel Polish", Decimal("10000.00"), 90, 15),
                ("Nails", "Pedicure", Decimal("14000.00"), 90, 15),
            ]
            for category_name, service_name, price, minutes, buffer_minutes in services:
                Service.objects.update_or_create(
                    business=business,
                    name=service_name,
                    defaults={
                        "category": category_map[category_name],
                        "price": price,
                        "duration": timedelta(minutes=minutes),
                        "buffer_time": timedelta(minutes=buffer_minutes),
                        "is_active": True,
                    },
                )

            service_map = {
                service.name: service
                for service in Service.objects.filter(business=business, is_active=True)
            }
            business.ai_rules = {
                "rules": [
                    "Always confirm service, date, and time before creating a booking.",
                    "If a preferred slot is unavailable, offer the nearest alternatives.",
                    "Keep replies concise and salon-friendly.",
                ],
                "allowed_master_service_pairs": [
                    {
                        "master_id": master_map["Aruzhan Saparova"].id,
                        "service_id": service_map["Women's Haircut"].id,
                    },
                    {
                        "master_id": master_map["Aruzhan Saparova"].id,
                        "service_id": service_map["Men's Haircut"].id,
                    },
                    {
                        "master_id": master_map["Aruzhan Saparova"].id,
                        "service_id": service_map["Hair Coloring"].id,
                    },
                    {
                        "master_id": master_map["Madina Ospanova"].id,
                        "service_id": service_map["Brow Shape + Tint"].id,
                    },
                    {
                        "master_id": master_map["Madina Ospanova"].id,
                        "service_id": service_map["Lash Lift"].id,
                    },
                    {
                        "master_id": master_map["Madina Ospanova"].id,
                        "service_id": service_map["Express Makeup"].id,
                    },
                    {
                        "master_id": master_map["Dana Kairatkyzy"].id,
                        "service_id": service_map["Manicure + Gel Polish"].id,
                    },
                    {
                        "master_id": master_map["Dana Kairatkyzy"].id,
                        "service_id": service_map["Pedicure"].id,
                    },
                ],
            }
            business.save(update_fields=["ai_rules", "updated_at"])

            clients = [
                {
                    "phone": "+77070000021",
                    "name": "Aliya Test",
                    "whatsapp_id": "77070000021@c.us",
                },
                {
                    "phone": "+77070000022",
                    "name": "Diana Demo",
                    "whatsapp_id": "77070000022@c.us",
                },
            ]
            client_map = {}
            for payload in clients:
                client, _ = Client.objects.update_or_create(
                    business=business,
                    phone=payload["phone"],
                    defaults={
                        "name": payload["name"],
                        "whatsapp_id": payload["whatsapp_id"],
                        "is_active": True,
                    },
                )
                client_map[payload["phone"]] = client

            tomorrow = timezone.localdate() + timedelta(days=1)
            day_after = timezone.localdate() + timedelta(days=2)
            manicure = Service.objects.get(business=business, name="Manicure + Gel Polish")
            haircut = Service.objects.get(business=business, name="Women's Haircut")

            sample_bookings = [
                {
                    "client": client_map["+77070000021"],
                    "master": master_map["Dana Kairatkyzy"],
                    "service": manicure,
                    "start_time": Booking.make_aware_datetime(tomorrow, time(12, 0)),
                    "status": Booking.Status.CONFIRMED,
                    "notes": "Demo confirmed nail booking.",
                },
                {
                    "client": client_map["+77070000022"],
                    "master": master_map["Aruzhan Saparova"],
                    "service": haircut,
                    "start_time": Booking.make_aware_datetime(day_after, time(15, 0)),
                    "status": Booking.Status.PENDING,
                    "notes": "Demo pending haircut booking.",
                },
            ]

            created_bookings = 0
            for payload in sample_bookings:
                booking, created = Booking.objects.get_or_create(
                    business=business,
                    client=payload["client"],
                    master=payload["master"],
                    service=payload["service"],
                    start_time=payload["start_time"],
                    defaults={
                        "client_data": {"source": "seed_demo_salon"},
                        "status": payload["status"],
                        "notes": payload["notes"],
                    },
                )
                if created:
                    created_bookings += 1
                else:
                    booking.status = payload["status"]
                    booking.notes = payload["notes"]
                    booking.client_data = {"source": "seed_demo_salon"}
                    booking.save(
                        update_fields=["status", "notes", "client_data", "updated_at"]
                    )

        action = "Created" if business_created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} demo salon '{business.display_brand_name}' "
                f"(slug={business.slug}, id={business.id})."
            )
        )
        self.stdout.write("Masters: 3")
        self.stdout.write("Categories: 3")
        self.stdout.write("Services: 8")
        self.stdout.write("Clients: 2")
        self.stdout.write(f"Sample bookings refreshed (newly created: {created_bookings}).")
