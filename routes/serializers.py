"""DRF serializers for the route optimization API."""

from decimal import Decimal, InvalidOperation

from rest_framework import serializers


class OptimizeRouteRequestSerializer(serializers.Serializer):
    """Validates the POST /api/v1/routes/optimize/ request body."""

    start = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=512,
        help_text="Origin location string (e.g. 'New York, NY')",
    )
    finish = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
        max_length=512,
        help_text="Destination location string (e.g. 'Chicago, IL')",
    )
    initial_fuel_gallons = serializers.DecimalField(
        required=False,
        max_digits=5,
        decimal_places=2,
        min_value=Decimal(0),
        max_value=Decimal(50),
        default=Decimal(50),
        help_text="Starting fuel in gallons (0-50, default 50)",
    )

    def validate_initial_fuel_gallons(self, value):
        """Ensure the value is a valid decimal within range."""
        if value is None:
            return Decimal(50)
        try:
            value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise serializers.ValidationError("Must be a valid number.")
        if value < 0 or value > 50:
            raise serializers.ValidationError("Must be between 0 and 50.")
        return value
