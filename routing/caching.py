"""Cache sanitized provider results, never station prices or credentials."""

import hashlib
import json

from django.core.cache import cache


def cache_key(kind, inputs):
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "fuel:v1:" + kind + ":" + hashlib.sha256(encoded.encode()).hexdigest()


class CachedRouter:
    def __init__(self, provider):
        self.provider = provider
        self.metrics = {"directions_api_calls": 0, "matrix_api_calls": 0,
                        "route_cache_hits": 0, "matrix_cache_hits": 0}

    def main_route(self, start, destination):
        key = cache_key("route", [self.provider.endpoint, "driving-car", "countryinfo",
                                  start.as_list(), destination.as_list()])
        result = cache.get(key)
        if result is not None:
            self.metrics["route_cache_hits"] += 1
            return result
        self.metrics["directions_api_calls"] += 1
        result = self.provider.main_route(start, destination)
        cache.set(key, result, timeout=86400)
        return result

    def distance_matrix(self, locations):
        key = cache_key("matrix", [self.provider.matrix_endpoint, "driving-car", "distance", "m",
                                   [point.as_list() for point in locations]])
        result = cache.get(key)
        if result is not None:
            self.metrics["matrix_cache_hits"] += 1
            return result
        self.metrics["matrix_api_calls"] += 1
        result = self.provider.distance_matrix(locations)
        cache.set(key, result, timeout=86400)
        return result
