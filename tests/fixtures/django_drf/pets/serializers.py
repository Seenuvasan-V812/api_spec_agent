from rest_framework import serializers

from pets.models import Pet


class TagSerializer(serializers.Serializer):
    """A label attached to a pet."""

    name = serializers.CharField(max_length=50, help_text="Tag text.")
    color = serializers.ChoiceField(choices=["red", "green", "blue"], required=False)


class PetSerializer(serializers.ModelSerializer):
    """Serialized representation of a pet."""

    tags = TagSerializer(many=True, required=False)
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = Pet
        fields = ["id", "name", "age", "status", "created_at", "tags", "display_name"]
        read_only_fields = ["created_at"]

    def get_display_name(self, obj) -> str:
        return f"{obj.name} ({obj.status})"
