import csv
import json
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from stations.geocoding import GeocodingResult
from stations.management.commands.import_cleaned_stations import REQUIRED_COLUMNS
from stations.models import FuelStation


def cleaned_row(station_id="1", **overrides):
    row = {
        "opis_truckstop_id": station_id, "truckstop_name": "Example Truck Stop",
        "address": "123 Main St", "city": "Dallas", "state": "TX",
        "selected_price_usd_per_gallon": "3.25", "selected_source_row": "2",
        "selected_source_price": "3.25", "source_row_count": "2",
        "distinct_price_count": "2", "distinct_name_count": "1",
        "distinct_address_count": "1", "address_conflict": "false",
        "geocoding_status": "required",
    }
    row.update(overrides)
    return row


def station(station_id="1", **overrides):
    values = cleaned_row(station_id)
    values["address_conflict"] = False
    values.update(overrides)
    return FuelStation.objects.create(**values)


class ImportTests(TestCase):
    def import_rows(self, *rows):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cleaned.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=sorted(REQUIRED_COLUMNS))
                writer.writeheader()
                writer.writerows(rows)
            call_command("import_cleaned_stations", path, stdout=StringIO())

    def test_repeat_import_updates_price_and_preserves_all_geocoding_fields(self):
        point = {"type": "Point", "coordinates": [-96.8, 32.7]}
        for index, status in enumerate(("successful", "failed", "ambiguous", "required")):
            pk = str(index + 1)
            obj = station(
                pk, geocoding_status=status, geocoding_provider="tomtom",
                geocoding_query="original query", geocoding_error="original reason",
                geocoding_relevance=Decimal("8.1"), geocoded_at=timezone.now(),
                location=point if status == "successful" else None,
            )
            before = {key: getattr(obj, key) for key in (
                "location", "geocoding_status", "geocoding_provider", "geocoding_query",
                "geocoding_error", "geocoding_relevance", "geocoded_at",
            )}
            self.import_rows(cleaned_row(pk, selected_price_usd_per_gallon="2.99"))
            obj.refresh_from_db()
            self.assertEqual(obj.selected_price_usd_per_gallon, Decimal("2.99"))
            for key, value in before.items():
                self.assertEqual(getattr(obj, key), value, key)

    def test_new_station_requires_geocoding_with_no_coordinates(self):
        self.import_rows(cleaned_row())
        obj = FuelStation.objects.get()
        self.assertEqual(obj.geocoding_status, "required")
        self.assertIsNone(obj.location)

    def test_geocoding_update_after_import_read_is_preserved(self):
        station()
        original = FuelStation.objects.get_or_create

        def provider_finishes_after_read(*args, **kwargs):
            obj, created = original(*args, **kwargs)
            FuelStation.objects.filter(pk=obj.pk).update(
                geocoding_status="successful", geocoding_provider="tomtom",
                location={"type": "Point", "coordinates": [-96.8, 32.7]},
            )
            return obj, created

        with patch.object(FuelStation.objects, "get_or_create", side_effect=provider_finishes_after_read):
            self.import_rows(cleaned_row(selected_price_usd_per_gallon="2.99"))
        obj = FuelStation.objects.get()
        self.assertEqual(obj.geocoding_status, "successful")
        self.assertEqual(obj.location["coordinates"], [-96.8, 32.7])
        self.assertEqual(obj.selected_price_usd_per_gallon, Decimal("2.99"))

    def test_changed_address_rolls_back_entire_import(self):
        station("2")
        with self.assertRaisesMessage(CommandError, "identity/address changed"):
            self.import_rows(cleaned_row("1"), cleaned_row("2", address="999 New St"))
        self.assertFalse(FuelStation.objects.filter(pk="1").exists())
        self.assertEqual(FuelStation.objects.get(pk="2").address, "123 Main St")

    def test_duplicate_conflicting_and_fabricated_success_input_is_rejected(self):
        cases = (
            (cleaned_row(), cleaned_row()),
            (cleaned_row(address_conflict="true"),),
            (cleaned_row(distinct_address_count="2"),),
            (cleaned_row(geocoding_status="successful"),),
        )
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(CommandError):
                self.import_rows(*rows)
            self.assertEqual(FuelStation.objects.count(), 0)

    def test_nonfinite_and_nonpositive_prices_are_rejected(self):
        for price in ("NaN", "Infinity", "-Infinity", "0", "-1", "invalid"):
            with self.subTest(price=price), self.assertRaises(CommandError):
                self.import_rows(cleaned_row(selected_price_usd_per_gallon=price))


class ReportTests(TestCase):
    def test_single_read_snapshot_reports_counts_and_exports_review(self):
        station("1", geocoding_status="successful", geocoding_provider="tomtom",
                location={"type": "Point", "coordinates": [-96.8, 32.7]})
        station("2", geocoding_status="ambiguous", geocoding_provider="tomtom",
                geocoding_error="Review required")
        station("3", geocoding_status="failed", geocoding_provider="mapbox")
        station("4")
        before = list(FuelStation.objects.values())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            output = StringIO()
            with self.assertNumQueries(1):
                call_command("geocoding_report", review_csv=path, stdout=output)
            report = json.loads(output.getvalue())
            self.assertEqual(report["total_stations"], 4)
            self.assertEqual(report["by_status"], {
                "required": 1, "successful": 1, "ambiguous": 1, "failed": 1,
            })
            self.assertEqual(report["by_provider"]["tomtom"]["successful"], 1)
            self.assertEqual(report["by_state"]["TX"]["failed"], 1)
            self.assertTrue(report["checks"]["counts_reconcile"])
            with path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual([row["opis_truckstop_id"] for row in csv.DictReader(handle)], ["2", "3"])
            call_command("geocoding_report", review_csv=path, include_successful=True, stdout=StringIO())
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["opis_truckstop_id"] for row in rows], ["1", "2", "3"])
            self.assertEqual(float(rows[0]["longitude"]), -96.8)
        self.assertEqual(list(FuelStation.objects.values()), before)

    def test_inconsistent_records_are_reported_and_exported(self):
        station("1", geocoding_status="successful")
        station("2", location={"type": "Point", "coordinates": [0, 0]})
        with TemporaryDirectory() as directory:
            output = StringIO()
            path = Path(directory) / "review.csv"
            call_command("geocoding_report", review_csv=path, stdout=output)
            checks = json.loads(output.getvalue())["checks"]
            self.assertEqual(checks["successful_without_location"], 1)
            self.assertEqual(checks["unsuccessful_with_location"], 1)
            with path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_empty_database_and_json_file_output(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reports" / "progress.json"
            call_command("geocoding_report", output=path)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["total_stations"], 0)
            self.assertIsNone(report["latest_geocoded_at"])

    def test_invalid_output_options_are_rejected_before_database_read(self):
        with self.assertNumQueries(0), self.assertRaises(CommandError):
            call_command("geocoding_report", include_successful=True)
        with self.assertNumQueries(0), self.assertRaises(CommandError):
            call_command("geocoding_report", output=Path("same"), review_csv=Path("same"))


@override_settings(TOMTOM_API_KEY="test-key")
class GeocodeCommandTests(TestCase):
    def test_only_required_records_are_processed_and_state_is_passed(self):
        station("1")
        station("2", geocoding_status="successful")
        station("3", geocoding_status="ambiguous", geocoding_provider="tomtom")
        station("4", geocoding_status="failed", geocoding_provider="mapbox")
        station("5", address_conflict=True)
        with patch("stations.management.commands.geocode_stations.TomTomGeocoder") as provider:
            provider.return_value.provider_name = "tomtom"
            provider.return_value.geocode.return_value = GeocodingResult(
                status="successful", longitude=-96.8, latitude=32.7, relevance=8,
            )
            call_command("geocode_stations", limit=10, delay=0, stdout=StringIO())
            provider.return_value.geocode.assert_called_once()
            self.assertEqual(provider.return_value.geocode.call_args.kwargs, {
                "expected_city": "Dallas", "expected_state": "TX",
            })
        obj = FuelStation.objects.get(pk="1")
        self.assertEqual(obj.location["coordinates"], [-96.8, 32.7])
        self.assertEqual(FuelStation.objects.get(pk="3").geocoding_status, "ambiguous")

    def test_retry_keeps_other_provider_and_successful_records_untouched(self):
        station("1", geocoding_status="ambiguous", geocoding_provider="tomtom")
        station("2", geocoding_status="successful", geocoding_provider="tomtom")
        station("3", geocoding_status="failed", geocoding_provider="mapbox")
        with patch("stations.management.commands.geocode_stations.TomTomGeocoder") as provider:
            provider.return_value.provider_name = "tomtom"
            provider.return_value.geocode.return_value = GeocodingResult(status="failed", error="No result")
            call_command("geocode_stations", retry_failed=True, limit=10, delay=0, stdout=StringIO())
            provider.return_value.geocode.assert_called_once()
        self.assertEqual(FuelStation.objects.get(pk="2").geocoding_status, "successful")
        self.assertEqual(FuelStation.objects.get(pk="3").geocoding_provider, "mapbox")

    def test_provider_failure_leaves_record_resumable(self):
        station()
        with patch("stations.management.commands.geocode_stations.TomTomGeocoder") as provider:
            provider.return_value.geocode.side_effect = RuntimeError("TomTom rate limit reached")
            with self.assertRaises(CommandError):
                call_command("geocode_stations", limit=1, delay=0, stdout=StringIO())
        self.assertEqual(FuelStation.objects.get().geocoding_status, "required")

    def test_address_conflicts_do_not_consume_batch_limit(self):
        station("1", address_conflict=True)
        station("2")
        with patch("stations.management.commands.geocode_stations.TomTomGeocoder") as provider:
            provider.return_value.provider_name = "tomtom"
            provider.return_value.geocode.return_value = GeocodingResult(status="failed", error="No result")
            call_command("geocode_stations", limit=1, delay=0, stdout=StringIO())
            provider.return_value.geocode.assert_called_once()
        self.assertEqual(FuelStation.objects.get(pk="1").geocoding_status, "required")
        self.assertEqual(FuelStation.objects.get(pk="2").geocoding_status, "failed")
