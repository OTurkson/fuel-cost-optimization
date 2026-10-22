from collections.abc import Mapping

from rest_framework import serializers

from routing.services import Coordinate, finite_number


class StrictSerializer(serializers.Serializer):
    """Reject ignored address/options fields instead of silently misrouting."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({key: "Unknown field." for key in sorted(unknown)})
        return super().to_internal_value(data)


class NumberField(serializers.FloatField):
    def to_internal_value(self, data):
        # Reject booleans and numeric strings instead of silently converting them.
        if not finite_number(data):
            raise serializers.ValidationError("Use a finite JSON number.")
        return super().to_internal_value(data)


class CoordinateSerializer(StrictSerializer):
    longitude = NumberField(min_value=-180, max_value=180)
    latitude = NumberField(min_value=-90, max_value=90)


class CandidateSearchSerializer(StrictSerializer):
    corridor_miles = NumberField(min_value=0.1, max_value=25, default=5)
    limit = serializers.IntegerField(min_value=1, max_value=200, default=50)


class DetourEvaluationSerializer(StrictSerializer):
    # Each station adds two matrix locations: its anchor and the station itself.
    limit = serializers.IntegerField(min_value=1, max_value=29, default=20)


class MainRouteRequestSerializer(StrictSerializer):
    start = CoordinateSerializer()
    destination = CoordinateSerializer()
    candidate_search = CandidateSearchSerializer(required=False)
    detour_evaluation = DetourEvaluationSerializer(required=False)

    def validate(self, attrs):
        # Detours need the station shortlist produced by candidate search.
        if "detour_evaluation" in attrs and "candidate_search" not in attrs:
            raise serializers.ValidationError({"candidate_search": "Required when requesting detour evaluation."})
        # Pass validated coordinate objects to the routing service.
        start = Coordinate(**attrs["start"])
        destination = Coordinate(**attrs["destination"])
        if start == destination:
            raise serializers.ValidationError("Start and destination must be different.")
        attrs.update(start=start, destination=destination)
        return attrs


class OptimizeRequestSerializer(StrictSerializer):
    start = CoordinateSerializer(required=False)
    destination = CoordinateSerializer(required=False)
    start_address = serializers.CharField(required=False, max_length=500, trim_whitespace=True)
    destination_address = serializers.CharField(required=False, max_length=500, trim_whitespace=True)
    candidate_search = CandidateSearchSerializer(default=dict)
    detour_evaluation = DetourEvaluationSerializer(default=dict)

    def validate(self, attrs):
        for endpoint in ("start", "destination"):
            if (endpoint in attrs) == (endpoint + "_address" in attrs):
                raise serializers.ValidationError({endpoint: "Supply coordinates or an address, exactly one."})
            if endpoint in attrs:
                attrs[endpoint] = Coordinate(**attrs[endpoint])
        if "start" in attrs and attrs["start"] == attrs.get("destination"):
            raise serializers.ValidationError("Start and destination must be different.")
        return attrs
