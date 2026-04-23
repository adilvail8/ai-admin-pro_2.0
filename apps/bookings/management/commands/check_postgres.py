from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = "Check that Django can connect to the configured PostgreSQL database."

    def handle(self, *args, **options):
        connection = connections["default"]

        if connection.vendor != "postgresql":
            raise CommandError(
                f"Default database vendor is '{connection.vendor}', not PostgreSQL."
            )

        try:
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), current_user, version()")
                database_name, database_user, version = cursor.fetchone()
        except Exception as error:
            raise CommandError(
                f"PostgreSQL connection failed: {error}"
            ) from error

        self.stdout.write(
            self.style.SUCCESS(
                "PostgreSQL connection is healthy: "
                f"db={database_name}, user={database_user}"
            )
        )
        self.stdout.write(version)
