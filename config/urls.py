from django.contrib import admin
from django.conf import settings
from django.urls import include, path


urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("api/v1/", include("apps.api.urls")),
    path("api/v1/", include("apps.bookings.urls")),
]
