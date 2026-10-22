from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.models import FuelStation


REQUIRED_COLUMNS = {
    "opis_truckstop_id",
    "truckstop_name",
    "address",
    "city",
    "state",
    "selected_price_usd_per_gallon",
    "selected_source_row",
    "selected_source_price",
    "source_row_count",
    "distinct_price_count",
    "distinct_name_count",
    "distinct_address_count",
    "address_conflict",
    "geocoding_status",
}


def parse_bool(value: str, *, row_number: int) -> bool:
    """Convert the cleaner's explicit boolean text without guessing."""
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise CommandError(f"Row {row_number}: invalid boolean {value!r}")
    return normalized == "true"


def parse_positive_int(value: str, field: str, *, row_number: int) -> int:
    """Validate positive source counters before they reach the database."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CommandError(f"Row {row_number}: invalid {field} {value!r}") from exc
    if parsed < 1:
        raise CommandError(f"Row {row_number}: {field} must be positive")
    return parsed


class Command(BaseCommand):
    help = "Import one station record per row from the cleaned station CSV."

    def add_arguments(self, parser) -> None:
        """Allow a custom cleaned CSV while keeping the project default handy."""
        parser.add_argument(
            "csv_path",
            nargs="?",
            type=Path,
            default=Path("data/cleaned_fuel_stations.csv"),
        )

    # A rejected row rolls back the entire import.
    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Upsert cleaned rows so interrupted imports can safely be rerun."""
        csv_path: Path = options["csv_path"]
        if not csv_path.is_file():
            raise CommandError(f"Cleaned station CSV does not exist: {csv_path}")

        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or [])
            missing = REQUIRED_COLUMNS - columns
            if missing:
                missing_text = ", ".join(sorted(missing))
                raise CommandError(f"CSV is missing required columns: {missing_text}")

            imported = 0
            seen_ids: set[str] = set()
            # Validate cleaned input before creating or updating station records.
            for row_number, row in enumerate(reader, start=2):
                if any(row.get(column) is None for column in REQUIRED_COLUMNS):
                    raise CommandError(f"Row {row_number}: incomplete CSV row")
                station_id = row["opis_truckstop_id"].strip()
                if not station_id:
                    raise CommandError(f"Row {row_number}: station ID is required")
                if station_id in seen_ids:
                    raise CommandError(f"Row {row_number}: duplicate station ID {station_id}")
                seen_ids.add(station_id)
                if row["geocoding_status"].strip() not in {"", "required"}:
                    raise CommandError(
                        f"Row {row_number}: cleaned input must require geocoding; "
                        "geocoding results are managed separately"
                    )
                try:
                    price = Decimal(row["selected_price_usd_per_gallon"])
                except InvalidOperation as exc:
                    raise CommandError(
                        f"Row {row_number}: invalid selected price"
                    ) from exc
                if not price.is_finite() or price <= 0:
                    raise CommandError(f"Row {row_number}: selected price must be positive")

                defaults = {
                    "truckstop_name": row["truckstop_name"].strip(),
                    "address": row["address"].strip(),
                    "city": row["city"].strip(),
                    "state": row["state"].strip().upper(),
                    "selected_price_usd_per_gallon": price,
                    "selected_source_row": parse_positive_int(
                        row["selected_source_row"],
                        "selected_source_row",
                        row_number=row_number,
                    ),
                    "selected_source_price": row["selected_source_price"].strip(),
                    "source_row_count": parse_positive_int(
                        row["source_row_count"],
                        "source_row_count",
                        row_number=row_number,
                    ),
                    "distinct_price_count": parse_positive_int(
                        row["distinct_price_count"],
                        "distinct_price_count",
                        row_number=row_number,
                    ),
                    "distinct_name_count": parse_positive_int(
                        row["distinct_name_count"],
                        "distinct_name_count",
                        row_number=row_number,
                    ),
                    "distinct_address_count": parse_positive_int(
                        row["distinct_address_count"],
                        "distinct_address_count",
                        row_number=row_number,
                    ),
                    "address_conflict": parse_bool(
                        row["address_conflict"], row_number=row_number
                    ),
                }
                if defaults["address_conflict"] or defaults["distinct_address_count"] > 1:
                    raise CommandError(
                        f"Row {row_number}: station {station_id} has conflicting "
                        "addresses; review before importing"
                    )
                station, created = FuelStation.objects.get_or_create(
                    opis_truckstop_id=station_id, defaults=defaults
                )
                if not created:
                    # A changed query could invalidate coordinates or race with
                    # an in-flight lookup. Require separate review instead.
                    identity_fields = ("truckstop_name", "address", "city", "state")
                    if any(
                        getattr(station, field) != defaults[field]
                        for field in identity_fields
                    ):
                        raise CommandError(
                            f"Row {row_number}: station {station_id} identity/address "
                            "changed; review separately before importing"
                        )
                    # Never write a stale copy of geocoding progress read above.
                    FuelStation.objects.filter(pk=station_id).update(**defaults)
                imported += 1

        self.stdout.write(self.style.SUCCESS(f"Imported {imported} fuel stations."))
