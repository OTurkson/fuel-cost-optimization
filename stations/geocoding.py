from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
        # Mapbox gets an address query rather than a truck-stop name search.
        parameters = urlencode(
            {
                "q": query,
                "country": "us",
                "types": "address",
                "autocomplete": "false",
                "limit": "5",
                "access_token": self.access_token,
            }
        )
        url = f"{self.endpoint}?{parameters}"
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
        except (URLError, TimeoutError) as exc:
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


class TomTomGeocoder:
    """Resolve addresses and fuel-stop POIs through TomTom Search."""

    endpoint = "https://api.tomtom.com/search/2/search"
    provider_name = "tomtom"

    def __init__(self, api_key: str, timeout_seconds: int = 30) -> None:
        if not api_key:
            raise ValueError("A TomTom API key is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def geocode(
        self, query: str, expected_city: str = "", expected_state: str = ""
    ) -> GeocodingResult:
        """Return a precise US result, or preserve uncertainty for review."""
        url = (
            f"{self.endpoint}/{quote(query, safe='')}.json?key={quote(self.api_key)}"
            "&countrySet=US&limit=5&typeahead=false"
        )
        request = Request(url, headers={"User-Agent": "fuel-cost-optimization/1.0"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload: dict[str, Any] = json.load(response)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise RuntimeError("TomTom authentication failed") from exc
            if exc.code == 429:
                raise RuntimeError("TomTom rate limit reached") from exc
            if exc.code == 404:
                return GeocodingResult(
                    status="failed", error="TomTom returned no matching result"
                )
            raise RuntimeError(f"TomTom request failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("TomTom request could not be reached") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("TomTom returned invalid JSON") from exc

        if not isinstance(payload, dict):
            return GeocodingResult(status="failed", error="Invalid TomTom response")
        results = payload.get("results", [])
        if not isinstance(results, list) or not results:
            return GeocodingResult(status="failed", error="No TomTom result")
        if any(
            not isinstance(result, dict)
            or not isinstance(result.get("address"), dict)
            for result in results
        ):
            return GeocodingResult(status="failed", error="Incomplete TomTom result")

        # A high score is not enough when the result is in the wrong city or state.
        if expected_state:
            state_matches = [
                result
                for result in results
                if self._text(result["address"].get("countryCode")).upper() == "US"
                and self._text(result["address"].get("countrySubdivision")).upper()
                == expected_state.strip().upper()
            ]
            if not state_matches:
                return GeocodingResult(
                    status="ambiguous",
                    error="TomTom returned no result in the expected US state",
                )
            results = state_matches

        if expected_city:
            city_matches = [
                result
                for result in results
                if self._text(result.get("address", {}).get("municipality")).casefold()
                == expected_city.strip().casefold()
            ]
            if not city_matches:
                return GeocodingResult(
                    status="ambiguous",
                    error="TomTom returned no result in the expected city",
                )
            results = city_matches

        first = results[0]
        position = first.get("position", {})
        address = first.get("address", {})
        score = first.get("score")
        if (
            not isinstance(position, dict)
            or not self._number(position.get("lon"))
            or not self._number(position.get("lat"))
            or not -180 <= position["lon"] <= 180
            or not -90 <= position["lat"] <= 90
            or not self._number(score)
        ):
            return GeocodingResult(status="failed", error="Incomplete TomTom result")
        # Fuzzy Search calls a precise address "Point Address", not "Address".
        if first.get("type") not in {"POI", "Point Address"}:
            return GeocodingResult(
                status="ambiguous",
                relevance=float(score),
                error=f"TomTom returned a {first.get('type', 'non-address')} result",
            )
        if self._text(address.get("countryCode")).upper() != "US":
            return GeocodingResult(
                status="ambiguous",
                relevance=float(score),
                error="TomTom result is outside the United States",
            )
        if score < 0.7:
            return GeocodingResult(
                status="ambiguous",
                relevance=float(score),
                error="Top TomTom result has low confidence",
            )

        # Check every close competitor, even when an earlier result is a duplicate.
        for candidate in results[1:]:
            if (
                self._number(candidate.get("score"))
                and float(score) - float(candidate["score"]) < 0.1
                and not self._same_place(first, candidate)
            ):
                return GeocodingResult(
                    status="ambiguous",
                    relevance=float(score),
                    error="TomTom returned similarly scored candidates",
                )

        return GeocodingResult(
            status="successful",
            longitude=float(position["lon"]),
            latitude=float(position["lat"]),
            relevance=float(score),
        )

    @staticmethod
    def _text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    @classmethod
    def _same_place(cls, first: dict, other: dict) -> bool:
        """Collapse only nearby matches with positive identity evidence."""
        positions = [first.get("position", {}), other.get("position", {})]
        if any(
            not isinstance(position, dict)
            or not all(cls._number(position.get(axis)) for axis in ("lat", "lon"))
            or not -90 <= position["lat"] <= 90
            or not -180 <= position["lon"] <= 180
            for position in positions
        ):
            return False
        # Distance only helps identify duplicates; it does not prove station access.
        lat1, lat2 = (math.radians(position["lat"]) for position in positions)
        dlat = lat2 - lat1
        dlon = math.radians(positions[1]["lon"] - positions[0]["lon"])
        haversine = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        distance_m = 2 * 6371008.8 * math.asin(math.sqrt(min(1, haversine)))
        if distance_m > 100:
            return False
        first_poi = first.get("poi") or {}
        other_poi = other.get("poi") or {}
        if not isinstance(first_poi, dict) or not isinstance(other_poi, dict):
            return False
        name = cls._text(first_poi.get("name")).casefold()
        other_name = cls._text(other_poi.get("name")).casefold()
        address = cls._text(first.get("address", {}).get("freeformAddress")).casefold()
        other_address = cls._text(other.get("address", {}).get("freeformAddress")).casefold()
        # Distinct named businesses at the same address remain ambiguous.
        if name and other_name and name != other_name:
            return False
        return bool(
            (name and name == other_name)
            or (address and address == other_address)
        )


def station_query(name: str, address: str, city: str, state: str) -> str:
    """Build a stable provider query with location and station identity."""
    parts = [name, address, city, state, "USA"]
    return ", ".join(part.strip() for part in parts if part.strip())


def mapbox_station_query(address: str, city: str, state: str) -> str:
    """Build an address-first query because Mapbox v6 does not return POIs."""
    parts = [address, city, state, "USA"]
    return ", ".join(part.strip() for part in parts if part.strip())
