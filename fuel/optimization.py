"""Choose forward station excursions and partial purchases on an estimated route."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from fuel.planning import (
    CENT, ZERO, FuelPlan, FuelPlanningError, FuelPurchase, InfeasibleFuelPlan,
    decimal_value,
)


@dataclass(frozen=True)
class DetourStation:
    station_id: str
    position_miles: Decimal
    access_miles: Decimal
    return_miles: Decimal
    price_per_gallon: Decimal

    def __post_init__(self):
        if not isinstance(self.station_id, str) or not self.station_id.strip():
            raise FuelPlanningError("Station IDs must be nonempty strings")
        for name in ("position_miles", "access_miles", "return_miles", "price_per_gallon"):
            value = decimal_value(getattr(self, name), name)
            if value < 0 or (name == "price_per_gallon" and value == 0):
                raise FuelPlanningError("Station distances must be nonnegative and prices positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class Label:
    cost: Decimal
    miles: Decimal
    purchases: tuple[FuelPurchase, ...]

    @property
    def rank(self):
        # Ties prefer less driving, then fewer stops, then stable station IDs.
        return self.cost, self.miles, len(self.purchases), tuple(p.station_id for p in self.purchases)


def optimize_fuel_stops(main_route_miles, stations, *, initial_fuel_gallons=50):
    """Minimize unrounded purchase cost on the supplied forward detour graph.

    A useful departure tank is full or just sufficient to reach a later purchase
    station/destination. Otherwise fuel can be shifted between adjacent purchases
    until a tank is full or an arrival is empty, without increasing cost. These
    critical levels avoid discretizing fuel into whole gallons. Unselected
    stations add no mileage. This is not a global road-network optimization.
    """
    distance = decimal_value(main_route_miles, "main route distance")
    initial = decimal_value(initial_fuel_gallons, "initial fuel")
    mpg, capacity = Decimal(10), Decimal(50)
    if distance < 0 or not ZERO <= initial <= capacity:
        raise FuelPlanningError("Invalid route distance or starting fuel")
    if len({s.station_id for s in stations}) != len(stations):
        raise FuelPlanningError("Station IDs must be unique")
    if any(s.position_miles > distance for s in stations):
        raise FuelPlanningError("Station anchor cannot be beyond the destination")
    ordered = sorted(stations, key=lambda s: (s.position_miles, s.station_id))
    start = DetourStation("start", ZERO, ZERO, ZERO, Decimal(1))
    destination = DetourStation("destination", distance, ZERO, ZERO, Decimal(1))
    nodes = [start, *ordered, destination]

    def leg(i, j):
        return nodes[i].return_miles + nodes[j].position_miles - nodes[i].position_miles + nodes[j].access_miles

    # Each node retains the cheapest path for each useful departure fuel level.
    labels = [{}, *({} for _ in ordered), {}]
    labels[0][initial] = Label(ZERO, ZERO, ())
    best = None
    ending_fuel = None
    for i in range(len(nodes) - 1):
        for j in range(i + 1, len(nodes)):
            miles = leg(i, j)
            needed = miles / mpg
            if needed > capacity:
                continue
            targets = {capacity}
            if j < len(nodes) - 1:
                targets.update(leg(j, k) / mpg for k in range(j + 1, len(nodes)) if leg(j, k) <= capacity * mpg)
            for fuel, previous in labels[i].items():
                if needed > fuel:
                    continue
                arrival = fuel - needed
                driven = previous.miles + miles
                if j == len(nodes) - 1:
                    finished = Label(previous.cost, driven, previous.purchases)
                    if best is None or finished.rank < best.rank:
                        best, ending_fuel = finished, arrival
                    continue
                for departure in targets:
                    quantity = departure - arrival
                    # A visit without a purchase is always shortcut by another edge.
                    if quantity <= 0:
                        continue
                    raw_cost = quantity * nodes[j].price_per_gallon
                    purchase = FuelPurchase(
                        nodes[j].station_id, driven, nodes[j].price_per_gallon,
                        arrival, quantity, departure, departure * mpg,
                        raw_cost.quantize(CENT, rounding=ROUND_HALF_UP),
                    )
                    label = Label(previous.cost + raw_cost, driven, (*previous.purchases, purchase))
                    incumbent = labels[j].get(departure)
                    if incumbent is None or label.rank < incumbent.rank:
                        labels[j][departure] = label
    if best is None:
        raise InfeasibleFuelPlan("No feasible sequence was established from the evaluated station shortlist")
    return FuelPlan(
        best.purchases, best.miles, mpg, capacity, initial, ending_fuel,
        best.miles / mpg,
        sum((p.gallons_purchased for p in best.purchases), ZERO),
        sum((p.fuel_cost for p in best.purchases), ZERO).quantize(CENT),
    )
