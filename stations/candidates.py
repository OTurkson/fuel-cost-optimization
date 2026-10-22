"""Read-only route-corridor search and a bounded, geographically spread shortlist."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from routing.services import Coordinate, METERS_PER_MILE, MainRoute, RoutingError, finite_number


MAX_CANDIDATES = 200
MAX_CORRIDOR_MILES = 25


@dataclass(frozen=True)
class StationCandidate:
    station_id: str
    name: str
    longitude: float
    latitude: float
    price_per_gallon: Decimal
    route_position_miles: float
    distance_to_route_miles: float

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id, "name": self.name,
            "longitude": self.longitude, "latitude": self.latitude,
            "price_per_gallon": str(self.price_per_gallon),
            "route_position_miles": self.route_position_miles,
            "distance_to_route_miles": self.distance_to_route_miles,
        }


def shortlist_candidates(
    candidates: list[StationCandidate], route_miles: float, limit: int
) -> list[StationCandidate]:
    """Price-ranked rounds across route bins preserve geographic variety.

    This is a heuristic shortlist, not a fuel-feasibility or optimality proof.
    A single nearest projection is used on routes that revisit the same area.
    """
    # Spread choices along the trip so cheap stations in one area do not dominate.
    bin_count = min(limit, max(1, math.ceil(route_miles / 100)))
    bins: dict[int, list[StationCandidate]] = {}
    for candidate in candidates:
        index = min(bin_count - 1, int(candidate.route_position_miles / route_miles * bin_count))
        bins.setdefault(index, []).append(candidate)
    # Prefer lower prices within each area, then shorter geographic offsets.
    for options in bins.values():
        options.sort(key=lambda candidate: (
            candidate.price_per_gallon, candidate.distance_to_route_miles,
            candidate.route_position_miles, candidate.station_id,
        ))
    selected = []
    depth = 0
    # Take one choice from each area before taking another from the same area.
    while len(selected) < min(limit, len(candidates)):
        for index in sorted(bins):
            if depth < len(bins[index]):
                selected.append(bins[index][depth])
                if len(selected) == limit:
                    break
        depth += 1
    return sorted(selected, key=lambda candidate: (candidate.route_position_miles, candidate.station_id))


def corridor_queryset(route: MainRoute, corridor_miles: float):
    """Use geography metre distances and the existing GiST location index.

    PostGIS 3.6 supports geography ST_LineLocatePoint, verified on this project's
    database. Do not use planar longitude/latitude degrees as mileage.
    """
    from django.contrib.gis.db.models import LineStringField
    from django.contrib.gis.db.models.functions import Distance, LineLocatePoint
    from django.contrib.gis.geos import LineString
    from django.contrib.gis.measure import D
    from django.db.models import Value

    from stations.models import FuelStation

    line = LineString(route.geometry["coordinates"], srid=4326)
    line_geography = Value(line, output_field=LineStringField(srid=4326, geography=True))
    # Filter with the spatial index before measuring or ordering nearby stations.
    return FuelStation.objects.filter(
        geocoding_status="successful", address_conflict=False,
        location__isnull=False, selected_price_usd_per_gallon__gt=0,
        location__dwithin=(line, D(mi=corridor_miles)),
    ).annotate(
        route_fraction=LineLocatePoint(line_geography, "location"),
        route_offset=Distance("location", line),
    ).order_by("opis_truckstop_id").values(
        "opis_truckstop_id", "truckstop_name", "location",
        "selected_price_usd_per_gallon", "route_fraction", "route_offset",
    )


def find_candidates(route: MainRoute, corridor_miles: float = 5, limit: int = 50) -> dict:
    if not finite_number(corridor_miles) or not 0.1 <= corridor_miles <= MAX_CORRIDOR_MILES:
        raise RoutingError("invalid_candidate_search", "Corridor must be between 0.1 and 25 miles.", 400)
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        raise RoutingError("invalid_candidate_search", "Candidate limit must be an integer between 1 and 200.", 400)
    if not settings.USE_POSTGIS:
        raise RoutingError("candidate_search_unavailable", "Candidate search requires PostgreSQL/PostGIS.", 503)
    if not finite_number(route.distance_miles) or route.distance_miles <= 0:
        raise RoutingError("invalid_routing_response", "Candidate search requires a positive route distance.")
    geometry = route.geometry
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        raise RoutingError("invalid_routing_response", "Candidate search requires a route LineString.")
    points = geometry.get("coordinates")
    if not isinstance(points, list) or len(points) < 2:
        raise RoutingError("invalid_routing_response", "Candidate search requires a complete route LineString.")
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise RoutingError("invalid_routing_response", "Invalid route coordinates.")
        Coordinate(*point)

    # Materialize one SELECT: background geocoding cannot change this snapshot.
    rows = list(corridor_queryset(route, corridor_miles))
    candidates = []
    for row in rows:
        price = row["selected_price_usd_per_gallon"]
        fraction = row["route_fraction"]
        offset_miles = row["route_offset"].m / METERS_PER_MILE
        if (
            not price.is_finite() or price <= 0
            or not finite_number(fraction) or not 0 <= fraction <= 1
            or not finite_number(offset_miles) or offset_miles < 0
        ):
            continue
        location = row["location"]
        candidates.append(StationCandidate(
            station_id=row["opis_truckstop_id"], name=row["truckstop_name"],
            longitude=location.x, latitude=location.y, price_per_gallon=price,
            route_position_miles=fraction * route.distance_miles,
            distance_to_route_miles=offset_miles,
        ))
    # Keep the full match count so callers can see whether options were omitted.
    selected = shortlist_candidates(candidates, route.distance_miles, limit)
    return {
        "stations": [candidate.as_dict() for candidate in selected],
        "corridor_miles": corridor_miles, "limit": limit,
        "matched_count": len(candidates), "returned_count": len(selected),
        "truncated": len(selected) < len(candidates),
        "position_method": "Geodesic nearest-point fraction of route shape scaled to main-route road distance",
        "distance_to_route_method": "Shortest geographic distance; not a driving detour",
        "selection_method": "Price-ranked rounds across route-position bins, with bin count capped by the requested limit",
        "fuel_feasibility_evaluated": False,
        "limitations": [
            "Only successfully geocoded, nonconflicting stations are considered.",
            "Projected station order and offsets do not establish driving access or fuel feasibility.",
            "A shortlist can omit a necessary station; no global optimality or infeasibility claim is made.",
            "On looping or self-crossing routes, only one nearest station occurrence is represented.",
        ],
        "pricing_assumption": "Minimum supplied price per station, treated as USD per US gallon for the demo; not verified current",
    }
