"""Normalize a full directional ORS road-distance matrix; null means unrouted."""

from dataclasses import dataclass

from routing.services import ATTRIBUTION, Coordinate, RoutingError, finite_number, geographic_meters


MAX_MATRIX_LOCATIONS = 58  # 58 squared = 3,364, below the published 3,500-pair limit.


@dataclass(frozen=True)
class SnappedLocation:
    location: Coordinate
    snapped_distance_meters: float
    distance_method: str = "provider_reported"


@dataclass(frozen=True)
class RoadDistanceMatrix:
    distances_meters: list[list[float | None]]
    sources: list[SnappedLocation | None]
    destinations: list[SnappedLocation | None]
    attribution: str


def normalize_matrix(payload, size: int, requested_locations: list[Coordinate] | None = None) -> RoadDistanceMatrix:
    invalid = RoutingError("invalid_matrix_response", "The routing provider returned an invalid distance matrix.")
    if not isinstance(payload, dict):
        raise invalid
    if requested_locations is not None and len(requested_locations) != size:
        raise invalid
    distances = payload.get("distances")
    if not isinstance(distances, list) or len(distances) != size:
        raise invalid
    # Rows are sources and columns are destinations; keep that direction intact.
    normalized = []
    for index, row in enumerate(distances):
        if not isinstance(row, list) or len(row) != size:
            raise invalid
        if any(value is not None and (not finite_number(value) or value < 0) for value in row):
            raise invalid
        if row[index] not in (None, 0):
            raise invalid
        # A null cell means no route, not a zero-mile journey.
        normalized.append([float(value) if value is not None else None for value in row])
    # These are the road locations the provider actually used for each endpoint.
    snapped = []
    for key in ("sources", "destinations"):
        items = payload.get(key)
        if not isinstance(items, list) or len(items) != size:
            raise invalid
        entries = []
        for index, item in enumerate(items):
            if item is None:
                entries.append(None)
                continue
            if not isinstance(item, dict):
                raise invalid
            coordinates = item.get("location")
            offset = item.get("snapped_distance")
            if (
                not isinstance(coordinates, list) or len(coordinates) != 2
            ):
                raise invalid
            try:
                coordinate = Coordinate(*coordinates)
            except RoutingError:
                raise invalid from None
            if offset is None and requested_locations is not None:
                # Live HeiGIT responses can omit snapped_distance despite the
                # backend schema. Compute geographic separation for snap checks
                # only; all driving distances still come from matrix cells.
                offset = geographic_meters(requested_locations[index], coordinate)
                method = "geographic_separation_from_snapped_coordinate"
            else:
                method = "provider_reported"
            if not finite_number(offset) or offset < 0:
                raise invalid
            if requested_locations is not None:
                offset = max(offset, geographic_meters(requested_locations[index], coordinate))
            entries.append(SnappedLocation(coordinate, float(offset), method))
        snapped.append(entries)
    metadata = payload.get("metadata")
    attribution = metadata.get("attribution") if isinstance(metadata, dict) else None
    return RoadDistanceMatrix(
        normalized, snapped[0], snapped[1],
        attribution if isinstance(attribution, str) and attribution.strip() else ATTRIBUTION,
    )
