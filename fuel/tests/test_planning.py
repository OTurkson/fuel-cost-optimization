from decimal import Decimal as D
import random

from django.test import SimpleTestCase

from fuel.planning import FuelPlanningError, InfeasibleFuelPlan, ItineraryStation, plan_fuel_trip


def station(pk, miles, price):
    return ItineraryStation(pk, D(str(miles)), D(str(price)))


class FuelPlanningTests(SimpleTestCase):
    def assert_accounting(self, plan):
        self.assertEqual(
            plan.starting_fuel_gallons + plan.total_gallons_purchased,
            plan.total_gallons_consumed + plan.ending_fuel_gallons,
        )
        self.assertEqual(plan.total_fuel_cost, sum((stop.fuel_cost for stop in plan.purchases), D("0")))
        for stop in plan.purchases:
            self.assertGreaterEqual(stop.arrival_fuel_gallons, 0)
            self.assertLessEqual(stop.departure_fuel_gallons, 50)
            self.assertEqual(stop.arrival_fuel_gallons + stop.gallons_purchased, stop.departure_fuel_gallons)

    def test_short_trip_uses_free_initial_fuel(self):
        plan = plan_fuel_trip(200, [station("a", 100, 2)])
        self.assertEqual(plan.purchases, ())
        self.assertEqual(plan.total_gallons_consumed, 20)
        self.assertEqual(plan.ending_fuel_gallons, 30)
        self.assertEqual(plan.total_fuel_cost, 0)
        self.assert_accounting(plan)

    def test_skip_nearer_expensive_station_when_cheaper_is_reachable(self):
        plan = plan_fuel_trip(200, [station("expensive", 40, 5), station("cheap", 120, 3)], initial_fuel_gallons=15)
        self.assertEqual([stop.station_id for stop in plan.purchases], ["cheap"])
        purchase = plan.purchases[0]
        self.assertEqual(purchase.arrival_fuel_gallons, 3)
        self.assertEqual(purchase.gallons_purchased, 5)
        self.assertEqual(plan.total_fuel_cost, 15)
        self.assertEqual(plan.ending_fuel_gallons, 0)
        self.assert_accounting(plan)

    def test_partial_purchase_to_reach_cheaper_station(self):
        plan = plan_fuel_trip(400, [station("expensive", 50, 5), station("cheap", 200, 3)], initial_fuel_gallons=10)
        self.assertEqual([stop.gallons_purchased for stop in plan.purchases], [D("10"), D("20")])
        self.assertEqual(plan.total_fuel_cost, 110)
        self.assert_accounting(plan)

    def test_late_purchase_only_buys_fuel_needed_to_finish(self):
        plan = plan_fuel_trip(700, [station("a", 250, 3), station("b", 490, 2)])
        self.assertEqual([stop.station_id for stop in plan.purchases], ["b"])
        self.assertEqual(plan.purchases[0].gallons_purchased, 20)
        self.assertEqual(plan.total_fuel_cost, 40)
        self.assert_accounting(plan)

    def test_multiple_stops_and_fill_when_no_cheaper_station_is_reachable(self):
        plan = plan_fuel_trip(1400, [station("a", 400, 2), station("b", 800, 4), station("c", 1200, 3)])
        self.assertEqual([stop.gallons_purchased for stop in plan.purchases], [D("40"), D("30"), D("20")])
        self.assertEqual(plan.purchases[0].departure_fuel_gallons, 50)
        self.assertEqual(plan.total_fuel_cost, 260)
        self.assert_accounting(plan)

    def test_unreachable_cheaper_station_does_not_replace_necessary_stop(self):
        plan = plan_fuel_trip(700, [station("expensive", 100, 5), station("cheap", 600, 1)], initial_fuel_gallons=10)
        self.assertEqual(plan.purchases[0].station_id, "expensive")
        self.assertEqual(plan.purchases[0].gallons_purchased, 50)
        self.assertEqual(plan.total_fuel_cost, 260)
        self.assert_accounting(plan)

    def test_exact_range_and_zero_trip_need_no_purchase(self):
        for distance in (0, 500):
            plan = plan_fuel_trip(distance, [])
            self.assertEqual(plan.total_fuel_cost, 0)
            self.assert_accounting(plan)

    def test_infeasible_gaps_are_reported(self):
        for distance, options in ((501, []), (1100, [station("a", 400, 3)]), (900, [station("late", 600, 1)])):
            with self.subTest(distance=distance), self.assertRaises(InfeasibleFuelPlan):
                plan_fuel_trip(distance, options)

    def test_supplied_detour_driving_miles_affect_feasibility(self):
        self.assertEqual(len(plan_fuel_trip(700, [station("a", 490, 3)]).purchases), 1)
        # Caller has incorporated a 20-mile access journey into station position.
        with self.assertRaises(InfeasibleFuelPlan):
            plan_fuel_trip(720, [station("a", 510, 3)])

    def test_currency_rounding_and_destination_station(self):
        plan = plan_fuel_trip("500.01", [station("a", 400, "3.335"), station("destination", "500.01", 1)])
        self.assertEqual(plan.purchases[0].gallons_purchased, D("0.001"))
        self.assertEqual(plan.total_fuel_cost, D("0.00"))
        self.assertEqual(len(plan.purchases), 1)
        self.assert_accounting(plan)
        self.assertEqual(plan.as_dict()["number_of_stops"], 1)

    def test_invalid_inputs_and_duplicate_ids_are_rejected(self):
        for value in ("NaN", "Infinity", -1, True):
            with self.subTest(value=value), self.assertRaises(FuelPlanningError):
                plan_fuel_trip(value, [])
        for value in (-1, 51):
            with self.subTest(initial=value), self.assertRaises(FuelPlanningError):
                plan_fuel_trip(200, [], initial_fuel_gallons=value)
        with self.assertRaises(FuelPlanningError):
            plan_fuel_trip(200, [station("a", 100, 3), station("a", 150, 2)])
        with self.assertRaises(FuelPlanningError):
            plan_fuel_trip(200, [station("a", 300, 3)])

    def test_greedy_cost_matches_exhaustive_fuel_states_on_small_fixed_itineraries(self):
        rng = random.Random(2026)
        for _ in range(40):
            destination = rng.randint(30, 120)  # Units of ten driving miles / one gallon.
            positions = sorted(rng.sample(range(1, destination), 5))
            prices = [rng.randint(1, 8) for _ in positions]
            initial = rng.randint(10, 50)
            # Independent reference: enumerate every integer-gallon purchase
            # quantity at each station, retaining minimum cost per fuel state.
            states = {initial: 0}
            previous = 0
            for position, price in zip(positions, prices):
                next_states = {}
                for remaining, cost in states.items():
                    arrival = remaining - (position - previous)
                    if arrival < 0:
                        continue
                    for purchase in range(51 - arrival):
                        departure = arrival + purchase
                        value = cost + purchase * price
                        next_states[departure] = min(next_states.get(departure, float("inf")), value)
                states = next_states
                previous = position
            feasible_costs = [cost for remaining, cost in states.items() if remaining >= destination - previous]
            options = [station(str(index), position * 10, price) for index, (position, price) in enumerate(zip(positions, prices))]
            if feasible_costs:
                plan = plan_fuel_trip(destination * 10, options, initial_fuel_gallons=initial)
                self.assertEqual(plan.total_fuel_cost, min(feasible_costs))
                self.assert_accounting(plan)
            else:
                with self.assertRaises(InfeasibleFuelPlan):
                    plan_fuel_trip(destination * 10, options, initial_fuel_gallons=initial)
