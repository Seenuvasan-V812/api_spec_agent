from django.urls import include, path
from rest_framework.routers import DefaultRouter

from pets.views import PetStatsView, PetViewSet

router = DefaultRouter()
router.register(r"pets", PetViewSet, basename="pet")

urlpatterns = [
    path("stats/", PetStatsView.as_view()),
    path("", include(router.urls)),
]
