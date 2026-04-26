from django.contrib import admin
from django.conf import settings
from django.urls import include, path
from rest_framework.permissions import AllowAny
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


class PublicSchemaView(SpectacularAPIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []


class PublicSwaggerUIView(SpectacularSwaggerView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []


urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("api/v1/schema/", PublicSchemaView.as_view(), name="api-schema"),
    path(
        "api/v1/schema/swagger-ui/",
        PublicSwaggerUIView.as_view(url_name="api-schema"),
        name="api-schema-swagger-ui",
    ),
    path("api/v1/", include("apps.api.urls")),
    path("api/v1/", include("apps.bookings.urls")),
]
