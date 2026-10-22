import copy
import json
from io import BytesIO
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from fuel.planning import InfeasibleFuelPlan, ItineraryStation, plan_fuel_trip
from routing.detours import estimate_itinerary_legs, evaluate_detours
from routing.matrix import normalize_matrix
from routing.services import Coordinate, METERS_PER_MILE, OpenRouteService, RoutingError
from routing.tests.test_routing import REQUEST_BODY, http_error, provider_response, response_stream
from routing.services import normalize_route


def candidate(pk="a", position=40, price="3"):
    return {
        "station_id": pk, "name": "Example station", "longitude": -96.797,
        "latitude": 32.78, "price_per_gallon": price,
        "route_position_miles": position, "distance_to_route_miles": 0.2,
    }


def matrix_payload(locations):
    size = len(locations)
    snaps = [{"location": point.as_list(), "snapped_distance": 1.0} for point in locations]
    return {
        "distances": [[0 if source == destination else 1609.344 for destination in range(size)] for source in range(size)],
        "sources": copy.deepcopy(snaps), "destinations": copy.deepcopy(snaps),
        "metadata": {"attribution": "Matrix provider", "query": {"api_key": "do-not-leak"}},
    }


def itinerary_station(pk, position, access, returning):
    return {"station_id": pk, "status": "evaluated", "anchor": {"route_position_miles_estimate": position},
            "access_miles": access, "return_miles": returning}


class MatrixTests(SimpleTestCase):
    def test_request_contract_and_directional_metres_are_preserved(self):
        points = [Coordinate(-96, 32), Coordinate(-96, 32.01)]
        payload = matrix_payload(points)
        payload["distances"][1][0] = 3218.688
        with patch("routing.services.urlopen", return_value=BytesIO(json.dumps(payload).encode())) as transport:
            matrix = OpenRouteService("secret-key").distance_matrix(points)
        transport.assert_called_once()
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.heigit.org/openrouteservice/v2/matrix/driving-car")
        self.assertEqual(json.loads(request.data), {"locations": [point.as_list() for point in points], "metrics": ["distance"], "units": "m"})
        self.assertEqual(request.get_header("Authorization"), "secret-key")
        self.assertEqual(matrix.distances_meters[1][0], 3218.688)
        self.assertEqual(matrix.distances_meters[0][1], 1609.344)
        self.assertEqual(matrix.attribution, "Matrix provider")

    def test_invalid_matrix_dimensions_values_or_snaps_are_rejected(self):
        points = [Coordinate(-96, 32), Coordinate(-96, 33)]
        valid = matrix_payload(points)
        cases = [[], {}, {**valid, "distances": [[0, 1]]}]
        for value in (-1, True, float("nan"), float("inf"), "10"):
            payload = copy.deepcopy(valid)
            payload["distances"][0][1] = value
            cases.append(payload)
        payload = copy.deepcopy(valid)
        payload["sources"][0]["location"] = [999, 32]
        cases.append(payload)
        payload = copy.deepcopy(valid)
        del payload["destinations"][0]["snapped_distance"]
        cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(RoutingError) as caught:
                normalize_matrix(payload, 2)
            self.assertEqual(caught.exception.code, "invalid_matrix_response")

    def test_null_distance_is_unrouted_not_zero(self):
        points = [Coordinate(-96, 32), Coordinate(-96, 33)]
        payload = matrix_payload(points)
        payload["distances"][0][1] = None
        self.assertIsNone(normalize_matrix(payload, 2).distances_meters[0][1])

    def test_live_response_without_snap_distances_uses_coordinate_quality_check(self):
        points = [Coordinate(-96, 32), Coordinate(-96, 33)]
        payload = matrix_payload(points)
        for key in ("sources", "destinations"):
            for item in payload[key]:
                del item["snapped_distance"]
        payload["sources"][0]["location"][1] += 0.001
        matrix = normalize_matrix(payload, 2, requested_locations=points)
        self.assertGreater(matrix.sources[0].snapped_distance_meters, 100)
        self.assertEqual(matrix.sources[0].distance_method, "geographic_separation_from_snapped_coordinate")
        self.assertEqual(matrix.distances_meters[0][1], 1609.344)

    def test_location_limit_is_checked_before_external_request(self):
        with patch("routing.services.urlopen") as transport:
            for size in (1, 59):
                with self.subTest(size=size), self.assertRaises(RoutingError):
                    OpenRouteService("test").distance_matrix([Coordinate(-96, 32)] * size)
            transport.assert_not_called()

    def test_matrix_limit_and_unsnappable_point_errors_are_safe(self):
        for code, expected in ((6004, "route_limit_exceeded"), (6010, "route_not_found")):
            with patch("routing.services.urlopen", side_effect=http_error(400, code)) as transport:
                with self.assertRaises(RoutingError) as caught:
                    OpenRouteService("secret").distance_matrix([Coordinate(-96, 32), Coordinate(-96, 33)])
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("do-not-leak", str(caught.exception))
            transport.assert_called_once()


class DetourEvaluationTests(SimpleTestCase):
    def setUp(self):
        self.route = normalize_route(provider_response())
        self.router = Mock()
        self.router.distance_matrix.side_effect = lambda points: normalize_matrix(matrix_payload(points), len(points))

    def test_one_matrix_measures_access_and_return_independently(self):
        def response(points):
            payload = matrix_payload(points)
            payload["distances"][1][0] = 3 * METERS_PER_MILE
            return normalize_matrix(payload, 2)
        self.router.distance_matrix.side_effect = response
        report = evaluate_detours(self.router, self.route, [candidate()])
        self.assertEqual(report["matrix_api_calls"], 1)
        self.assertEqual(report["usable_count"], 1)
        stop = report["stations"][0]
        self.assertEqual(stop["access_miles"], 1)
        self.assertEqual(stop["return_miles"], 3)
        self.assertEqual(stop["detour_miles"], 4)
        self.assertEqual(stop["detour_gallons_consumed"], 0.4)
        self.assertFalse(report["fuel_feasibility_evaluated"])
        self.router.distance_matrix.assert_called_once()

    def test_zero_candidates_makes_no_matrix_call(self):
        report = evaluate_detours(self.router, self.route, [])
        self.assertEqual(report["matrix_api_calls"], 0)
        self.assertEqual(report["usable_count"], 0)
        self.router.distance_matrix.assert_not_called()

    def test_detour_shortlist_is_bounded_before_matrix_request(self):
        candidates = [candidate(str(index), position=index, price=str(index + 1)) for index in range(50)]
        report = evaluate_detours(self.router, self.route, candidates, limit=29)
        self.assertEqual(report["evaluated_count"], 29)
        self.assertEqual(report["omitted_count"], 21)
        self.assertEqual(report["matrix_location_count"], 58)
        self.assertEqual(report["matrix_pair_count"], 3364)
        self.router.distance_matrix.assert_called_once()

    def test_unrouted_and_excessive_or_mismatched_snaps_remain_unusable(self):
        for kind in ("unrouted", "far", "mismatch"):
            def response(points):
                payload = matrix_payload(points)
                if kind == "unrouted":
                    payload["distances"][0][1] = None
                elif kind == "far":
                    payload["sources"][1]["snapped_distance"] = 101
                else:
                    payload["destinations"][1]["location"][0] += 0.001
                return normalize_matrix(payload, 2)
            self.router.distance_matrix.side_effect = response
            with self.subTest(kind=kind):
                report = evaluate_detours(self.router, self.route, [candidate()])
                self.assertEqual(report["usable_count"], 0)
                self.assertNotIn("detour_miles", report["stations"][0])

    def test_identical_anchor_coordinates_do_not_lose_station_mapping(self):
        report = evaluate_detours(self.router, self.route, [candidate("a"), candidate("b")])
        self.assertEqual([item["station_id"] for item in report["stations"]], ["a", "b"])
        self.assertTrue(all(item["detour_miles"] == 2 for item in report["stations"]))


class ItineraryDistanceTests(SimpleTestCase):
    def test_access_return_and_main_route_are_each_counted_once(self):
        selected = [itinerary_station("a", 100, 3, 4), itinerary_station("b", 300, 5, 6)]
        legs = estimate_itinerary_legs(600, selected)
        self.assertEqual([leg["distance_miles_estimate"] for leg in legs], [103, 209, 306])
        self.assertEqual(sum(leg["distance_miles_estimate"] for leg in legs), 600 + 3 + 4 + 5 + 6)

    def test_detour_can_make_station_unreachable_in_hand_calculated_model(self):
        legs = estimate_itinerary_legs(700, [itinerary_station("cheap", 490, 20, 20)])
        self.assertEqual(legs[0]["distance_miles_estimate"], 510)
        with self.assertRaises(InfeasibleFuelPlan):
            plan_fuel_trip(740, [ItineraryStation("cheap", 510, 2)])

    def test_cheaper_station_detour_can_raise_total_purchase_cost(self):
        nearby = plan_fuel_trip(700, [ItineraryStation("nearby", 400, 3)])
        legs = estimate_itinerary_legs(700, [itinerary_station("cheaper", 400, 50, 50)])
        total = sum(leg["distance_miles_estimate"] for leg in legs)
        cheaper = plan_fuel_trip(total, [ItineraryStation("cheaper", legs[0]["distance_miles_estimate"], 2.5)])
        self.assertEqual(nearby.total_fuel_cost, 60)
        self.assertEqual(cheaper.total_fuel_cost, 75)

    def test_same_anchor_two_actual_excursions_count_both_round_trips(self):
        selected = [itinerary_station("a", 100, 2, 3), itinerary_station("b", 100, 4, 5)]
        self.assertEqual(sum(leg["distance_miles_estimate"] for leg in estimate_itinerary_legs(200, selected)), 214)

    def test_invalid_order_duplicates_and_unevaluated_stations_are_rejected(self):
        for selected in (
            [itinerary_station("a", 300, 1, 1), itinerary_station("b", 100, 1, 1)],
            [itinerary_station("a", 100, 1, 1), itinerary_station("a", 200, 1, 1)],
            [{**itinerary_station("a", 100, 1, 1), "status": "unrouted"}],
        ):
            with self.subTest(selected=selected), self.assertRaises(RoutingError):
                estimate_itinerary_legs(600, selected)


@override_settings(ORS_API_KEY="test-key", USE_POSTGIS=True)
class DetourApiTests(SimpleTestCase):
    def body(self):
        return {**REQUEST_BODY, "candidate_search": {}, "detour_evaluation": {"limit": 20}}

    def post(self, body):
        return self.client.post("/api/routes/main/", data=json.dumps(body), content_type="application/json")

    def test_main_route_plus_detours_uses_exactly_two_provider_requests(self):
        def transport(request, **kwargs):
            if "/directions/" in request.full_url:
                return response_stream()
            points = [Coordinate(*point) for point in json.loads(request.data)["locations"]]
            return BytesIO(json.dumps(matrix_payload(points)).encode())
        with patch("routing.services.urlopen", side_effect=transport) as http:
            with patch("routing.views.find_candidates", return_value={"stations": [candidate()]}):
                response = self.post(self.body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["routing_api_calls"], 2)
        self.assertEqual(response.json()["detour_evaluation"]["usable_count"], 1)
        self.assertEqual(http.call_count, 2)

    def test_empty_shortlist_still_uses_only_one_provider_request(self):
        with patch("routing.services.urlopen", return_value=response_stream()) as http:
            with patch("routing.views.find_candidates", return_value={"stations": []}):
                response = self.post(self.body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["routing_api_calls"], 1)
        http.assert_called_once()

    def test_matrix_failure_does_not_fall_back_to_straight_line_or_retry(self):
        with patch("routing.services.urlopen", side_effect=[response_stream(), TimeoutError()]) as http:
            with patch("routing.views.find_candidates", return_value={"stations": [candidate()]}):
                response = self.post(self.body())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(http.call_count, 2)
        self.assertNotIn("detour_evaluation", response.json())

    def test_invalid_options_are_rejected_before_routing(self):
        with patch("routing.services.urlopen") as http:
            for body in (
                {**REQUEST_BODY, "detour_evaluation": {}},
                {**self.body(), "detour_evaluation": {"limit": 30}},
                {**self.body(), "detour_evaluation": None},
            ):
                with self.subTest(body=body):
                    self.assertEqual(self.post(body).status_code, 400)
            http.assert_not_called()
