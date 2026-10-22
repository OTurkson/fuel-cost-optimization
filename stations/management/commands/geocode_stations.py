from __future__ import annotations

import time
from decimal import Decimal

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from stations.geocoding import MapboxGeocoder, station_query
from stations.models import FuelStation


class Command(BaseCommand):
    help = "Geocode stations that do not yet have successful coordinates."

    def add_arguments(self, parser) -> None:
        """Expose safe controls for sampling, resuming, and rate limiting."""
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--delay", type=float, default=1.0)
        parser.add_argument("--retry-failed", action="store_true")

    def handle(self, *args, **options) -> None:
        """Process a bounded batch and commit each result for safe resumption."""
        token = getattr(settings, "MAPBOX_ACCESS_TOKEN", "")
        if not token:
            raise CommandError("MAPBOX_ACCESS_TOKEN is not configured")
        if options["limit"] < 1:
            raise CommandError("--limit must be positive")
        if options["delay"] < 0:
            raise CommandError("--delay cannot be negative")

        statuses = ["required"]
        if options["retry_failed"]:
            statuses.extend(["failed", "ambiguous"])
        stations = FuelStation.objects.filter(
            geocoding_status__in=statuses
        ).order_by("opis_truckstop_id")[: options["limit"]]
        geocoder = MapboxGeocoder(token)
        processed = 0

        for station in stations:
            query = station_query(
                station.truckstop_name,
                station.address,
                station.city,
                station.state,
            )
            try:
                result = geocoder.geocode(query)
            except RuntimeError as exc:
                raise CommandError(str(exc)) from exc

            station.geocoding_provider = geocoder.provider_name
            station.geocoding_query = query
            station.geocoded_at = timezone.now()
            station.geocoding_status = result.status
            station.geocoding_error = result.error
            station.geocoding_relevance = (
                Decimal(str(result.relevance)) if result.relevance is not None else None
            )
            if result.status == "successful":
                station.location = Point(result.longitude, result.latitude, srid=4326)
                station.geocoding_error = ""
            station.save(
                update_fields=[
                    "location",
                    "geocoding_status",
                    "geocoding_error",
                    "geocoding_provider",
                    "geocoding_query",
                    "geocoding_relevance",
                    "geocoded_at",
                ]
            )
            processed += 1
            self.stdout.write(
                f"{station.opis_truckstop_id}: {result.status}"
                + (f" ({result.error})" if result.error else "")
            )
            if options["delay"]:
                time.sleep(options["delay"])

        self.stdout.write(self.style.SUCCESS(f"Processed {processed} stations."))
