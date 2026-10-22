from time import perf_counter

from django.conf import settings
from django.db import DatabaseError
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from routing.serializers import MainRouteRequestSerializer, OptimizeRequestSerializer
from routing.optimization import optimize_trip
from routing.services import OpenRouteService, RoutingError
from routing.detours import evaluate_detours
from stations.candidates import find_candidates


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def optimize_route(request):
    serializer = OptimizeRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": {"code": "invalid_request", "message": "Provide a start and destination using addresses or coordinates.",
                                   "details": serializer.errors}}, status=400)
    try:
        return Response(optimize_trip(serializer.validated_data))
    except RoutingError as exc:
        return Response({"error": {"code": exc.code, "message": str(exc)}}, status=exc.status_code)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def main_route(request):
    # Validate before spending any routing requests.
    serializer = MainRouteRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": {
            "code": "invalid_request", "message": "Provide start and destination coordinates.",
            "details": serializer.errors,
        }}, status=400)
    started = perf_counter()
    candidate_options = serializer.validated_data.get("candidate_search")
    detour_options = serializer.validated_data.get("detour_evaluation")
    try:
        if candidate_options is not None and not settings.USE_POSTGIS:
            raise RoutingError("candidate_search_unavailable", "Candidate search requires PostgreSQL/PostGIS.", 503)
        # Get the main route once; later steps reuse its geometry and distance.
        router = OpenRouteService(settings.ORS_API_KEY, settings.ORS_TIMEOUT_SECONDS)
        route = router.main_route(
            serializer.validated_data["start"], serializer.validated_data["destination"]
        )
        routing_elapsed_ms = round((perf_counter() - started) * 1000, 2)
        candidate_result = None
        candidate_elapsed_ms = None
        detour_result = None
        detour_elapsed_ms = None
        # Station searches are local and only run when the caller asks for them.
        if candidate_options is not None:
            search_started = perf_counter()
            try:
                candidate_result = find_candidates(route, **candidate_options)
            except DatabaseError:
                raise RoutingError("candidate_search_unavailable", "The station search database is unavailable.", 503) from None
            candidate_elapsed_ms = round((perf_counter() - search_started) * 1000, 2)
        # Detour evaluation can add one Matrix request for the shortlist.
        if detour_options is not None:
            detour_started = perf_counter()
            detour_result = evaluate_detours(router, route, candidate_result["stations"], **detour_options)
            detour_elapsed_ms = round((perf_counter() - detour_started) * 1000, 2)
    except RoutingError as exc:
        # Return safe messages without exposing provider bodies or credentials.
        return Response({"error": {"code": exc.code, "message": str(exc)}}, status=exc.status_code)
    # Keep the basic response small and attach optional results below.
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
    if detour_result is not None:
        result["detour_evaluation"] = detour_result
        result["metrics"]["routing_api_calls"] += detour_result["matrix_api_calls"]
        result["metrics"]["detour_evaluation_elapsed_ms"] = detour_elapsed_ms
    return Response(result)
