"""Summarize one materialized snapshot without updating station records."""

from collections import Counter, defaultdict


def geocoding_report(rows: list[dict]) -> dict:
    statuses = Counter({status: 0 for status in ("required", "successful", "ambiguous", "failed")})
    providers = defaultdict(Counter)
    states = defaultdict(Counter)
    errors = Counter()
    successful_without_location = 0
    unsuccessful_with_location = 0
    conflicts = 0
    timestamps = []
    # Build all summaries from the same station snapshot.
    for row in rows:
        status = row["geocoding_status"]
        provider = row["geocoding_provider"] or "unassigned"
        statuses[status] += 1
        providers[provider][status] += 1
        states[row["state"]][status] += 1
        if row["geocoding_error"]:
            errors[(provider, status, row["geocoding_error"])] += 1
        # A successful lookup should have coordinates; other statuses should not.
        has_location = row["location"] is not None
        successful_without_location += status == "successful" and not has_location
        unsuccessful_with_location += status != "successful" and has_location
        conflicts += bool(row["address_conflict"])
        if row["geocoded_at"] is not None:
            timestamps.append(row["geocoded_at"])
    return {
        "total_stations": len(rows),
        "by_status": dict(statuses),
        "by_provider": {key: dict(value) for key, value in sorted(providers.items())},
        "by_state": {key: dict(value) for key, value in sorted(states.items())},
        "errors": [
            {"provider": provider, "status": status, "error": error, "count": count}
            for (provider, status, error), count in sorted(
                errors.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "latest_geocoded_at": max(timestamps).isoformat() if timestamps else None,
        "checks": {
            "counts_reconcile": sum(statuses.values()) == len(rows),
            "successful_without_location": successful_without_location,
            "unsuccessful_with_location": unsuccessful_with_location,
            "stations_with_address_conflicts": conflicts,
        },
    }
