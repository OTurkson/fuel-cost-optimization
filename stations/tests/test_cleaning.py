import csv
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase
from openpyxl import Workbook

from stations.cleaning import EXPECTED_HEADERS, clean


class CleaningProvenanceTests(SimpleTestCase):
    def test_prices_stay_unchanged_under_agreed_demo_unit_assumption(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "source.xlsx"
            output = directory / "cleaned.csv"
            report_path = directory / "report.json"
            workbook = Workbook()
            workbook.active.append(EXPECTED_HEADERS)
            workbook.active.append([1, "Truck Stop", "123 Main St", "Dallas", "tx", 1, 3.5])
            workbook.active.append([1, "Truck Stop", "123 Main St", "Dallas", "tx", 2, 3.25])
            workbook.save(source)
            workbook.close()
            original_hash = hashlib.sha256(source.read_bytes()).digest()
            report = clean(source, output, report_path)
            self.assertFalse(report["retail_price_unit"]["verified"])
            self.assertTrue(report["retail_price_unit"]["assumed_for_demo"])
            self.assertEqual(report["counts"]["original_rows"], 2)
            self.assertEqual(report["counts"]["stations_retained_after_cleaning"], 1)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["selected_price_usd_per_gallon"], "3.25")
            self.assertEqual(original_hash, hashlib.sha256(source.read_bytes()).digest())
