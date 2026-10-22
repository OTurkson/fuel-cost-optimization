from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from routing.services import MainRoute, RoutingError
from stations.candidates import StationCandidate, find_candidates, shortlist_candidates


def candidate(pk, position, price="3", offset=0):
    return StationCandidate(pk, "Example", -96, 32, Decimal(price), position, offset)


def route():
    return MainRoute(
        geometry={"type": "LineString", "coordinates": [[-96, 32], [-95, 30]]},
        distance_meters=1609344, duration_seconds=None, attribution="Test",
    )


class CandidateShortlistTests(SimpleTestCase):
    def test_keeps_candidates_distributed_instead_of_only_cheapest_cluster(self):
        options = [candidate("a", 10, "1"), candidate("b", 20, "1.1"), candidate("c", 30, "1.2"),
                   candidate("far", 650, "4"), candidate("end", 950, "5")]
        result = shortlist_candidates(options, 1000, 3)
        self.assertEqual([item.station_id for item in result], ["a", "far", "end"])

    def test_price_then_proximity_and_id_give_deterministic_selection(self):
        options = [candidate("b", 40, "3", 2), candidate("a", 40, "3", 1), candidate("c", 40, "2", 3)]
        self.assertEqual(shortlist_candidates(options, 100, 1)[0].station_id, "c")
        self.assertEqual([item.station_id for item in shortlist_candidates(options, 100, 2)], ["a", "c"])
        self.assertEqual(shortlist_candidates(list(reversed(options)), 100, 2), shortlist_candidates(options, 100, 2))

    def test_full_shortlist_is_ordered_by_route_position_and_includes_boundaries(self):
        options = [candidate("end", 1000), candidate("middle", 400), candidate("start", 0)]
        self.assertEqual([item.station_id for item in shortlist_candidates(options, 1000, 50)], ["start", "middle", "end"])
        self.assertEqual(shortlist_candidates([], 1000, 50), [])

    @override_settings(USE_POSTGIS=True)
    def test_search_materializes_one_snapshot_and_exposes_estimate_limitations(self):
        rows = [
            {"opis_truckstop_id": "a", "truckstop_name": "Example", "location": SimpleNamespace(x=-96, y=32),
             "selected_price_usd_per_gallon": Decimal("3.123456"), "route_fraction": 0.25,
             "route_offset": SimpleNamespace(m=1609.344)},
            {"opis_truckstop_id": "b", "truckstop_name": "Example", "location": SimpleNamespace(x=-95, y=31),
             "selected_price_usd_per_gallon": Decimal("2"), "route_fraction": 0.75,
             "route_offset": SimpleNamespace(m=0)},
        ]
        with patch("stations.candidates.corridor_queryset", return_value=iter(rows)) as query:
            result = find_candidates(route(), limit=1)
        query.assert_called_once()
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["returned_count"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stations"][0]["station_id"], "b")
        self.assertFalse(result["fuel_feasibility_evaluated"])
        self.assertIn("not a driving detour", result["distance_to_route_method"])
        with patch("stations.candidates.corridor_queryset", return_value=rows):
            all_matches = find_candidates(route())
        first = all_matches["stations"][0]
        self.assertAlmostEqual(first["route_position_miles"], 250)
        self.assertAlmostEqual(first["distance_to_route_miles"], 1)
        self.assertEqual(first["price_per_gallon"], "3.123456")

    def test_invalid_options_or_missing_postgis_make_no_query(self):
        with patch("stations.candidates.corridor_queryset") as query:
            for options in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"corridor_miles": 0}, {"corridor_miles": 26}, {"corridor_miles": float("nan")}):
                with self.subTest(options=options), self.assertRaises(RoutingError):
                    find_candidates(route(), **options)
            with override_settings(USE_POSTGIS=False), self.assertRaises(RoutingError) as caught:
                find_candidates(route())
            self.assertEqual(caught.exception.code, "candidate_search_unavailable")
            query.assert_not_called()
