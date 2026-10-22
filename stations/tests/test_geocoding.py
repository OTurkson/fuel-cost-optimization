import copy
import json
from io import StringIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from django.test import SimpleTestCase

from stations.geocoding import MapboxGeocoder, TomTomGeocoder, mapbox_station_query


def match(**overrides):
    result = {
        "type": "POI", "score": 8.0,
        "position": {"lon": -96.8, "lat": 32.7},
        "poi": {"name": "Example Truck Stop"},
        "address": {
            "countryCode": "US", "countrySubdivision": "TX",
            "municipality": "Dallas", "freeformAddress": "123 Main St, Dallas, TX",
        },
    }
    result.update(overrides)
    return result


class TomTomMatchingTests(SimpleTestCase):
    def geocode(self, results):
        with patch(
            "stations.geocoding.urlopen",
            return_value=StringIO(json.dumps({"results": results})),
        ):
            return TomTomGeocoder("test-key").geocode(
                "Example Truck Stop, Dallas, TX, USA", expected_city="Dallas", expected_state="TX"
            )

    def test_precise_point_address_is_accepted(self):
        result = self.geocode([match(type="Point Address", poi={})])
        self.assertEqual(result.status, "successful")
        self.assertEqual((result.longitude, result.latitude), (-96.8, 32.7))

    def test_wrong_or_missing_state_is_not_accepted(self):
        for state in ("GA", ""):
            with self.subTest(state=state):
                candidate = match()
                candidate["address"]["countrySubdivision"] = state
                self.assertEqual(self.geocode([candidate]).status, "ambiguous")

    def test_wrong_state_does_not_hide_a_correct_lower_ranked_match(self):
        wrong = match(score=9)
        wrong["address"]["countrySubdivision"] = "GA"
        self.assertEqual(self.geocode([wrong, match()]).status, "successful")

    def test_wrong_city_or_country_is_not_accepted(self):
        for field, value in (("municipality", "Austin"), ("countryCode", "CA")):
            with self.subTest(field=field):
                candidate = match()
                candidate["address"][field] = value
                self.assertEqual(self.geocode([candidate]).status, "ambiguous")

    def test_imprecise_result_types_are_rejected(self):
        for kind in ("Street", "Cross Street", "Address Range", "Geography", "Address"):
            with self.subTest(kind=kind):
                self.assertEqual(self.geocode([match(type=kind)]).status, "ambiguous")

    def test_invalid_coordinates_or_scores_are_failed(self):
        for candidate in (
            match(position={"lon": -181, "lat": 32}),
            match(position={"lon": -96, "lat": 91}),
            match(position={"lon": float("nan"), "lat": 32}),
            match(position={"lon": True, "lat": 32}),
            match(score=float("inf")),
            match(score=None),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.geocode([candidate]).status, "failed")

    def test_nearby_duplicate_does_not_hide_third_distinct_candidate(self):
        first = match()
        duplicate = copy.deepcopy(first)
        distinct = match(poi={"name": "Different Truck Stop"})
        self.assertEqual(self.geocode([first, duplicate, distinct]).status, "ambiguous")

    def test_duplicate_poi_at_same_location_is_accepted(self):
        self.assertEqual(self.geocode([match(), match()]).status, "successful")

    def test_same_brand_at_different_locations_remains_ambiguous(self):
        distant = match(position={"lat": 32.71, "lon": -96.8})
        self.assertEqual(self.geocode([match(), distant]).status, "ambiguous")

    def test_blank_names_and_addresses_are_not_identity_evidence(self):
        first = match(poi={})
        first["address"]["freeformAddress"] = ""
        second = copy.deepcopy(first)
        self.assertEqual(self.geocode([first, second]).status, "ambiguous")

    def test_no_results_or_malformed_response_are_failed(self):
        for results in ([], [None], [{"address": None}]):
            with self.subTest(results=results):
                self.assertEqual(self.geocode(results).status, "failed")
        with patch("stations.geocoding.urlopen", return_value=StringIO("[]")):
            self.assertEqual(TomTomGeocoder("test").geocode("query").status, "failed")

    def test_provider_errors_stop_batch_without_leaking_key(self):
        for error in (
            HTTPError("https://example.invalid/?key=secret", 429, "rate limit", {}, None),
            HTTPError("https://example.invalid/?key=secret", 401, "unauthorized", {}, None),
            URLError("network unavailable"),
            TimeoutError("timeout"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch("stations.geocoding.urlopen", side_effect=error):
                    with self.assertRaises(RuntimeError) as caught:
                        TomTomGeocoder("secret").geocode("query")
                self.assertNotIn("secret", str(caught.exception))

    def test_query_path_encodes_slashes(self):
        with patch("stations.geocoding.urlopen", return_value=StringIO('{"results":[]}')) as request:
            TomTomGeocoder("secret").geocode("US 1/9, Dallas")
        self.assertIn("US%201%2F9%2C%20Dallas.json", request.call_args.args[0].full_url)


class MapboxQueryTests(SimpleTestCase):
    def test_station_query_uses_address_without_business_name(self):
        self.assertEqual(
            mapbox_station_query("123 Main St", "Dallas", "TX"),
            "123 Main St, Dallas, TX, USA",
        )

    def test_geocode_requests_exact_address_features(self):
        payload = {
            "features": [
                {
                    "geometry": {"coordinates": [-96.8, 32.7]},
                    "properties": {"feature_type": "address"},
                }
            ]
        }
        with patch(
            "stations.geocoding.urlopen",
            return_value=StringIO(json.dumps(payload)),
        ) as request:
            result = MapboxGeocoder("test-token").geocode(
                "123 Main St, Dallas, TX, USA"
            )

        self.assertEqual(result.status, "successful")
        url = request.call_args.args[0].full_url
        self.assertIn("types=address", url)
        self.assertIn("autocomplete=false", url)
