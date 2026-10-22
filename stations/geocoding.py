from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class GeocodingResult:
    """The small, provider-neutral result the import command needs."""

    status: str
    longitude: float | None = None
    latitude: float | None = None
    relevance: float | None = None
    error: str = ""


class MapboxGeocoder:
    """Resolve station descriptions through Mapbox without storing the token."""

    endpoint = "https://api.mapbox.com/search/geocode/v6/forward"
    provider_name = "mapbox"

    def __init__(self, access_token: str, timeout_seconds: int = 30) -> None:
        if not access_token:
            raise ValueError("A Mapbox access token is required")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds

    def geocode(self, query: str) -> GeocodingResult:
        """Return a cautious match; do not turn a weak result into coordinates."""
        url = (
            f"{self.endpoint}?q={quote(query)}&country=us"
            f"&limit=5&access_token={quote(self.access_token)}"
        )
        request = Request(url, headers={"User-Agent": "fuel-cost-optimization/1.0"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload: dict[str, Any] = json.load(response)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise RuntimeError("Mapbox authentication failed") from exc
            if exc.code == 429:
                raise RuntimeError("Mapbox rate limit reached") from exc
            raise RuntimeError(f"Mapbox request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError("Mapbox request could not be reached") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Mapbox returned invalid JSON") from exc

        features = payload.get("features", [])
        if not isinstance(features, list) or not features:
            return GeocodingResult(status="failed", error="No Mapbox result")

        first = features[0]
        coordinates = first.get("geometry", {}).get("coordinates", [])
        properties = first.get("properties", {})
        relevance = first.get("relevance")
        feature_type = properties.get("feature_type")
        if (
            not isinstance(coordinates, list)
            or len(coordinates) < 2
        ):
            return GeocodingResult(status="failed", error="Incomplete Mapbox result")

        # Geocoding v6 returns streets and intersections but no longer returns
        # POIs. A street coordinate is not precise enough for a fuel station.
        if feature_type not in {"address", "intersection", "poi"}:
            return GeocodingResult(
                status="ambiguous",
                error=f"Mapbox returned a {feature_type or 'non-address'} result",
            )

        if isinstance(relevance, (int, float)) and relevance < 0.7:
            return GeocodingResult(
                status="ambiguous",
                relevance=float(relevance),
                error="Top Mapbox result has low relevance",
            )

        if len(features) > 1 and isinstance(relevance, (int, float)):
            second_relevance = features[1].get("relevance")
            if (
                isinstance(second_relevance, (int, float))
                and float(relevance) - float(second_relevance) < 0.1
            ):
                return GeocodingResult(
                    status="ambiguous",
                    relevance=float(relevance),
                    error="Mapbox returned similarly relevant candidates",
                )

        return GeocodingResult(
            status="successful",
            longitude=float(coordinates[0]),
            latitude=float(coordinates[1]),
            relevance=float(relevance) if isinstance(relevance, (int, float)) else None,
        )


def station_query(name: str, address: str, city: str, state: str) -> str:
    """Build a stable query that gives Mapbox both context and station identity."""
    parts = [name, address, city, state, "USA"]
    return ", ".join(part.strip() for part in parts if part.strip())
