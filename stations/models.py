from __future__ import annotations

from django.conf import settings
from django.db import models

if settings.USE_POSTGIS:
    from django.contrib.gis.db.models import PointField
else:
    PointField = None


class FuelStation(models.Model):
    """Store one cleaned physical station and its geocoding progress.

    The station ID is the business key from the source file. Coordinates are
    nullable because geocoding happens after cleaning and may need retrying.
    """

    opis_truckstop_id = models.CharField(max_length=32, primary_key=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    selected_price_usd_per_gallon = models.DecimalField(
        max_digits=10, decimal_places=6
    )
    selected_source_row = models.PositiveIntegerField()
    selected_source_price = models.CharField(max_length=64)
    source_row_count = models.PositiveIntegerField()
    distinct_price_count = models.PositiveIntegerField()
    distinct_name_count = models.PositiveIntegerField()
    distinct_address_count = models.PositiveIntegerField()
    address_conflict = models.BooleanField(default=False)
    if PointField is not None:
        location = PointField(
            geography=True, null=True, blank=True, spatial_index=True
        )
    else:
        # The local SQLite fallback lets checks run without native GDAL.
        location = models.JSONField(null=True, blank=True)
    geocoding_status = models.CharField(
        max_length=32,
        choices=[
            ("required", "Required"),
            ("successful", "Successful"),
            ("failed", "Failed"),
            ("ambiguous", "Ambiguous"),
        ],
        default="required",
    )
    geocoding_error = models.TextField(blank=True)
    geocoding_provider = models.CharField(max_length=32, blank=True)
    geocoding_query = models.TextField(blank=True)
    geocoding_relevance = models.DecimalField(
        max_digits=4, decimal_places=3, null=True, blank=True
    )
    geocoded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("opis_truckstop_id",)

    def __str__(self) -> str:
        """Make station records readable in the Django shell and admin."""
        return f"{self.truckstop_name} ({self.opis_truckstop_id})"
