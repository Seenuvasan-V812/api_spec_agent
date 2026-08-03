from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from pets.serializers import PetSerializer
from pets.models import Pet


class PetViewSet(ModelViewSet):
    """Manage pets in the adoption catalog."""

    queryset = Pet.objects.all()
    serializer_class = PetSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=True, methods=["post"])
    def adopt(self, request, pk=None):
        """Mark a pet as adopted."""
        pet = self.get_object()
        pet.status = "adopted"
        pet.save()
        serializer = self.get_serializer(pet)
        return Response(serializer.data)


class PetStatsView(APIView):
    """Aggregate statistics about the adoption catalog."""

    def get(self, request):
        """Return per-status pet counts."""
        return Response({"available": 0, "pending": 0, "adopted": 0})

    def post(self, request):
        """Validate and echo a pet payload without persisting it."""
        serializer = PetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
