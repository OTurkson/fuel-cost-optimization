import copy
import json
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.cache.backends.filebased import FileBasedCache
from django.test import SimpleTestCase, override_settings

from routing.addresses import AddressResolver, normalize_address
from routing.caching import CachedRouter
from routing.services import Coordinate, MainRoute, METERS_PER_MILE, RoutingError
from routing.tests.test_routing import REQUEST_BODY, provider_response, response_stream


def address_payload(longitude=-96.797, latitude=32.7767, confidence=0.95):
    return {"type": "FeatureCollection", "features": [{"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
            "properties": {"country_a": "USA", "confidence": confidence, "label": "Dallas, Texas, USA"}}]}


class AddressTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_lookup_is_us_restricted_and_normalized_address_hits_cache(self):
        with patch("routing.addresses.urlopen", return_value=BytesIO(json.dumps(address_payload()).encode())) as http:
            resolver = AddressResolver("secret")
            first = resolver.resolve(" Dallas,  TX ")
            self.assertEqual(first, resolver.resolve("dallas, TX"))
        http.assert_called_once()
        request = http.call_args.args[0]
        self.assertIn("boundary.country=USA", request.full_url)
        self.assertNotIn("secret", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "secret")
        self.assertEqual((resolver.api_calls, resolver.cache_hits), (1, 1))

    def test_unresolved_foreign_low_confidence_and_ambiguous_addresses(self):
        cases = [{"type": "FeatureCollection", "features": []}, address_payload(confidence=0.5)]
        foreign = address_payload()
        foreign["features"][0]["properties"]["country_a"] = "CAN"
        cases.append(foreign)
        ambiguous = address_payload()
        ambiguous["features"].extend(address_payload(-100, 40, 0.94)["features"])
        cases.append(ambiguous)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(RoutingError) as caught:
                normalize_address(payload)
            self.assertEqual(caught.exception.status_code, 422)

    def test_malformed_response_and_timeout_are_safe_and_not_cached(self):
        with self.assertRaises(RoutingError) as caught:
            normalize_address({"features": []})
        self.assertEqual(caught.exception.status_code, 502)
        with patch("routing.addresses.urlopen", side_effect=TimeoutError()) as http:
            for _ in range(2):
                with self.assertRaises(RoutingError) as caught:
                    AddressResolver("secret").resolve("Dallas")
                self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(http.call_count, 2)


class CacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_key_includes_coordinate_order_and_success_only_is_cached(self):
        provider = Mock(endpoint="directions", matrix_endpoint="matrix")
        provider.main_route.side_effect = [RoutingError("timeout", "Timeout", 504), "one", "reverse"]
        router = CachedRouter(provider)
        a, b = Coordinate(-96, 32), Coordinate(-95, 30)
        with self.assertRaises(RoutingError):
            router.main_route(a, b)
        self.assertEqual(router.main_route(a, b), "one")
        self.assertEqual(router.main_route(a, b), "one")
        self.assertEqual(router.main_route(b, a), "reverse")
        self.assertEqual(provider.main_route.call_count, 3)
        self.assertEqual(router.metrics["route_cache_hits"], 1)

    def test_file_cache_survives_new_backend_instance_and_expiration(self):
        with TemporaryDirectory() as folder:
            first = FileBasedCache(folder, {})
            first.set("route", {"miles": 700}, 86400)
            second = FileBasedCache(folder, {})
            self.assertEqual(second.get("route"), {"miles": 700})
            first.set("expired", "old", -1)
            self.assertIsNone(second.get("expired"))


@override_settings(ORS_API_KEY="test-key")
class OptimizationApiTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def post(self, body=REQUEST_BODY):
        return self.client.post("/api/routes/optimize/", data=json.dumps(body), content_type="application/json")

    def assert_balanced(self, result):
        s = result["summary"]
        self.assertAlmostEqual(s["starting_fuel_gallons"] + s["total_gallons_purchased"],
                               s["total_gallons_consumed"] + s["ending_fuel_gallons"])
        self.assertAlmostEqual(sum(stop["fuel_cost"] for stop in result["fuel_stops"]), s["total_fuel_cost"])
        self.assertAlmostEqual(s["main_route_miles"] + s["detour_miles"], s["total_distance_miles"])
        self.assertAlmostEqual(sum(leg["distance_miles_estimate"] for leg in result["legs"]), s["total_distance_miles"])
        for stop in result["fuel_stops"]:
            self.assertTrue(0 <= stop["arrival_fuel_gallons"] <= stop["departure_fuel_gallons"] <= 50)
        self.assertTrue(all(leg["distance_miles_estimate"] <= 500 for leg in result["legs"]))

    def test_short_trip_has_complete_numeric_summary_and_repeated_route_hits_cache(self):
        with patch("routing.services.urlopen", return_value=response_stream()) as http:
            first, second = self.post(), self.post()
        self.assertEqual(first.status_code, 200, first.content)
        result = first.json()
        self.assertEqual(result["summary"]["total_fuel_cost"], 0)
        self.assertEqual(result["fuel_stops"], [])
        self.assertEqual(result["route"]["maximum_range_miles"], 500)
        self.assertEqual(result["route"]["fuel_economy_mpg"], 10)
        self.assertEqual(second.json()["metrics"]["routing_api_calls"], 0)
        self.assertEqual(second.json()["metrics"]["route_cache_hits"], 1)
        http.assert_called_once()
        self.assert_balanced(result)

    def test_invalid_or_duplicate_inputs_are_rejected_before_calls(self):
        with patch("routing.services.urlopen") as http:
            for body in ({}, {**REQUEST_BODY, "start_address": "Dallas"},
                         {**REQUEST_BODY, "unknown": True}, {"start_address": " ", "destination_address": "Houston"},
                         {**REQUEST_BODY, "start": {"longitude": True, "latitude": 32}},
                         {**REQUEST_BODY, "detour_evaluation": {"limit": 30}}):
                with self.subTest(body=body):
                    self.assertEqual(self.post(body).status_code, 400)
            http.assert_not_called()

    def test_address_inputs_resolve_and_repeated_lookups_are_cached(self):
        body = {"start_address": "Dallas, TX", "destination_address": "Houston, TX"}
        def resolve(request, **kwargs):
            payload = address_payload() if "Dallas" in request.full_url else address_payload(-95.37, 29.76)
            return BytesIO(json.dumps(payload).encode())
        with patch("routing.addresses.urlopen", side_effect=resolve) as geocode:
            with patch("routing.services.urlopen", return_value=response_stream()):
                first, second = self.post(body), self.post(body)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["metrics"]["geocoding_api_calls"], 2)
        self.assertEqual(second.json()["metrics"]["geocoding_api_calls"], 0)
        self.assertEqual(second.json()["metrics"]["address_cache_hits"], 2)
        self.assertEqual(geocode.call_count, 2)

    @override_settings(USE_POSTGIS=True)
    def test_long_trip_prices_refresh_while_route_and_matrix_are_reused(self):
        from routing.matrix import normalize_matrix
        from routing.tests.test_detours import matrix_payload
        route = MainRoute({"type": "LineString", "coordinates": [[-96, 32], [-96, 35], [-96, 39]]},
                          800 * METERS_PER_MILE, 1000, "ORS")
        candidates = [{"station_id": "a", "name": "A", "longitude": -96, "latitude": 35,
                       "price_per_gallon": "3", "route_position_miles": 385, "distance_to_route_miles": 0},
                      {"station_id": "b", "name": "B", "longitude": -96, "latitude": 35,
                       "price_per_gallon": "4", "route_position_miles": 385, "distance_to_route_miles": 0}]
        provider = Mock()
        provider.endpoint, provider.matrix_endpoint = "directions", "matrix"
        provider.main_route.return_value = route
        provider.distance_matrix.side_effect = lambda points: normalize_matrix(matrix_payload(points), len(points))
        with patch("routing.optimization.OpenRouteService", return_value=provider):
            with patch("routing.optimization.find_candidates", side_effect=lambda *a, **k: {"stations": copy.deepcopy(candidates)}):
                first = self.post()
                candidates[0]["price_per_gallon"] = "5"
                second = self.post()
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual([s["station_id"] for s in first.json()["fuel_stops"]], ["a"])
        self.assertEqual([s["station_id"] for s in second.json()["fuel_stops"]], ["b"])
        self.assertEqual(first.json()["metrics"]["routing_api_calls"], 2)
        self.assertEqual(second.json()["metrics"]["routing_api_calls"], 0)
        provider.main_route.assert_called_once()
        provider.distance_matrix.assert_called_once()
        self.assert_balanced(first.json())
        self.assert_balanced(second.json())

    @override_settings(USE_POSTGIS=True)
    def test_empty_shortlist_returns_clear_infeasibility_without_matrix(self):
        route = MainRoute({"type": "LineString", "coordinates": [[-96, 32], [-96, 39]]},
                          800 * METERS_PER_MILE, 1000, "ORS")
        with patch("routing.services.OpenRouteService.main_route", return_value=route):
            with patch("routing.optimization.find_candidates", return_value={"stations": []}):
                with patch("routing.services.OpenRouteService.distance_matrix") as matrix:
                    response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "fuel_plan_not_found")
        matrix.assert_not_called()

    def test_timeout_is_returned_without_retry(self):
        with patch("routing.services.urlopen", side_effect=TimeoutError()) as http:
            response = self.post()
        self.assertEqual(response.status_code, 504)
        http.assert_called_once()

    @override_settings(USE_POSTGIS=True)
    def test_multiple_stops_use_one_directions_and_one_matrix_request(self):
        from routing.tests.test_detours import matrix_payload
        payload = provider_response()
        feature = payload["features"][0]
        feature["geometry"]["coordinates"] = [[-96, 32], [-96, 36], [-96, 40], [-96, 44]]
        feature["properties"].update(summary={"distance": 1300 * METERS_PER_MILE, "duration": 1000},
                                     way_points=[0, 3], extras={"countryinfo": {"values": [[0, 3, 214]]}})
        candidates = [{"station_id": str(index), "name": "Station " + str(index),
                       "longitude": -96, "latitude": lat, "price_per_gallon": str(4 - index),
                       "route_position_miles": position, "distance_to_route_miles": 0}
                      for index, (lat, position) in enumerate(((36, 433), (40, 867)))]

        def transport(request, **kwargs):
            if "/directions/" in request.full_url:
                return response_stream(payload)
            points = [Coordinate(*point) for point in json.loads(request.data)["locations"]]
            return BytesIO(json.dumps(matrix_payload(points)).encode())

        with patch("routing.services.urlopen", side_effect=transport) as http:
            with patch("routing.optimization.find_candidates", return_value={"stations": candidates}):
                response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["summary"]["number_of_stops"], 2)
        self.assertAlmostEqual(result["summary"]["total_distance_miles"], 1304)
        self.assertEqual(result["metrics"]["routing_api_calls"], 2)
        self.assertEqual(http.call_count, 2)
        self.assertNotIn("never-return-this", response.content.decode())
        self.assert_balanced(result)
