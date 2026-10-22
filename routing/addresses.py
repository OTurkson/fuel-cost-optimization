"""Resolve trip addresses without using the background station geocoder."""

import json
import unicodedata
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache

from routing.caching import cache_key
from routing.services import Coordinate, RoutingError, finite_number, geographic_meters


class AddressResolver:
    endpoint = "https://api.heigit.org/pelias/v1/search"

    def __init__(self, api_key, timeout_seconds=30):
        self.api_key, self.timeout_seconds = api_key, timeout_seconds
        self.api_calls = self.cache_hits = 0

    def resolve(self, address):
        text = " ".join(unicodedata.normalize("NFKC", address).split())
        key = cache_key("address", [self.endpoint, text.casefold(), "USA", 3])
        cached = cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if not self.api_key.strip():
            raise RoutingError("geocoding_not_configured", "ORS_API_KEY is required to resolve trip addresses.", 503)
        request = Request(self.endpoint + "?" + urlencode({"text": text, "size": 3, "boundary.country": "USA"}),
                          headers={"Authorization": self.api_key, "Accept": "application/json"})
        self.api_calls += 1
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            code = 503 if exc.code in (401, 403, 429) or exc.code >= 500 else 502
            raise RoutingError("geocoding_unavailable", "The address provider rejected the lookup; check access and quota.", code) from None
        except TimeoutError:
            raise RoutingError("geocoding_timeout", "The address provider timed out.", 504) from None
        except URLError as exc:
            code = 504 if isinstance(exc.reason, TimeoutError) else 503
            raise RoutingError("geocoding_unavailable", "The address provider could not be reached.", code) from None
        except (OSError, HTTPException):
            raise RoutingError("geocoding_unavailable", "The address provider connection failed.", 503) from None
        except (ValueError, UnicodeError):
            raise RoutingError("invalid_geocoding_response", "The address provider returned invalid JSON.") from None
        result = normalize_address(payload)
        cache.set(key, result, timeout=30 * 86400)
        return result


def normalize_address(payload):
    invalid = RoutingError("invalid_geocoding_response", "The address provider returned an invalid result.")
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise invalid
    matches = []
    for feature in payload["features"]:
        if not isinstance(feature, dict):
            raise invalid
        geometry, properties = feature.get("geometry"), feature.get("properties")
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            raise invalid
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) != 2:
            raise invalid
        try:
            point = Coordinate(*coordinates)
        except RoutingError:
            raise invalid from None
        confidence = properties.get("confidence")
        if not finite_number(confidence) or not 0 <= confidence <= 1:
            raise invalid
        if properties.get("country_a") == "USA" and confidence >= 0.7:
            matches.append((confidence, point, properties.get("label", "Resolved US location")))
    if not matches:
        raise RoutingError("address_not_found", "No confident US address match was found. Try a fuller address or coordinates.", 422)
    matches.sort(key=lambda row: row[0], reverse=True)
    confidence, point, label = matches[0]
    if any(confidence - score < 0.05 and geographic_meters(point, other) > 100 for score, other, _ in matches[1:]):
        raise RoutingError("address_ambiguous", "The address has multiple plausible matches. Add a city/state or supply coordinates.", 422)
    metadata = payload.get("geocoding")
    attribution = metadata.get("attribution") if isinstance(metadata, dict) else None
    return {"longitude": point.longitude, "latitude": point.latitude,
            "label": label if isinstance(label, str) else "Resolved US location",
            "attribution": attribution if isinstance(attribution, str) and attribution.strip()
            else "Geocoding by openrouteservice / Pelias"}
