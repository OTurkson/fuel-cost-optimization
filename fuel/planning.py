"""Greedy partial refueling on a fixed itinerary with supplied driving distances.

All station positions must be cumulative driving miles on the SAME itinerary,
including any detours already selected by the caller. Nearest-route projections
are not suitable inputs. This module does not choose alternative road detours.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


ZERO = Decimal("0")
CENT = Decimal("0.01")


class FuelPlanningError(ValueError):
    pass


class InfeasibleFuelPlan(FuelPlanningError):
    pass


def decimal_value(value, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise FuelPlanningError(f"{name} must be a finite number") from None
    if not parsed.is_finite():
        raise FuelPlanningError(f"{name} must be a finite number")
    return parsed


@dataclass(frozen=True)
class ItineraryStation:
    station_id: str
    distance_miles: Decimal
    price_per_gallon: Decimal

    def __post_init__(self):
        distance = decimal_value(self.distance_miles, "station distance")
        price = decimal_value(self.price_per_gallon, "station price")
        if not isinstance(self.station_id, str) or not self.station_id.strip():
            raise FuelPlanningError("station_id must be a nonempty string")
        if distance < 0 or price <= 0:
            raise FuelPlanningError("Station distance must be nonnegative and price must be positive")
        object.__setattr__(self, "distance_miles", distance)
        object.__setattr__(self, "price_per_gallon", price)


@dataclass(frozen=True)
class FuelPurchase:
    station_id: str
    distance_miles: Decimal
    price_per_gallon: Decimal
    arrival_fuel_gallons: Decimal
    gallons_purchased: Decimal
    departure_fuel_gallons: Decimal
    remaining_range_miles: Decimal
    fuel_cost: Decimal

    def as_dict(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in vars(self).items()}


@dataclass(frozen=True)
class FuelPlan:
    purchases: tuple[FuelPurchase, ...]
    total_distance_miles: Decimal
    fuel_economy_mpg: Decimal
    tank_capacity_gallons: Decimal
    starting_fuel_gallons: Decimal
    ending_fuel_gallons: Decimal
    total_gallons_consumed: Decimal
    total_gallons_purchased: Decimal
    total_fuel_cost: Decimal

    def as_dict(self) -> dict:
        result = {key: str(value) for key, value in vars(self).items() if key != "purchases"}
        result["fuel_stops"] = [purchase.as_dict() for purchase in self.purchases]
        result["number_of_stops"] = len(self.purchases)
        result["distance_model"] = "Caller-supplied cumulative driving miles on a fixed itinerary"
        result["strategy"] = "Partial refueling toward the next reachable cheaper-or-equal-price station"
        result["currency_rounding"] = "Each purchase rounded to cents with ROUND_HALF_UP; total sums rounded purchases"
        return result


def plan_fuel_trip(
    total_distance_miles,
    stations: list[ItineraryStation],
    *,
    initial_fuel_gallons=Decimal("50"),
) -> FuelPlan:
    """Use the agreed 10 MPG, 50-gallon vehicle; initial fuel is already paid.

    The destination behaves as a zero-price target. With no cheaper station in
    range, buy up to full capacity, limited to what is needed to finish. A visit
    with zero purchase is omitted from the stop list. No reserve is imposed.
    """
    distance = decimal_value(total_distance_miles, "total distance")
    initial = decimal_value(initial_fuel_gallons, "initial fuel")
    mpg, capacity = Decimal("10"), Decimal("50")
    if distance < 0 or not ZERO <= initial <= capacity:
        raise FuelPlanningError("Distance must be nonnegative and initial fuel must be between 0 and 50 gallons")
    if len({station.station_id for station in stations}) != len(stations):
        raise FuelPlanningError("Station IDs must be unique within the fixed itinerary")
    if any(station.distance_miles > distance for station in stations):
        raise FuelPlanningError("Station position cannot be beyond the destination")
    ordered = sorted(
        (station for station in stations if station.distance_miles < distance),
        key=lambda station: (station.distance_miles, station.price_per_gallon, station.station_id),
    )
    fuel = initial
    position = ZERO
    purchases = []
    for index, station in enumerate(ordered):
        needed = (station.distance_miles - position) / mpg
        if needed > fuel:
            raise InfeasibleFuelPlan(f"Insufficient fuel to reach station {station.station_id} on the supplied itinerary")
        fuel -= needed
        position = station.distance_miles
        # First cheaper-or-equal station is sufficient: any more expensive
        # intervening fuel can be avoided by carrying fuel from here.
        target_fuel = min(capacity, (distance - position) / mpg)
        for following in ordered[index + 1:]:
            gallons_to_following = (following.distance_miles - position) / mpg
            if gallons_to_following > capacity:
                break
            if following.price_per_gallon <= station.price_per_gallon:
                target_fuel = gallons_to_following
                break
        quantity = max(ZERO, target_fuel - fuel)
        if quantity > 0:
            departure = fuel + quantity
            purchases.append(FuelPurchase(
                station_id=station.station_id, distance_miles=position,
                price_per_gallon=station.price_per_gallon,
                arrival_fuel_gallons=fuel, gallons_purchased=quantity,
                departure_fuel_gallons=departure,
                remaining_range_miles=departure * mpg,
                fuel_cost=(quantity * station.price_per_gallon).quantize(CENT, rounding=ROUND_HALF_UP),
            ))
            fuel = departure
    final_needed = (distance - position) / mpg
    if final_needed > fuel:
        raise InfeasibleFuelPlan("Insufficient fuel to reach the destination on the supplied itinerary")
    fuel -= final_needed
    return FuelPlan(
        purchases=tuple(purchases), total_distance_miles=distance,
        fuel_economy_mpg=mpg, tank_capacity_gallons=capacity,
        starting_fuel_gallons=initial, ending_fuel_gallons=fuel,
        total_gallons_consumed=distance / mpg,
        total_gallons_purchased=sum((purchase.gallons_purchased for purchase in purchases), ZERO),
        total_fuel_cost=sum((purchase.fuel_cost for purchase in purchases), ZERO).quantize(CENT),
    )
