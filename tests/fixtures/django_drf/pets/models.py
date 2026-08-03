from django.db import models


class Pet(models.Model):
    """A pet available for adoption."""

    STATUS_CHOICES = [
        ("available", "Available"),
        ("pending", "Pending"),
        ("adopted", "Adopted"),
    ]

    name = models.CharField(max_length=100, help_text="Display name of the pet.")
    age = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="available")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
