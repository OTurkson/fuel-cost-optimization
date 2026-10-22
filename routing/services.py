"""One openrouteservice request and a small provider-independent route model."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


METERS_PER_MILE = 1609.344
MAX_ROUTE_METERS = 6_000_000
US_COUNTRY_ID = 214  # ORS country IDs, not ISO numeric codes.
ATTRIBUTION = "© openrouteservice by HeiGIT | Data © OpenStreetMap contributors"


class RoutingError(Exception):
    """A safe error code/message for callers, without provider response bodies."""

    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def finite_number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Coordinate:
    longitude: float
    latitude: float

    def __post_init__(self):
        if (
            not finite_number(self.longitude) or not -180 <= self.longitude <= 180
            or not finite_number(self.latitude) or not -90 <= self.latitude <= 90
        ):
            raise RoutingError("invalid_coordinates", "Coordinates must be finite WGS84 longitude/latitude values.", 400)

    def as_list(self) -> list[float]:
        return [self.longitude, self.latitude]


def geographic_meters(first: Coordinate, second: Coordinate) -> float:
    """Spherical coordinate separation for snap checks, never a driving leg."""
    lat1, lat2 = math.radians(first.latitude), math.radians(second.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(second.longitude - first.longitude)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1, value)))


@dataclass(frozen=True)
class MainRoute:
    geometry: dict
    distance_meters: float
    duration_seconds: float | None
    attribution: str
    profile: str = "driving-car"

    @property
    def distance_miles(self) -> float:
        return self.distance_meters / METERS_PER_MILE

    def as_dict(self) -> dict:
        return {
            "geometry": self.geometry,
            "distance_meters": self.distance_meters,
            "distance_miles": self.distance_miles,
            "duration_seconds": self.duration_seconds,
            "profile": self.profile,
            "provider": "openrouteservice",
            "attribution": self.attribution,
        }


class OpenRouteService:
    endpoint = "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson"
    matrix_endpoint = "https://api.heigit.org/openrouteservice/v2/matrix/driving-car"

    def __init__(self, api_key: str, timeout_seconds: float = 30):
        if not api_key.strip():
            raise RoutingError("routing_not_configured", "ORS_API_KEY is not configured on the server.", 503)
        if not finite_number(timeout_seconds) or timeout_seconds <= 0:
            raise RoutingError("routing_not_configured", "Routing timeout must be a positive finite number.", 503)
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds

    def main_route(self, start: Coordinate, destination: Coordinate) -> MainRoute:
        if start == destination:
            raise RoutingError("identical_locations", "Start and destination must be different.", 400)
        payload = self._post_json(
            self.endpoint,
            {
                "coordinates": [start.as_list(), destination.as_list()],
                "units": "m", "instructions": False,
                "extra_info": ["countryinfo"],
            },
            accept="application/geo+json",
        )
        return normalize_route(payload)

    def distance_matrix(self, locations: list[Coordinate]):
        from routing.matrix import MAX_MATRIX_LOCATIONS, normalize_matrix

        if not 2 <= len(locations) <= MAX_MATRIX_LOCATIONS:
            raise RoutingError("invalid_matrix_request", "Matrix requests require 2 to 58 locations.", 400)
        # Omitting sources/destinations requests every directional pair.
        payload = self._post_json(self.matrix_endpoint, {
            "locations": [location.as_list() for location in locations],
            "metrics": ["distance"], "units": "m",
        })
        return normalize_matrix(payload, len(locations), requested_locations=locations)

    def _post_json(self, endpoint: str, body: dict, accept="application/json"):
        request = Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json", "Accept": accept,
                "User-Agent": "fuel-cost-optimization/1.0",
            },
            method="POST",
        )
        # No automatic retries, including for matrix errors or exhausted quota.
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise self._http_error(exc) from None
        except TimeoutError:
            raise RoutingError("routing_timeout", "The routing provider timed out.", 504) from None
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RoutingError("routing_timeout", "The routing provider timed out.", 504) from None
            raise RoutingError("routing_unavailable", "The routing provider could not be reached.", 503) from None
        except (OSError, HTTPException):
            raise RoutingError("routing_unavailable", "The routing provider connection failed.", 503) from None
        except (ValueError, UnicodeError):
            raise RoutingError("invalid_routing_response", "The routing provider returned invalid JSON.") from None
        return payload

    @staticmethod
    def _http_error(error: HTTPError) -> RoutingError:
        if error.code == 401:
            return RoutingError("routing_authentication_failed", "The routing provider rejected the server credentials.", 503)
        if error.code == 403:
            # The provider also uses 403 when the daily allowance is exhausted.
            return RoutingError("routing_access_denied", "Routing access was denied; check the server key and daily quota.", 503)
        if error.code == 429:
            return RoutingError("routing_rate_limited", "The routing provider rate limit was reached. Try again later.", 503)
        if error.code >= 500:
            return RoutingError("routing_unavailable", "The routing provider is temporarily unavailable.", 503)
        try:
            # Read only the error code; the provider's message may contain inputs.
            payload = json.load(error)
            code = payload.get("error", {}).get("code")
        except (ValueError, UnicodeError, AttributeError, OSError):
            code = None
        if type(code) is not int:
            code = None
        if code in {2004, 6004} or error.code == 413:
            return RoutingError("route_limit_exceeded", "The trip exceeds the routing provider's supported limits.", 422)
        if code in {2009, 2010, 2013, 2014, 2015, 2016, 6010}:
            return RoutingError("route_not_found", "No driving route could be established between these locations.", 422)
        return RoutingError("routing_request_failed", "The routing provider rejected the route request.")


def normalize_route(payload: dict) -> MainRoute:
    """Validate GeoJSON, normalize SI units, and check the snapped endpoints."""
    invalid = RoutingError("invalid_routing_response", "The routing provider returned an incomplete or invalid route.")
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise invalid
    features = payload.get("features")
    if not isinstance(features, list):
        raise invalid
    if not features:
        raise RoutingError("route_not_found", "No driving route was returned for these locations.", 422)
    if len(features) != 1 or not isinstance(features[0], dict):
        raise invalid
    feature = features[0]
    geometry = feature.get("geometry")
    properties = feature.get("properties")
    if feature.get("type") != "Feature" or not isinstance(geometry, dict) or not isinstance(properties, dict):
        raise invalid
    points = geometry.get("coordinates")
    if geometry.get("type") != "LineString" or not isinstance(points, list) or len(points) < 2:
        raise invalid
    # Check the whole line before it reaches PostGIS or a map client.
    for point in points:
        if (
            not isinstance(point, list) or len(point) != 2
            or not all(finite_number(value) for value in point)
            or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90
        ):
            raise invalid
    # Distances stay in metres internally; miles are derived from the same value.
    summary = properties.get("summary")
    if not isinstance(summary, dict):
        raise invalid
    distance = summary.get("distance")
    duration = summary.get("duration")
    if not finite_number(distance) or distance <= 0:
        raise invalid
    if distance > MAX_ROUTE_METERS:
        raise RoutingError("route_limit_exceeded", "The driving route exceeds the supported 6,000 km limit.", 422)
    if duration is not None and (not finite_number(duration) or duration < 0):
        raise invalid
    if properties.get("way_points") != [0, len(points) - 1]:
        raise invalid

    # Provider country metadata avoids adding reverse-geocoding requests. These
    # are the road-snapped endpoints; this is not a cadastral boundary check.
    extras = properties.get("extras")
    countryinfo = extras.get("countryinfo") if isinstance(extras, dict) else None
    sections = countryinfo.get("values") if isinstance(countryinfo, dict) else None
    if not isinstance(sections, list) or not sections:
        raise RoutingError("route_country_unverified", "The provider did not supply usable endpoint country information.")
    last_end = 0
    for section in sections:
        if (
            not isinstance(section, list) or len(section) != 3
            or any(type(value) is not int for value in section)
            or section[0] != last_end or not section[0] < section[1] < len(points)
        ):
            raise invalid
        last_end = section[1]
    if last_end != len(points) - 1:
        raise invalid
    if sections[0][2] <= 0 or sections[-1][2] <= 0:
        raise RoutingError("route_country_unverified", "The provider could not identify an endpoint country.")
    if sections[0][2] != US_COUNTRY_ID or sections[-1][2] != US_COUNTRY_ID:
        raise RoutingError("outside_us", "Both routed endpoints must be within the United States.", 400)
    metadata = payload.get("metadata")
    attribution = metadata.get("attribution") if isinstance(metadata, dict) else None
    return MainRoute(
        geometry={"type": "LineString", "coordinates": points},
        distance_meters=float(distance),
        duration_seconds=float(duration) if duration is not None else None,
        attribution=attribution if isinstance(attribution, str) and attribution.strip() else ATTRIBUTION,
    )
