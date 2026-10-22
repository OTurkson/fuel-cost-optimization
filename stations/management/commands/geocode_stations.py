from __future__ import annotations

import time
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from stations.geocoding import (
    MapboxGeocoder,
    TomTomGeocoder,
    mapbox_station_query,
    station_query,
)
from stations.models import FuelStation


class Command(BaseCommand):
    help = "Geocode stations that do not yet have successful coordinates."

    def add_arguments(self, parser) -> None:
        """Expose safe controls for sampling, resuming, and rate limiting."""
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--delay", type=float, default=1.0)
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument(
            "--provider",
            choices=("mapbox", "tomtom"),
            default="tomtom",
        )

    def handle(self, *args, **options) -> None:
        """Process a bounded batch and commit each result for safe resumption."""
        if options["provider"] == "mapbox":
            token = getattr(settings, "MAPBOX_ACCESS_TOKEN", "")
            if not token:
                raise CommandError("MAPBOX_ACCESS_TOKEN is not configured")
            geocoder = MapboxGeocoder(token)
        else:
            token = getattr(settings, "TOMTOM_API_KEY", "")
            if not token:
                raise CommandError("TOMTOM_API_KEY is not configured")
            geocoder = TomTomGeocoder(token)
        if options["limit"] < 1:
            raise CommandError("--limit must be positive")
        if options["delay"] < 0:
            raise CommandError("--delay cannot be negative")

        station_filter = Q(geocoding_status="required")
        if options["retry_failed"]:
            station_filter |= Q(
                geocoding_provider=options["provider"],
                geocoding_status__in=["failed", "ambiguous"],
            )
        stations = FuelStation.objects.filter(station_filter, address_conflict=False).order_by(
            "opis_truckstop_id"
        )[: options["limit"]]
        processed = 0

        for station in stations:
            if options["provider"] == "mapbox":
                query = mapbox_station_query(
                    station.address, station.city, station.state
                )
            else:
                query = station_query(
                    station.truckstop_name,
                    station.address,
                    station.city,
                    station.state,
                )
            try:
                if options["provider"] == "tomtom":
                    result = geocoder.geocode(
                        query, expected_city=station.city, expected_state=station.state
                    )
                else:
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
                if settings.USE_POSTGIS:
                    from django.contrib.gis.geos import Point

                    station.location = Point(result.longitude, result.latitude, srid=4326)
                else:
                    station.location = {
                        "type": "Point",
                        "coordinates": [result.longitude, result.latitude],
                    }
                station.geocoding_error = ""
            else:
                station.location = None
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
