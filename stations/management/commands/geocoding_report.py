from __future__ import annotations

import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from stations.models import FuelStation
from stations.reporting import geocoding_report


FIELDS = (
    "opis_truckstop_id", "truckstop_name", "address", "city", "state",
    "address_conflict", "selected_price_usd_per_gallon", "geocoding_status",
    "geocoding_provider", "geocoding_query", "geocoding_relevance",
    "geocoding_error", "geocoded_at", "location",
)


class Command(BaseCommand):
    help = "Read geocoding progress and optionally export a review CSV; no station writes."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout.")
        parser.add_argument("--review-csv", type=Path, help="Export failed/ambiguous records.")
        parser.add_argument(
            "--include-successful", action="store_true",
            help="Also export successful matches for manual spot checks.",
        )

    def handle(self, *args, **options) -> None:
        output = options["output"]
        review_csv = options["review_csv"]
        if options["include_successful"] and not review_csv:
            raise CommandError("--include-successful requires --review-csv")
        if output and review_csv and output.resolve() == review_csv.resolve():
            raise CommandError("JSON and CSV outputs must use different paths")
        # One SELECT gives both exports the same snapshot even during geocoding.
        rows = list(FuelStation.objects.order_by("opis_truckstop_id").values(*FIELDS))
        report = geocoding_report(rows)
        report["generated_at"] = timezone.now().isoformat()
        payload = json.dumps(report, indent=2) + "\n"
        try:
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(payload, encoding="utf-8")
            else:
                self.stdout.write(payload, ending="")
            if review_csv:
                review_csv.parent.mkdir(parents=True, exist_ok=True)
                with review_csv.open("w", newline="", encoding="utf-8") as handle:
                    fields = [field for field in FIELDS if field != "location"]
                    writer = csv.DictWriter(handle, fieldnames=[*fields, "longitude", "latitude"])
                    writer.writeheader()
                    # Export records needing review, plus successes when requested.
                    for row in rows:
                        status = row["geocoding_status"]
                        inconsistent = (status == "successful") != (row["location"] is not None)
                        if not (
                            status in {"failed", "ambiguous"}
                            or row["address_conflict"] or inconsistent
                            or (options["include_successful"] and status == "successful")
                        ):
                            continue
                        record = {field: row[field] for field in fields}
                        location = row["location"]
                        # Accept the PostGIS point and the SQLite test representation.
                        if location is not None:
                            coordinates = (
                                location["coordinates"] if isinstance(location, dict)
                                else (location.x, location.y)
                            )
                            record.update(longitude=coordinates[0], latitude=coordinates[1])
                        writer.writerow(record)
        except OSError as exc:
            raise CommandError(f"Could not write geocoding report: {exc}") from exc
