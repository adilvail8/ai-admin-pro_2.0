from django.db import migrations
import phonenumber_field.modelfields


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0011_bookingsession"),
    ]

    operations = [
        migrations.AlterField(
            model_name="client",
            name="phone",
            field=phonenumber_field.modelfields.PhoneNumberField(
                blank=True,
                max_length=128,
                null=True,
                region="KZ",
            ),
        ),
    ]
