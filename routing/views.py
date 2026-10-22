from time import perf_counter

from django.conf import settings
from django.db import DatabaseError
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from routing.serializers import MainRouteRequestSerializer
from routing.services import OpenRouteService, RoutingError
from stations.candidates import find_candidates


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def main_route(request):
    serializer = MainRouteRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": {
            "code": "invalid_request", "message": "Provide start and destination coordinates.",
            "details": serializer.errors,
        }}, status=400)
    started = perf_counter()
    candidate_options = serializer.validated_data.get("candidate_search")
    try:
        if candidate_options is not None and not settings.USE_POSTGIS:
            raise RoutingError("candidate_search_unavailable", "Candidate search requires PostgreSQL/PostGIS.", 503)
        router = OpenRouteService(settings.ORS_API_KEY, settings.ORS_TIMEOUT_SECONDS)
        route = router.main_route(
            serializer.validated_data["start"], serializer.validated_data["destination"]
        )
        routing_elapsed_ms = round((perf_counter() - started) * 1000, 2)
        candidate_result = None
        candidate_elapsed_ms = None
        if candidate_options is not None:
            search_started = perf_counter()
            try:
                candidate_result = find_candidates(route, **candidate_options)
            except DatabaseError:
                raise RoutingError("candidate_search_unavailable", "The station search database is unavailable.", 503) from None
            candidate_elapsed_ms = round((perf_counter() - search_started) * 1000, 2)
    except RoutingError as exc:
        return Response({"error": {"code": exc.code, "message": str(exc)}}, status=exc.status_code)
    result = {
        "route": route.as_dict(),
        "start": {"longitude": serializer.validated_data["start"].longitude,
                  "latitude": serializer.validated_data["start"].latitude},
        "destination": {"longitude": serializer.validated_data["destination"].longitude,
                        "latitude": serializer.validated_data["destination"].latitude},
        "metrics": {"routing_api_calls": 1, "routing_elapsed_ms": routing_elapsed_ms},
    }
    if candidate_result is not None:
        result["candidate_search"] = candidate_result
        result["metrics"]["candidate_search_elapsed_ms"] = candidate_elapsed_ms
    return Response(result)
