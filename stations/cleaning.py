"""Deterministically clean the supplied fuel-price workbook.

The source workbook is never modified. One retained row is emitted per valid
OPIS Truckstop ID, using the minimum valid Retail Price for that station.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import openpyxl


EXPECTED_HEADERS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]
STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
MAX_REASONABLE_PRICE = 10.0


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_state(value: Any) -> str:
    return text(value).upper()


def normalize_station_id(value: Any) -> str | None:
    value_text = text(value)
    if not value_text:
        return None
    try:
        number = float(value_text)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return None
    return str(int(number))


def parse_price(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(str(value).strip().replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0 or price > MAX_REASONABLE_PRICE:
        return None
    return price


def clean(source: Path, output: Path, report_path: Path) -> dict[str, Any]:
    # Read the workbook without changing it, and release the file when finished.
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        return _clean_workbook(workbook, source, output, report_path)
    finally:
        workbook.close()


def _clean_workbook(workbook, source: Path, output: Path, report_path: Path) -> dict[str, Any]:
    if len(workbook.worksheets) != 1:
        raise ValueError("Expected exactly one worksheet")
    worksheet = workbook.worksheets[0]
    rows = worksheet.iter_rows(values_only=True)
    headers = [text(value) for value in next(rows)]
    if headers != EXPECTED_HEADERS:
        raise ValueError(f"Unexpected headers: {headers!r}")

    source_rows = 0
    invalid_identifiers = 0
    invalid_prices = 0
    stations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_reasons: dict[str, int] = defaultdict(int)

    # Keep accepted rows grouped by station ID, regardless of Rack ID.
    for row_number, row in enumerate(rows, start=2):
        source_rows += 1
        values = dict(zip(EXPECTED_HEADERS, row))
        station_id = normalize_station_id(values["OPIS Truckstop ID"])
        if station_id is None:
            invalid_identifiers += 1
            invalid_reasons["invalid_station_id"] += 1
            continue

        price = parse_price(values["Retail Price"])
        if price is None:
            invalid_prices += 1
            invalid_reasons["invalid_or_missing_price"] += 1
            continue

        stations[station_id].append(
            {
                "source_row": row_number,
                "station_id": station_id,
                "name": text(values["Truckstop Name"]),
                "address": text(values["Address"]),
                "city": text(values["City"]),
                "state": normalize_state(values["State"]),
                "rack_id": text(values["Rack ID"]),
                "source_price": text(values["Retail Price"]),
                "price": price,
            }
        )

    retained: list[dict[str, Any]] = []
    conflicting_addresses = 0
    excluded_no_valid_price = 0
    duplicate_source_rows = 0
    state_or_address_flags = 0

    all_station_ids = {
        normalize_station_id(row[0])
        for row in worksheet.iter_rows(min_row=2, values_only=True)
        if normalize_station_id(row[0]) is not None
    }
    for station_id in sorted(all_station_ids, key=int):
        records = stations.get(station_id, [])
        if not records:
            excluded_no_valid_price += 1
            continue
        duplicate_source_rows += max(0, len(records) - 1)
        # Flag conflicting locations rather than silently merging them.
        addresses = {
            (record["address"], record["city"], record["state"]) for record in records
        }
        names = {record["name"] for record in records}
        if len(addresses) > 1:
            conflicting_addresses += 1
        if any(
            not record["address"]
            or not record["city"]
            or not STATE_PATTERN.fullmatch(record["state"])
            for record in records
        ):
            state_or_address_flags += 1

        # Use the lowest supplied price; source row breaks ties consistently.
        selected = min(records, key=lambda record: (record["price"], record["source_row"]))
        retained.append(
            {
                "opis_truckstop_id": station_id,
                "truckstop_name": selected["name"],
                "address": selected["address"],
                "city": selected["city"],
                "state": selected["state"],
                "selected_price_usd_per_gallon": selected["price"],
                "selected_source_row": selected["source_row"],
                "selected_source_price": selected["source_price"],
                "source_row_count": len(records),
                "distinct_price_count": len({record["price"] for record in records}),
                "distinct_name_count": len(names),
                "distinct_address_count": len(addresses),
                "address_conflict": len(addresses) > 1,
                "geocoding_status": "required",
            }
        )

    # Write an intermediate CSV so the cleaned records can be reviewed first.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(retained[0].keys()) if retained else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(retained)

    # Record the rules and counts needed to explain how this output was produced.
    report = {
        "source_file": str(source),
        "source_format": "XLSX",
        "source_encoding": "XLSX container; text values read as Unicode",
        "source_worksheet": worksheet.title,
        "source_headers": headers,
        "retail_price_unit": {
            "value": "USD per US gallon (agreed demo assumption; no conversion)",
            "verified": False,
            "assumed_for_demo": True,
            "required_unit": "USD per US gallon",
            "evidence": "Project owner authorized treating supplied numeric prices as USD per US gallon for this demo; not independently verified or claimed current.",
        },
        "cleaning_rules": {
            "station_identity": "Positive integer OPIS Truckstop ID, normalized to a decimal string.",
            "price": f"Finite numeric values greater than 0 and no greater than {MAX_REASONABLE_PRICE}; invalid values are excluded and counted.",
            "selection": "Minimum valid price per OPIS Truckstop ID; ties use the earliest source row.",
            "address_conflict": "Different (address, city, state) values for one station ID are flagged, not merged or silently corrected.",
            "state": "Trimmed and uppercased; non-two-letter values are flagged.",
        },
        "counts": {
            "original_rows": source_rows,
            "distinct_station_ids": len(all_station_ids),
            "duplicate_or_repeated_source_rows": duplicate_source_rows,
            "records_with_invalid_identifiers": invalid_identifiers,
            "invalid_or_missing_prices": invalid_prices,
            "stations_with_conflicting_addresses": conflicting_addresses,
            "stations_with_incomplete_addresses_or_states": state_or_address_flags,
            "stations_retained_after_cleaning": len(retained),
            "stations_excluded_no_valid_price": excluded_no_valid_price,
            "stations_requiring_geocoding": len(retained),
            "stations_successfully_geocoded": 0,
            "stations_failed_or_ambiguous_geocoding": 0,
        },
        "reconciliation": {
            "source_rows": source_rows,
            "valid_price_rows_plus_invalid_price_rows_plus_invalid_id_rows": (
                sum(len(records) for records in stations.values())
                + invalid_prices
                + invalid_identifiers
            ),
            "distinct_ids": len(all_station_ids),
            "retained_plus_excluded_no_valid_price": len(retained) + excluded_no_valid_price,
            "one_cleaned_record_per_station_id": len(
                {record["opis_truckstop_id"] for record in retained}
            )
            == len(retained),
        },
        "invalid_reasons": dict(invalid_reasons),
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/cleaned_fuel_stations.csv"))
    parser.add_argument("--report", type=Path, default=Path("data/cleaning_report.json"))
    args = parser.parse_args()
    report = clean(args.source, args.output, args.report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
