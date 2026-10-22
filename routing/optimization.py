"""Join read-only station searching, provider calls, and pure fuel planning."""

from decimal import Decimal
from time import perf_counter

from django.conf import settings
from django.db import DatabaseError

from fuel.optimization import DetourStation, optimize_fuel_stops
from fuel.planning import InfeasibleFuelPlan
from routing.addresses import AddressResolver
from routing.caching import CachedRouter
from routing.detours import evaluate_detours
from routing.services import Coordinate, OpenRouteService, RoutingError
from stations.candidates import find_candidates


def optimize_trip(options):
    started = perf_counter()
    timings = {}
    resolver = AddressResolver(settings.ORS_API_KEY, settings.ORS_TIMEOUT_SECONDS)
    endpoints = {}
    for name in ("start", "destination"):
        if name in options:
            point = options[name]
            endpoints[name] = {"longitude": point.longitude, "latitude": point.latitude}
        else:
            endpoints[name] = resolver.resolve(options[name + "_address"])
    start, destination = (Coordinate(endpoints[name]["longitude"], endpoints[name]["latitude"])
                          for name in ("start", "destination"))
    if start == destination:
        raise RoutingError("identical_locations", "Start and destination resolve to the same location.", 400)
    timings["address_resolution_elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
    router = CachedRouter(OpenRouteService(settings.ORS_API_KEY, settings.ORS_TIMEOUT_SECONDS))
    phase_started = perf_counter()
    route = router.main_route(start, destination)
    timings["main_route_elapsed_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    main_miles = Decimal(str(route.distance_miles))
    candidates, detours, usable = None, None, []
    # A full starting tank covers short trips without any station or matrix calls.
    if main_miles > 500:
        if not settings.USE_POSTGIS:
            raise RoutingError("candidate_search_unavailable", "Long-trip station search requires PostgreSQL/PostGIS.", 503)
        try:
            phase_started = perf_counter()
            candidates = find_candidates(route, **options.get("candidate_search", {}))
        except DatabaseError:
            raise RoutingError("candidate_search_unavailable", "The station search database is unavailable.", 503) from None
        timings["candidate_search_elapsed_ms"] = round((perf_counter() - phase_started) * 1000, 2)
        phase_started = perf_counter()
        detours = evaluate_detours(router, route, candidates["stations"], **options.get("detour_evaluation", {}))
        timings["detour_evaluation_elapsed_ms"] = round((perf_counter() - phase_started) * 1000, 2)
        usable = [row for row in detours["stations"] if row["status"] == "evaluated"]
    stations = [DetourStation(
        row["station_id"], row["anchor"]["route_position_miles_estimate"],
        row["access_miles"], row["return_miles"], row["price_per_gallon"],
    ) for row in usable]
    try:
        phase_started = perf_counter()
        plan = optimize_fuel_stops(main_miles, stations)
    except InfeasibleFuelPlan:
        raise RoutingError("fuel_plan_not_found",
                           "No feasible fuel-stop sequence was established from the evaluated shortlist. "
                           "Incomplete geocoding, shortlist limits, or unusable detours may omit needed stations.", 422) from None
    timings["fuel_optimization_elapsed_ms"] = round((perf_counter() - phase_started) * 1000, 2)
    by_id = {row["station_id"]: row for row in usable}
    coordinates = {row["station_id"]: row for row in candidates["stations"]} if candidates else {}
    stops, legs = [], []
    previous_position, previous_return, previous_id = Decimal(0), Decimal(0), "start"
    for purchase in plan.purchases:
        row, location = by_id[purchase.station_id], coordinates[purchase.station_id]
        station = next(s for s in stations if s.station_id == purchase.station_id)
        leg_miles = previous_return + station.position_miles - previous_position + station.access_miles
        legs.append({"from": previous_id, "to": station.station_id,
                     "distance_miles_estimate": float(leg_miles), "gallons_consumed_estimate": float(leg_miles / 10)})
        stops.append({
            **{key: float(value) if isinstance(value, Decimal) else value for key, value in vars(purchase).items()},
            "name": row["name"], "longitude": location["longitude"], "latitude": location["latitude"],
            "access_miles": float(station.access_miles), "return_miles": float(station.return_miles),
            "detour_miles": float(station.access_miles + station.return_miles), "anchor": row["anchor"],
        })
        previous_position, previous_return, previous_id = station.position_miles, station.return_miles, station.station_id
    final_miles = previous_return + main_miles - previous_position
    legs.append({"from": previous_id, "to": "destination", "distance_miles_estimate": float(final_miles),
                 "gallons_consumed_estimate": float(final_miles / 10)})
    metrics = {**router.metrics, **timings, "routing_api_calls": router.metrics["directions_api_calls"] + router.metrics["matrix_api_calls"],
               "geocoding_api_calls": resolver.api_calls, "address_cache_hits": resolver.cache_hits,
               "elapsed_ms": round((perf_counter() - started) * 1000, 2)}
    return {
        **endpoints,
        "route": {**route.as_dict(), "maximum_range_miles": 500, "fuel_economy_mpg": 10,
                  "tank_capacity_gallons": 50, "geometry_includes_detours": False},
        "fuel_stops": stops,
        "summary": {
            "main_route_miles": float(main_miles), "detour_miles": float(plan.total_distance_miles - main_miles),
            "total_distance_miles": float(plan.total_distance_miles),
            "total_gallons_consumed": float(plan.total_gallons_consumed),
            "total_gallons_purchased": float(plan.total_gallons_purchased),
            "starting_fuel_gallons": float(plan.starting_fuel_gallons), "ending_fuel_gallons": float(plan.ending_fuel_gallons),
            "total_fuel_cost": float(plan.total_fuel_cost), "number_of_stops": len(stops),
        },
        "pricing_assumption": "Minimum valid supplied price per station; static demo USD per US gallon, not verified as current",
        "optimization": {
            "method": "Dynamic programming over forward station excursions and critical departure fuel levels",
            "global_optimality": False,
            "distance_model": "Estimated main-route splits plus independently evaluated access and same-anchor return road distances",
            "currency_rounding": "Minimize unrounded expenditure; round each purchase HALF_UP to cents and sum those costs",
            "initial_fuel_cost_included": False, "reserve_gallons": 0,
            "candidate_matched_count": candidates.get("matched_count", len(candidates["stations"])) if candidates else 0,
            "candidate_shortlisted_count": len(candidates["stations"]) if candidates else 0,
            "candidate_omitted_count": max(0, candidates.get("matched_count", len(candidates["stations"]))
                                             - len(candidates["stations"])) if candidates else 0,
            "detours_evaluated_count": detours["evaluated_count"] if detours else 0,
            "detours_omitted_count": detours["omitted_count"] if detours else 0,
            "detours_rejected_count": detours["evaluated_count"] - len(usable) if detours else 0,
            "usable_detours_count": len(usable),
            "detour_attribution": detours.get("attribution") if detours else None,
            "candidate_search_skipped": main_miles <= 500,
        },
        "legs": legs,
        "limitations": [
            "Only successfully geocoded, unambiguous stations in the bounded shortlist are considered; coverage may be incomplete.",
            "Intermediate main-route distances and fuel balances are estimates, not verified exact range guarantees.",
            "Each selected station leaves and rejoins at the same anchor; access and return are counted once each.",
            "Stations follow anchor order, with station ID breaking ties; alternative road routes and repeated visits are not searched.",
            "Map geometry and duration describe the main route only; detour shapes and forecourt access are not available.",
        ],
        "metrics": metrics,
    }
