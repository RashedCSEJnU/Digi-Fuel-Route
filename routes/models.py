from django.db import models


class FuelStation(models.Model):
    """A truck-stop fuel station with a retail price and optional coordinates."""

    class GeocodeStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        RESOLVED = "resolved", "Resolved"
        UNRESOLVED = "unresolved", "Unresolved"
        FAILED = "failed", "Failed"

    opis_truckstop_id = models.CharField(max_length=64, blank=True, default="")
    rack_id = models.CharField(max_length=64, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=512, blank=True, default="")
    city = models.CharField(max_length=128, blank=True, default="")
    state = models.CharField(max_length=8, blank=True, default="")
    postal_code = models.CharField(max_length=16, blank=True, default="")
    retail_price = models.DecimalField(
        max_digits=10, decimal_places=6, null=True, blank=True
    )
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True
    )
    normalized_identity = models.CharField(max_length=512, unique=True, db_index=True)
    geocode_status = models.CharField(
        max_length=16,
        choices=GeocodeStatus.choices,
        default=GeocodeStatus.PENDING,
        db_index=True,
    )
    geocode_failure_reason = models.CharField(max_length=255, blank=True, default="")
    source_row_number = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [  # noqa: RUF012
            models.Index(fields=["state"]),
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self):
        return f"{self.name or self.normalized_identity} ({self.state})"


class GeocodeCache(models.Model):
    """Persistent cache for geocoding queries."""

    class Status(models.TextChoices):
        RESOLVED = "resolved", "Resolved"
        UNRESOLVED = "unresolved", "Unresolved"
        FAILED = "failed", "Failed"

    normalized_query = models.CharField(max_length=512, unique=True, db_index=True)
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True
    )
    display_name = models.CharField(max_length=512, blank=True, default="")
    country_code = models.CharField(max_length=8, blank=True, default="")
    provider_response_subset = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RESOLVED
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.normalized_query


class RouteCache(models.Model):
    """Persistent cache for OSRM route responses."""

    cache_key = models.CharField(max_length=512, unique=True, db_index=True)
    origin_latitude = models.DecimalField(max_digits=10, decimal_places=7)
    origin_longitude = models.DecimalField(max_digits=10, decimal_places=7)
    destination_latitude = models.DecimalField(max_digits=10, decimal_places=7)
    destination_longitude = models.DecimalField(max_digits=10, decimal_places=7)
    profile = models.CharField(max_length=32, default="driving")
    provider_name = models.CharField(max_length=32, default="OSRM")
    distance_miles = models.FloatField()
    duration_seconds = models.FloatField()
    geometry = models.JSONField()
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.cache_key
