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
        if not finite_number(data):
            raise serializers.ValidationError("Use a finite JSON number.")
        return super().to_internal_value(data)


class CoordinateSerializer(StrictSerializer):
    longitude = NumberField(min_value=-180, max_value=180)
    latitude = NumberField(min_value=-90, max_value=90)


class CandidateSearchSerializer(StrictSerializer):
    corridor_miles = NumberField(min_value=0.1, max_value=25, default=5)
    limit = serializers.IntegerField(min_value=1, max_value=200, default=50)


class MainRouteRequestSerializer(StrictSerializer):
    start = CoordinateSerializer()
    destination = CoordinateSerializer()
    candidate_search = CandidateSearchSerializer(required=False)

    def validate(self, attrs):
        start = Coordinate(**attrs["start"])
        destination = Coordinate(**attrs["destination"])
        if start == destination:
            raise serializers.ValidationError("Start and destination must be different.")
        attrs.update(start=start, destination=destination)
        return attrs
