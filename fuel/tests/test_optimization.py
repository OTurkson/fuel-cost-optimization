from decimal import Decimal
from functools import lru_cache
from itertools import combinations
import random

from django.test import SimpleTestCase

from fuel.optimization import DetourStation, optimize_fuel_stops
from fuel.planning import FuelPlanningError, InfeasibleFuelPlan, ItineraryStation, plan_fuel_trip


def station(pk, position, price, access=0, returning=0):
    return DetourStation(pk, position, access, returning, price)


class OptimizationTests(SimpleTestCase):
    def balance(self, plan):
        self.assertEqual(plan.starting_fuel_gallons + plan.total_gallons_purchased,
                         plan.total_gallons_consumed + plan.ending_fuel_gallons)
        self.assertEqual(sum(p.fuel_cost for p in plan.purchases), plan.total_fuel_cost)
        for purchase in plan.purchases:
            self.assertGreater(purchase.gallons_purchased, 0)
            self.assertGreaterEqual(purchase.arrival_fuel_gallons, 0)
            self.assertLessEqual(purchase.departure_fuel_gallons, 50)

    def test_short_trip_uses_only_starting_fuel(self):
        plan = optimize_fuel_stops(400, [station("a", 100, 1, 5, 5)])
        self.assertEqual(plan.purchases, ())
        self.assertEqual(plan.ending_fuel_gallons, 10)
        self.assertEqual(plan.total_fuel_cost, 0)
        self.balance(plan)

    def test_skip_near_expensive_station_when_cheaper_is_reachable(self):
        plan = optimize_fuel_stops(700, [station("expensive", 200, 5), station("cheap", 400, 2)])
        self.assertEqual([p.station_id for p in plan.purchases], ["cheap"])
        self.assertEqual(plan.purchases[0].arrival_fuel_gallons, 10)
        self.assertEqual(plan.purchases[0].gallons_purchased, 20)
        self.assertEqual(plan.total_fuel_cost, 40)
        self.balance(plan)

    def test_partial_purchase_to_reach_cheaper_station(self):
        plan = optimize_fuel_stops(900, [station("a", 400, 5), station("b", 600, 2)])
        self.assertEqual([p.gallons_purchased for p in plan.purchases], [10, 30])
        self.assertEqual(plan.total_fuel_cost, 110)
        self.balance(plan)

    def test_detour_can_reverse_price_preference(self):
        plan = optimize_fuel_stops(700, [station("near", 400, 3), station("cheap", 400, 2.5, 50, 50)])
        self.assertEqual([p.station_id for p in plan.purchases], ["near"])
        self.assertEqual(plan.total_fuel_cost, 60)
        self.assertEqual(plan.total_distance_miles, 700)

    def test_asymmetric_detours_are_counted_once_and_can_break_reachability(self):
        plan = optimize_fuel_stops(900, [station("a", 400, 3, 3, 7), station("b", 700, 2, 4, 6)])
        self.assertEqual(plan.total_distance_miles, 920)
        self.assertEqual([p.distance_miles for p in plan.purchases], [403, 714])
        self.balance(plan)
        with self.assertRaises(InfeasibleFuelPlan):
            optimize_fuel_stops(700, [station("a", 490, 1, 20, 20)])

    def test_invalid_inputs(self):
        for mileage, stations in ((-1, []), (800, [station("a", 900, 2)]),
                                  (800, [station("a", 100, 2), station("a", 200, 2)])):
            with self.assertRaises(FuelPlanningError):
                optimize_fuel_stops(mileage, stations)
        with self.assertRaises(FuelPlanningError):
            station("a", 50, 0)

    def test_fractional_distances_match_exhaustive_itinerary_search(self):
        rng = random.Random(902)
        for case in range(40):
            distance = Decimal(rng.randrange(60000, 130000)) / 100
            points = sorted(rng.sample(range(10000, int(distance * 100)), 6))
            stations = [station(str(i), Decimal(point) / 100, Decimal(rng.randrange(2000, 6000)) / 1000,
                                Decimal(rng.randrange(1000)) / 100, Decimal(rng.randrange(1000)) / 100)
                        for i, point in enumerate(points)]
            costs = []
            for count in range(len(stations) + 1):
                for selected in combinations(stations, count):
                    miles, previous, returning = Decimal(0), Decimal(0), Decimal(0)
                    fixed = []
                    for stop in selected:
                        miles += returning + stop.position_miles - previous + stop.access_miles
                        fixed.append(ItineraryStation(stop.station_id, miles, stop.price_per_gallon))
                        previous, returning = stop.position_miles, stop.return_miles
                    miles += returning + distance - previous
                    try:
                        fixed_plan = plan_fuel_trip(miles, fixed)
                    except InfeasibleFuelPlan:
                        continue
                    costs.append(sum(p.gallons_purchased * p.price_per_gallon for p in fixed_plan.purchases))
            with self.subTest(case=case):
                if not costs:
                    with self.assertRaises(InfeasibleFuelPlan):
                        optimize_fuel_stops(distance, stations)
                else:
                    plan = optimize_fuel_stops(distance, stations)
                    self.assertEqual(sum(p.gallons_purchased * p.price_per_gallon for p in plan.purchases), min(costs))
                    self.balance(plan)

    def test_matches_exhaustive_integer_fuel_graph_oracle(self):
        # Every edge uses whole gallons here. Enumerating every legal integer
        # purchase independently checks both station selection and fuel amounts.
        rng = random.Random(781)
        for case in range(80):
            distance = rng.randrange(55, 151) * 10
            positions = sorted(rng.sample(range(5, distance // 10), 4))
            stations = [station(str(i), p * 10, rng.randrange(1, 7), rng.randrange(3) * 10,
                                rng.randrange(3) * 10) for i, p in enumerate(positions)]
            nodes = [station("start", 0, 1), *stations, station("end", distance, 1)]

            @lru_cache(None)
            def brute(index, fuel):
                if index == len(nodes) - 1:
                    return 0
                best = float("inf")
                for following in range(index + 1, len(nodes)):
                    needed = int((nodes[index].return_miles + nodes[following].position_miles
                                  - nodes[index].position_miles + nodes[following].access_miles) / 10)
                    if needed > fuel:
                        continue
                    arrival = fuel - needed
                    if following == len(nodes) - 1:
                        best = min(best, 0)
                    else:
                        for bought in range(1, 51 - arrival):
                            best = min(best, bought * int(nodes[following].price_per_gallon)
                                       + brute(following, arrival + bought))
                return best

            expected = brute(0, 50)
            with self.subTest(case=case):
                if expected == float("inf"):
                    with self.assertRaises(InfeasibleFuelPlan):
                        optimize_fuel_stops(distance, stations)
                else:
                    plan = optimize_fuel_stops(distance, stations)
                    self.assertEqual(plan.total_fuel_cost, Decimal(expected))
                    self.balance(plan)
