"""Root URL configuration for the fixture project."""

from django.http import JsonResponse
from django.urls import include, path


def health(request):
    """Service liveness probe."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health),
    path("api/v1/", include("pets.urls")),
]
