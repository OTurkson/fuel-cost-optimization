# Phase 4 routing contract

Checked against official provider documentation on 22 October 2026.

## Request and response

Use `POST https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson`
with the key in the server-side `Authorization` header. The provider announced
the [host migration](https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912)
in April 2026. The [subsequent announcement](https://ask.openrouteservice.org/t/reducing-the-quota-of-deprecated-api-api-openrouteservice-org/8013)
sets retirement of the old host for 2–6 November 2026. No fallback to the old
host is implemented.

The request contains exactly two `[longitude, latitude]` coordinate pairs,
`units: "m"`, `instructions: false`, and `extra_info: ["countryinfo"]`.
The fixed `driving-car` profile avoids adding unspecified vehicle dimensions
or other routing options. `driving-hgv` is also a provider profile, but is not
exposed by this phase's endpoint.

The [return-type documentation](https://giscience.github.io/openrouteservice/api-reference/endpoints/directions/requests-and-return-types)
defines GeoJSON as a FeatureCollection containing a route LineString and
properties with summary distance in metres, duration in seconds, and waypoint
indices. The adapter requires one complete route, validates every coordinate,
and preserves its line for later PostGIS searches. Miles are calculated as
metres / 1609.344 without premature rounding. Duration is optional.
Metadata query/engine fields are not relayed to clients. Provider attribution
is retained, with an ORS/HeiGIT/OpenStreetMap fallback if omitted.

The [extra-info documentation](https://giscience.github.io/openrouteservice/api-reference/endpoints/directions/extra-info/)
supports `countryinfo` for driving profiles and defines contiguous geometry
index ranges. The [country table](https://giscience.github.io/openrouteservice/technical-details/country-list)
identifies the United States as provider ID 214. The first and last ranges must
identify the US; missing, unknown, or incomplete metadata is rejected. Foreign
transit between US endpoints is permitted. These checks concern road-snapped
endpoints, not the precise legal boundary under the input coordinates. Provider
snapping can shift an endpoint near a border; no reverse geocoding is performed.
Alaska/Hawaii coordinates are not excluded, but disconnected trips return no
route when the provider cannot connect them.

## Limits and errors

The official [quota FAQ](https://giscience.github.io/openrouteservice/frequently-asked-questions#when-and-how-does-my-quota-reset)
documents the default Directions allowance of 2,000 requests per 24-hour window
and 40 requests in any sliding 60-second window. The actual allowance must be
checked in the [account dashboard](https://account.heigit.org/).
HTTP 403 can indicate exhausted daily quota as well as denied access;
HTTP 429 indicates rate limiting. The client does not automatically retry.

The provider's [published restrictions](https://openrouteservice.org/restrictions/)
list a maximum driving-route distance of 6,000 km and 50 waypoints. These were
available in the provider page's indexed content during verification; the main
website currently serves an outage notice, so direct access to that page was
unavailable. This adapter uses only two waypoints, handles the provider's limit
error, and rejects any returned distance exceeding 6,000,000 metres. Its limit
is independent of the vehicle's later 500-mile tank range.

[Routing error codes](https://giscience.github.io/openrouteservice/api-reference/error-codes)
distinguish limit errors (2004), missing routes (2009), and unsnappable points
(2010). Responses from this application use:

| HTTP | Application error | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Missing/invalid coordinates, duplicate endpoints, or unsupported fields |
| 400 | `outside_us` | A routed endpoint is outside the US |
| 422 | `route_not_found` | No route or an endpoint cannot be snapped |
| 422 | `route_limit_exceeded` | Provider limits exceeded |
| 503 | `routing_not_configured` | Missing server key or invalid timeout setting |
| 503 | `routing_authentication_failed`, `routing_access_denied` | Server credentials/access/quota problem |
| 503 | `routing_rate_limited`, `routing_unavailable` | Rate limit or provider connection/service failure |
| 504 | `routing_timeout` | Provider request timed out |
| 502 | `invalid_routing_response`, `route_country_unverified`, `routing_request_failed` | Provider contract could not be verified or request was rejected unexpectedly |

Provider response bodies and credentials are not included in these errors.
Standard DRF JSON errors still apply to malformed JSON, unsupported media types,
and unsupported HTTP methods.

## Call budget and later phases

Every valid request with a configured key makes one main-routing call, with a
30-second socket timeout. Invalid input and missing configuration make none.
There are no station lookups, station-geocoding calls, database writes, retries,
or detour requests. Successful responses report the call count and measured
routing/normalization elapsed time. The timeout is a socket timeout, not a strict
total wall-clock request deadline.

Requests are not cached yet. Persistent caching and expiration belong to Phase 9.
Current [HeiGIT terms](https://account.heigit.org/info/tos) must be reviewed when
implementing storage. A recent [official staff clarification](https://ask.openrouteservice.org/t/free-plan-in-an-app-with-a-paid-tier-license-cc-by-sa-4-0-personal-data-and-attribution-questions/8079)
identifies route results as CC-BY-SA 4.0; older indexed terms say CC-BY 4.0.
Do not assume the older license or invent a provider-mandated cache TTL.
The adapter already carries attribution with each route.

Directions waypoints describe sequential travel; returned segments run between
successive waypoints. They are not independent detour measurements. A future
Phase 6 design must verify Matrix/other suitable semantics and applicable
limits before promising that all detours fit in one additional request.

The Phase 4 default response returns a main route only. Phase 5 now adds an
optional `candidate_search` request object for a read-only PostGIS shortlist,
documented in the README. It adds no external requests. A station database
failure returns `candidate_search_unavailable` (503). Address resolution,
detour measurement, integrated fuel purchases/optimization, and persistent route
caching remain separate phases. No fuel-feasibility claim is made for long routes.

## Live smoke check

On 22 October 2026, the Django endpoint was exercised with the locally configured
key for the README's Dallas/Houston coordinates. It returned HTTP 200,
384,375.3 metres (238.8397384 miles), a duration of 13,482.6 seconds, and a
1,460-point LineString. Routing plus normalization measured 628.42 ms for this
single check; this is an observation, not a response-time guarantee. The response
is saved locally at `data/main_route_dallas_houston.json` (ignored by Git).

Database cursor execution was blocked during the check, and the response
contained no provider key. The check used Django's test client and a real
provider request; importing/running the Postman collection remains a manual
client check. The initial sandboxed attempt could not reach the provider;
the permitted network retry succeeded. No geocoding process was restarted,
and no station data was written.
