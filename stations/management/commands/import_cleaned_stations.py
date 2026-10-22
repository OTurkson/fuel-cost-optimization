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
            for row_number, row in enumerate(reader, start=2):
                station_id = row["opis_truckstop_id"].strip()
                if not station_id:
                    raise CommandError(f"Row {row_number}: station ID is required")
                try:
                    price = Decimal(row["selected_price_usd_per_gallon"])
                except InvalidOperation as exc:
                    raise CommandError(
                        f"Row {row_number}: invalid selected price"
                    ) from exc
                if price <= 0:
                    raise CommandError(f"Row {row_number}: selected price must be positive")

                FuelStation.objects.update_or_create(
                    opis_truckstop_id=station_id,
                    defaults={
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
                        "geocoding_status": row["geocoding_status"].strip() or "required",
                    },
                )
                imported += 1

        self.stdout.write(self.style.SUCCESS(f"Imported {imported} fuel stations."))
