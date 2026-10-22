import copy
import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.db import DatabaseError

from routing.services import Coordinate, OpenRouteService, RoutingError, normalize_route


START = Coordinate(-96.7970, 32.7767)
DESTINATION = Coordinate(-95.3698, 29.7604)
REQUEST_BODY = {
    "start": {"longitude": START.longitude, "latitude": START.latitude},
    "destination": {"longitude": DESTINATION.longitude, "latitude": DESTINATION.latitude},
}


def provider_response():
    # Small synthetic shape with hand-checkable SI-to-mile conversion. This is
    # an offline contract fixture, not a claim about the real Dallas road route.
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [
                START.as_list(), [-96.0, 31.0], DESTINATION.as_list(),
            ]},
            "properties": {
                "summary": {"distance": 160934.4, "duration": 7200},
                "way_points": [0, 2],
                "extras": {"countryinfo": {"values": [[0, 2, 214]]}},
            },
        }],
        "metadata": {"attribution": "Provider attribution", "query": {"api_key": "never-return-this"}},
    }


def response_stream(payload=None):
    return BytesIO(json.dumps(provider_response() if payload is None else payload).encode())


def http_error(status, code=None):
    return HTTPError(
        "https://example.invalid/?secret=do-not-leak", status, "do-not-leak",
        {}, BytesIO(json.dumps({"error": {"code": code, "message": "do-not-leak"}}).encode()),
    )


class RoutingServiceTests(SimpleTestCase):
    def test_single_post_has_correct_coordinate_order_header_and_units(self):
        with patch("routing.services.urlopen", return_value=response_stream()) as transport:
            route = OpenRouteService("secret-key").main_route(START, DESTINATION)
        transport.assert_called_once()
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "secret-key")
        body = json.loads(request.data)
        self.assertEqual(body["coordinates"], [START.as_list(), DESTINATION.as_list()])
        self.assertEqual(body["units"], "m")
        self.assertEqual(body["extra_info"], ["countryinfo"])
        self.assertEqual(transport.call_args.kwargs["timeout"], 30)
        self.assertAlmostEqual(route.distance_miles, 100)
        self.assertEqual(route.duration_seconds, 7200)
        self.assertNotIn("never-return-this", json.dumps(route.as_dict()))

    def test_invalid_coordinates_are_rejected(self):
        for longitude, latitude in ((181, 0), (0, 91), (True, 0), (0, float("nan")), (float("inf"), 0), (10**400, 0)):
            with self.subTest(longitude=longitude, latitude=latitude), self.assertRaises(RoutingError):
                Coordinate(longitude, latitude)

    def test_missing_key_and_identical_points_do_not_call_provider(self):
        with patch("routing.services.urlopen") as transport:
            with self.assertRaises(RoutingError) as caught:
                OpenRouteService("")
            self.assertEqual(caught.exception.code, "routing_not_configured")
            with self.assertRaises(RoutingError):
                OpenRouteService("test").main_route(START, START)
            transport.assert_not_called()

    def test_provider_errors_are_safe_and_never_retried(self):
        cases = (
            (http_error(401), "routing_authentication_failed", 503),
            (http_error(403), "routing_access_denied", 503),
            (http_error(429), "routing_rate_limited", 503),
            (http_error(503), "routing_unavailable", 503),
            (http_error(400, 2004), "route_limit_exceeded", 422),
            (http_error(413), "route_limit_exceeded", 422),
            (http_error(404, 2009), "route_not_found", 422),
            (http_error(404, 2010), "route_not_found", 422),
            (http_error(404), "routing_request_failed", 502),
            (http_error(400, []), "routing_request_failed", 502),
            (TimeoutError(), "routing_timeout", 504),
            (URLError(TimeoutError()), "routing_timeout", 504),
            (URLError("do-not-leak"), "routing_unavailable", 503),
            (ConnectionResetError(), "routing_unavailable", 503),
        )
        for error, code, status in cases:
            with self.subTest(code=code), patch("routing.services.urlopen", side_effect=error) as transport:
                with self.assertRaises(RoutingError) as caught:
                    OpenRouteService("do-not-leak").main_route(START, DESTINATION)
                self.assertEqual((caught.exception.code, caught.exception.status_code), (code, status))
                self.assertNotIn("do-not-leak", str(caught.exception))
                transport.assert_called_once()

    def test_invalid_json_is_safe(self):
        with patch("routing.services.urlopen", return_value=BytesIO(b"not-json")):
            with self.assertRaises(RoutingError) as caught:
                OpenRouteService("test").main_route(START, DESTINATION)
        self.assertEqual(caught.exception.code, "invalid_routing_response")

    def test_empty_routes_and_returned_road_distance_limit(self):
        payload = provider_response()
        payload["features"] = []
        with self.assertRaises(RoutingError) as caught:
            normalize_route(payload)
        self.assertEqual(caught.exception.code, "route_not_found")
        payload = provider_response()
        payload["features"][0]["properties"]["summary"]["distance"] = 6_000_001
        with self.assertRaises(RoutingError) as caught:
            normalize_route(payload)
        self.assertEqual(caught.exception.code, "route_limit_exceeded")

    def test_malformed_routes_fail_instead_of_returning_partial_geometry(self):
        modifications = (
            ("geometry", None),
            ("geometry", {"type": "Polygon", "coordinates": []}),
            ("geometry", {"type": "LineString", "coordinates": [[-96, 32]]}),
            ("geometry", {"type": "LineString", "coordinates": [[-96, 32], [0, float("nan")]]}),
            ("geometry", {"type": "LineString", "coordinates": [[-96, 32], [999, 32]]}),
            ("properties", None),
            ("properties", {"summary": {"distance": "100"}}),
        )
        for key, value in modifications:
            payload = provider_response()
            payload["features"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RoutingError):
                normalize_route(payload)
        for field, value in (("distance", -1), ("distance", True), ("duration", float("inf")), ("duration", -1)):
            payload = provider_response()
            payload["features"][0]["properties"]["summary"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RoutingError):
                normalize_route(payload)

    def test_optional_duration_and_attribution_fallback(self):
        payload = provider_response()
        del payload["metadata"]
        del payload["features"][0]["properties"]["summary"]["duration"]
        route = normalize_route(payload)
        self.assertIsNone(route.duration_seconds)
        self.assertIn("HeiGIT", route.attribution)

    def test_country_validation_checks_both_endpoints_and_fails_closed(self):
        for sections, expected in (
            ([[0, 2, 35]], "outside_us"),
            ([[0, 1, 214], [1, 2, 128]], "outside_us"),
            ([[0, 2, 0]], "route_country_unverified"),
            ([], "route_country_unverified"),
            ([[0, 1, 214]], "invalid_routing_response"),
            ([[1, 2, 214]], "invalid_routing_response"),
        ):
            payload = provider_response()
            payload["features"][0]["properties"]["extras"]["countryinfo"]["values"] = sections
            with self.subTest(sections=sections), self.assertRaises(RoutingError) as caught:
                normalize_route(payload)
            self.assertEqual(caught.exception.code, expected)

    def test_us_endpoints_may_have_foreign_transit_segments(self):
        payload = provider_response()
        payload["features"][0]["geometry"]["coordinates"].insert(1, [-96.1, 32])
        properties = payload["features"][0]["properties"]
        properties["way_points"] = [0, 3]
        properties["extras"]["countryinfo"]["values"] = [[0, 1, 214], [1, 2, 35], [2, 3, 214]]
        self.assertGreater(normalize_route(payload).distance_miles, 0)


@override_settings(ORS_API_KEY="test-key")
class MainRouteApiTests(SimpleTestCase):
    def post(self, data):
        return self.client.post("/api/routes/main/", data=json.dumps(data), content_type="application/json")

    def test_success_returns_normalized_route_without_database_access(self):
        with patch("routing.services.urlopen", return_value=response_stream()) as transport:
            response = self.post(REQUEST_BODY)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertAlmostEqual(body["route"]["distance_miles"], 100)
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertEqual(body["metrics"]["routing_api_calls"], 1)
        self.assertGreaterEqual(body["metrics"]["routing_elapsed_ms"], 0)
        self.assertNotIn("fuel_stops", body)
        self.assertNotIn("test-key", response.content.decode())
        transport.assert_called_once()

    def test_invalid_requests_never_call_provider(self):
        invalid = [{}, [], None, {"start_address": "Dallas"}]
        for field, value in (("latitude", 91), ("latitude", True), ("longitude", "-96"), ("longitude", 10**400)):
            body = copy.deepcopy(REQUEST_BODY)
            body["start"][field] = value
            invalid.append(body)
        identical = copy.deepcopy(REQUEST_BODY)
        identical["destination"] = identical["start"]
        invalid.append(identical)
        unknown = copy.deepcopy(REQUEST_BODY)
        unknown["profile"] = "walking"
        invalid.append(unknown)
        with patch("routing.services.urlopen") as transport:
            for body in invalid:
                with self.subTest(body=body):
                    response = self.post(body)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["code"], "invalid_request")
            transport.assert_not_called()

    def test_missing_key_returns_configuration_error(self):
        with override_settings(ORS_API_KEY=""), patch("routing.services.urlopen") as transport:
            response = self.post(REQUEST_BODY)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "routing_not_configured")
        transport.assert_not_called()

    def test_provider_errors_return_clear_json(self):
        for error, status, code in (
            (TimeoutError(), 504, "routing_timeout"),
            (http_error(429), 503, "routing_rate_limited"),
            (http_error(404, 2009), 422, "route_not_found"),
            (http_error(400, 2004), 422, "route_limit_exceeded"),
        ):
            with self.subTest(code=code), patch("routing.services.urlopen", side_effect=error):
                response = self.post(REQUEST_BODY)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn("do-not-leak", response.content.decode())

    def test_get_is_not_supported(self):
        with patch("routing.services.urlopen") as transport:
            self.assertEqual(self.client.get("/api/routes/main/").status_code, 405)
            transport.assert_not_called()

    @override_settings(USE_POSTGIS=True)
    def test_opt_in_candidate_search_uses_one_route_call_and_passes_options(self):
        request = copy.deepcopy(REQUEST_BODY)
        request["candidate_search"] = {"corridor_miles": 4, "limit": 20}
        candidates = {"stations": [], "matched_count": 0, "returned_count": 0, "fuel_feasibility_evaluated": False}
        with patch("routing.services.urlopen", return_value=response_stream()) as transport:
            with patch("routing.views.find_candidates", return_value=candidates) as search:
                response = self.post(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidate_search"], candidates)
        self.assertEqual(search.call_args.kwargs, {"corridor_miles": 4, "limit": 20})
        self.assertEqual(response.json()["metrics"]["routing_api_calls"], 1)
        transport.assert_called_once()

    def test_invalid_candidate_options_do_not_make_provider_calls(self):
        with patch("routing.services.urlopen") as transport:
            for options in ({"corridor_miles": 26}, {"limit": 201}, {"unknown": True}, None):
                request = copy.deepcopy(REQUEST_BODY)
                request["candidate_search"] = options
                with self.subTest(options=options):
                    self.assertEqual(self.post(request).status_code, 400)
            transport.assert_not_called()

    @override_settings(USE_POSTGIS=False)
    def test_candidate_search_without_postgis_fails_before_external_call(self):
        request = copy.deepcopy(REQUEST_BODY)
        request["candidate_search"] = {}
        with patch("routing.services.urlopen") as transport:
            response = self.post(request)
        self.assertEqual(response.status_code, 503)
        transport.assert_not_called()

    @override_settings(USE_POSTGIS=True)
    def test_candidate_database_failure_returns_safe_error(self):
        request = copy.deepcopy(REQUEST_BODY)
        request["candidate_search"] = {}
        with patch("routing.services.urlopen", return_value=response_stream()):
            with patch("routing.views.find_candidates", side_effect=DatabaseError("private database details")):
                response = self.post(request)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private database details", response.content.decode())
