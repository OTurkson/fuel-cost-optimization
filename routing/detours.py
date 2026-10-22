"""One matrix evaluates independent station access and return-to-anchor driving.

Each selected station excursion leaves and rejoins the main route at the SAME
route vertex. This preserves the main route and gives additive, nonnegative
detours. It does not find optimal entry/rejoin points or detour geometry.
"""

from __future__ import annotations

from decimal import Decimal

from routing.services import Coordinate, METERS_PER_MILE, MainRoute, OpenRouteService, RoutingError, geographic_meters
from stations.candidates import StationCandidate, shortlist_candidates


MAX_DETOUR_STATIONS = 29
MAX_SNAPPING_METERS = 100


def route_anchors(route: MainRoute, stations: list[StationCandidate]) -> list[dict]:
    points = [Coordinate(*point) for point in route.geometry["coordinates"]]
    # Shape length gives an approximate position along the full driving route.
    cumulative = [0.0]
    for previous, point in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + geographic_meters(previous, point))
    if cumulative[-1] <= 0:
        raise RoutingError("invalid_routing_response", "The route shape has zero length.")
    anchors = []
    for station in stations:
        location = Coordinate(station.longitude, station.latitude)
        # Pick an existing route vertex as the departure and return point.
        index = min(range(len(points)), key=lambda index: geographic_meters(points[index], location))
        anchors.append({
            "coordinate": points[index], "vertex_index": index,
            "route_position_miles_estimate": cumulative[index] / cumulative[-1] * route.distance_miles,
        })
    return anchors


def evaluate_detours(
    router: OpenRouteService, route: MainRoute, candidates: list[dict], limit: int = 20,
) -> dict:
    if type(limit) is not int or not 1 <= limit <= MAX_DETOUR_STATIONS:
        raise RoutingError("invalid_detour_request", "Detour limit must be between 1 and 29 stations.", 400)
    stations = [StationCandidate(
        station_id=row["station_id"], name=row["name"],
        longitude=row["longitude"], latitude=row["latitude"],
        price_per_gallon=Decimal(row["price_per_gallon"]),
        route_position_miles=row["route_position_miles"],
        distance_to_route_miles=row["distance_to_route_miles"],
    ) for row in candidates]
    # Keep the second shortlist small enough for one matrix request.
    selected = shortlist_candidates(stations, route.distance_miles, limit)
    result = {
        "model": "Leave main route at a selected vertex, visit station, return to the same anchor",
        "candidate_count": len(stations), "evaluated_count": len(selected),
        "omitted_count": len(stations) - len(selected), "limit": limit,
        "matrix_api_calls": 0, "matrix_location_count": 0, "matrix_pair_count": 0,
        "stations": [], "usable_count": 0, "max_snapping_meters": MAX_SNAPPING_METERS,
        "fuel_feasibility_evaluated": False,
        "limitations": [
            "The nearest route vertex is an anchor choice, not an optimized exit/rejoin pair.",
            "Provider distances end at snapped road locations; station forecourt access is not verified.",
            "Anchor positions along the route are estimates scaled from shape length; intermediate main-route driving splits remain estimated.",
            "Matrix results have no detour geometry, and do not constrain road approach direction at the anchor.",
            "Null routes or excessive snapping are unusable for this model; they do not prove a trip globally infeasible.",
        ],
    }
    # An empty shortlist should not spend an extra provider request.
    if not selected:
        return result
    anchors = route_anchors(route, selected)
    # Matrix indices put all anchors first, then their stations in the same order.
    locations = [anchor["coordinate"] for anchor in anchors]
    locations.extend(Coordinate(station.longitude, station.latitude) for station in selected)
    matrix = router.distance_matrix(locations)
    count = len(selected)
    result.update(
        matrix_api_calls=1, matrix_location_count=len(locations),
        matrix_pair_count=len(locations) ** 2, attribution=matrix.attribution,
    )
    for index, (station, anchor) in enumerate(zip(selected, anchors)):
        station_index = count + index
        record = {
            "station_id": station.station_id, "name": station.name,
            "price_per_gallon": str(station.price_per_gallon),
            "anchor": {
                "longitude": anchor["coordinate"].longitude,
                "latitude": anchor["coordinate"].latitude,
                "vertex_index": anchor["vertex_index"],
                "route_position_miles_estimate": anchor["route_position_miles_estimate"],
            },
        }
        # Read each direction separately; one-way roads can make them different.
        access = matrix.distances_meters[index][station_index]
        returning = matrix.distances_meters[station_index][index]
        snaps = [matrix.sources[index], matrix.destinations[index],
                 matrix.sources[station_index], matrix.destinations[station_index]]
        # A road snap or missing route can make this excursion unusable.
        if access is None or returning is None or any(snap is None for snap in snaps):
            record.update(status="unrouted", reason="Access or return route could not be established")
        elif any(snap.snapped_distance_meters > MAX_SNAPPING_METERS for snap in snaps):
            record.update(status="snapping_rejected", reason="An anchor or station snapped more than 100 metres from its coordinate")
        elif matrix.sources[index].location != matrix.destinations[index].location or matrix.sources[station_index].location != matrix.destinations[station_index].location:
            record.update(status="snapping_rejected", reason="Source and destination snaps do not agree on the same road location")
        else:
            # Returning to the same anchor makes both legs extra trip mileage.
            detour_miles = (access + returning) / METERS_PER_MILE
            record.update(
                status="evaluated", access_miles=access / METERS_PER_MILE,
                return_miles=returning / METERS_PER_MILE, detour_miles=detour_miles,
                detour_gallons_consumed=detour_miles / 10,
                anchor_snapped_distance_meters=max(snaps[0].snapped_distance_meters, snaps[1].snapped_distance_meters),
                station_snapped_distance_meters=max(snaps[2].snapped_distance_meters, snaps[3].snapped_distance_meters),
                snapped_anchor=matrix.sources[index].location.as_list(),
                snapped_station=matrix.sources[station_index].location.as_list(),
                snapping_distance_method=(
                    "Geographic separation from requested to snapped coordinate (quality check only)"
                    if any(snap.distance_method != "provider_reported" for snap in snaps)
                    else "Provider-reported distance, checked against coordinate separation"
                ),
            )
        result["stations"].append(record)
    result["stations"].sort(key=lambda station: (station["anchor"]["route_position_miles_estimate"], station["station_id"]))
    result["usable_count"] = sum(station["status"] == "evaluated" for station in result["stations"])
    return result


def estimate_itinerary_legs(main_route_miles: float, selected_detours: list[dict]) -> list[dict]:
    """Count each selected access/return once; main-route splits are estimates.

    This helper prepares distance accounting for future itinerary selection. It
    must not be described as exact driving legs or an end-to-end fuel solution.
    """
    from routing.services import finite_number

    if not finite_number(main_route_miles) or main_route_miles <= 0:
        raise RoutingError("invalid_itinerary", "Main-route distance must be positive.", 400)
    if len({station["station_id"] for station in selected_detours}) != len(selected_detours):
        raise RoutingError("invalid_itinerary", "A detour station cannot be counted twice.", 400)
    previous_position, previous_return, previous_id = 0.0, 0.0, "start"
    legs = []
    for station in selected_detours:
        if station["status"] != "evaluated":
            raise RoutingError("invalid_itinerary", "Only evaluated detours can be used in an itinerary.", 400)
        position = station["anchor"]["route_position_miles_estimate"]
        access, returning = station["access_miles"], station["return_miles"]
        if (
            not all(finite_number(value) and value >= 0 for value in (position, access, returning))
            or not previous_position <= position <= main_route_miles
        ):
            raise RoutingError("invalid_itinerary", "Detours must have nonnegative distances and forward-ordered anchors.", 400)
        # Leave the previous station, follow the main route, then enter this one.
        legs.append({
            "from": previous_id, "to": station["station_id"],
            "main_route_miles_estimate": position - previous_position,
            "return_miles": previous_return, "access_miles": access,
            "distance_miles_estimate": previous_return + position - previous_position + access,
        })
        previous_position, previous_return, previous_id = position, returning, station["station_id"]
    # Include the last station's return journey before heading to the destination.
    legs.append({
        "from": previous_id, "to": "destination",
        "main_route_miles_estimate": main_route_miles - previous_position,
        "return_miles": previous_return, "access_miles": 0.0,
        "distance_miles_estimate": previous_return + main_route_miles - previous_position,
    })
    return legs
