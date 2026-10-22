# Fuel-Optimized Route API

This project implements the Fuel Cost Optimization API. Development is completed phase by phase so that data preparation, geocoding, routing, optimization, and API integration remain independently testable.

## Phase 1: environment setup

The repository contains a Django 6.1.2 project with Django REST Framework and
the PostgreSQL driver. Django 6.1.2 is the latest official stable Django
release. It requires Python 3.12 or newer. Until PostgreSQL/PostGIS is configured, the project uses
SQLite locally so Django checks and the development server can be verified.

```powershell
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
.\.venv312\Scripts\python.exe manage.py check
.\.venv312\Scripts\python.exe manage.py runserver
```

Set `USE_POSTGIS=1` and the `DB_*` variables from
[.env.example](.env.example) when running against PostgreSQL with PostGIS.
This mode also requires the native GDAL library on the development machine,
as required by Django GIS. Generate and apply migrations in that mode; the
local SQLite fallback uses a JSON placeholder for the not-yet-populated
location so checks can run without GDAL. No API keys or credentials belong in
the repository.

### Windows GIS prerequisites

The project expects both sides of the spatial stack:

1. **GDAL on the development machine.** The GDAL runtime is installed
   separately from Python and is configured automatically from
   `C:\Program Files\GDAL\gdal.dll` when present.
2. **PostGIS inside PostgreSQL.** Open
   `C:\Program Files\PostgreSQL\18\bin\stackbuilder.exe`, select the running
   PostgreSQL 18 instance, choose the Spatial Extensions category, and install
   the PostGIS package. Then enable it in the project database:

   ```sql
   CREATE EXTENSION IF NOT EXISTS postgis;
   ```

   The SQL can be run in pgAdmin or with `psql`. The Django migration cannot
   create the extension files; they must be installed on the PostgreSQL
   server first.

After that setup, verify the project and create the spatial table:

```powershell
.\.venv312\Scripts\python.exe manage.py check
.\.venv312\Scripts\python.exe manage.py migrate
.\.venv312\Scripts\python.exe manage.py import_cleaned_stations
```

## Phase 3: station geocoding and validation

The station model and import command are in place. TomTom is the selected
provider. Add `TOMTOM_API_KEY` to the ignored `.env` file and start a bounded
batch when no other geocoding command is running:

```powershell
.\.venv312\Scripts\python.exe manage.py geocode_stations --limit 100 --delay 1
```

The default limit is 50. Each result is saved separately so the command can
resume. Successful records are skipped. Existing Mapbox results are retained;
no new Mapbox lookups are needed. `--retry-failed` explicitly includes failed
and ambiguous records from the selected provider, but skips successful records
and other providers' results. Address conflicts are skipped for manual review.

TomTom matching accepts `POI` and `Point Address` results, requires the expected
US state and city, and rejects street, intersection, and address-range results.
It checks all returned competing results. Duplicate representations must be
within 100 metres and have a matching nonempty name or address; different named
businesses remain ambiguous. The stored score is TomTom's provider score, not
a calibrated probability. See the
[official Fuzzy Search response documentation](https://docs.tomtom.com/search-api/documentation/search-service/fuzzy-search).

### While a batch is running

An already-running command keeps its loaded Python code. These matching fixes
apply to newly started commands; they do not change existing stored matches.
Let the current batch finish before starting another geocoding command or
running an import. Multiple workers are not coordinated and can duplicate
lookups. No migrations or restarts are needed for this update.

The report command is safe to run during geocoding. It reads a single snapshot,
makes no provider calls, and never updates station records:

```powershell
.\.venv312\Scripts\python.exe manage.py geocoding_report
.\.venv312\Scripts\python.exe manage.py geocoding_report --output data/geocoding_progress.json --review-csv data/geocoding_review.csv
```

Counts are grouped by status, provider, state, and error. The report checks for
successful records missing coordinates and unsuccessful records with coordinates.
The CSV contains failed, ambiguous, conflicting, and inconsistent records.
Add `--include-successful` to export successful matches for manual spot checks,
particularly those created before state validation was added. The JSON and CSV
come from the same snapshot; counts can advance immediately afterward.

### Safe reimports and data provenance

After geocoding is idle, importing the same cleaned station identities updates
prices and audit counters while preserving all geocoding fields. Imports reject
changed names/addresses, duplicate station IDs, address conflicts, nonfinite
prices, and input claiming completed geocoding without coordinates. A rejected
import rolls back all its changes. Review identity/address corrections separately
before changing records; stale coordinates must not be silently retained.

`openpyxl` is included in requirements. The cleaner implementation lives in
`stations/cleaning.py` so it can be versioned independently of ignored private
inputs. The existing `priv/clean_fuel_data.py` entry point remains compatible:

```powershell
.\.venv312\Scripts\python.exe -m stations.cleaning priv/fuel-prices-for-be-assessment.xlsx --output data/cleaned_preview.csv --report data/cleaning_preview_report.json
```

The workbook and existing data outputs are preserved. By agreement with the
project owner, supplied numeric prices are treated as **USD per US gallon for
this demo**, without conversion or independent verification. New cleaning
reports record `assumed_for_demo: true` and retain `verified: false` to distinguish
the assumption from source evidence. This does not block development. Minimum
supplied prices are static demo inputs and are not claimed to be current prices.

### Offline regression tests

```powershell
.\.venv312\Scripts\python.exe manage.py test stations.tests --settings=fuel_api.test_settings --noinput
```

This settings module uses an in-memory SQLite database and the model's JSON
location fallback. It does not migrate or write to PostgreSQL. Provider calls
are mocked. Tests cover matching, provider failures, resumability, import
rollback and progress preservation, report exports, and cleaner provenance.
The normal development settings continue to use PostGIS; SQLite tests do not
replace checking real spatial queries.

## Phase 4: main driving route

Add `ORS_API_KEY=your-key` to the ignored `.env` file. Create/manage the key in
the [HeiGIT dashboard](https://account.heigit.org/). Keep the key on the server;
Postman sends only trip coordinates. Routing uses the current HeiGIT API host.
No migrations, station imports, or geocoder restarts are needed.

Start the development server in a separate terminal:

```powershell
.\.venv312\Scripts\python.exe manage.py runserver --noreload
```

In Postman, send `POST http://127.0.0.1:8000/api/routes/main/` with a raw JSON body:

```json
{
  "start": {"longitude": -96.7970, "latitude": 32.7767},
  "destination": {"longitude": -95.3698, "latitude": 29.7604}
}
```

These are Dallas/Houston example coordinates; the provider calculates their
actual route. You can also import
[`postman/main-routing.postman_collection.json`](postman/main-routing.postman_collection.json)
for this request and input-validation examples. Change the collection's
`base_url` variable if using a different server port.

A successful response contains `route.geometry` (GeoJSON LineString),
`route.distance_meters`, `route.distance_miles`, `route.duration_seconds`,
profile/provider/attribution, the submitted endpoints, and metrics containing
one routing call and measured elapsed milliseconds. The geometry's endpoints
are snapped to roads and can differ slightly from the submitted coordinates.
Coordinates must be finite JSON numbers, with distinct start/destination.
Both routed endpoints must be identified as US locations by provider metadata.
Addresses and custom provider routing options are not accepted by this endpoint.
An optional local `candidate_search` object enables the Phase 5 search below.

Failures return JSON errors for invalid input (400), missing route/provider
limits (422), unavailable/unconfigured routing (503), timeout (504), or invalid
provider responses (502). Details and verified provider behavior are documented
in [`docs/routing-provider.md`](docs/routing-provider.md).

Without `candidate_search`, this endpoint returns the main route and makes no
database queries. With that option it also returns a read-only station shortlist.
Fuel optimization will be connected after detour evaluation. Repeated requests
currently make new routing calls; persistent caching remains Phase 9.

Run the complete offline regression suite:

```powershell
.\.venv312\Scripts\python.exe manage.py test routing.tests stations.tests --settings=fuel_api.test_settings --noinput
.\.venv312\Scripts\python.exe manage.py check
```

Routing tests use mocked responses and forbid database access. They verify the
request contract, country checks, metre/mile conversion, provider failures,
malformed responses, credential redaction, and the endpoint's one-call budget.
A real provider/Postman success still requires a configured key and network access.

## Phase 5: candidate station search

Add this option to the same Postman request body:

```json
{
  "start": {"longitude": -96.7970, "latitude": 32.7767},
  "destination": {"longitude": -95.3698, "latitude": 29.7604},
  "candidate_search": {"corridor_miles": 5, "limit": 50}
}
```

The Postman collection includes a **Main route with candidate stations** request.
`corridor_miles` accepts 0.1–25 miles (default 5), and `limit` accepts 1–200
(default 50). An empty `candidate_search: {}` uses both defaults. This opt-in
search requires PostGIS; SQLite returns a clear 503 before making a routing call.
Restart a development server started with `--noreload` once to load these code
changes; leave the geocoding terminal running.

One index-assisted geography `ST_DWithin` query selects successfully geocoded,
nonconflicting stations with positive prices. A single query snapshot is safe
while geocoding continues. The existing GiST spatial index is reused, and no
station records are written. The geodesic `ST_LineLocatePoint` function requires
PostGIS 3.6 (available in this project's database). Candidate search adds zero
external calls to the one Directions request.

The response's `candidate_search.stations` contains IDs, names, coordinates,
decimal price strings, approximate `route_position_miles`, and geographic
`distance_to_route_miles`. The nearest-point fraction of the route geometry is
scaled to the provider's total road distance for approximate ordering. This
does not measure driving access or an actual detour. Revisited/looping route
sections currently get one nearest station occurrence.

When needed, stations are shortlisted in price-ranked rounds across route bins
of approximately 100 miles; bins widen when a small requested limit caps their
count. This keeps options spread along the trip. Counts,
truncation, selection method, and limitations are included. No candidate is
declared reachable or infeasible on these estimates; a shortlist may omit a
necessary station, and geocoding coverage is still incomplete.

The local read-only smoke result is saved at
`data/candidates_dallas_houston.json`. It used the saved route geometry and made
no provider calls. Station counts will change as more geocoding results arrive.

## Partial-refueling planner: independent preparation

The updated brief retains a full initial 50-gallon tank and 10 MPG, and allows
partial refueling. The pure module `fuel/planning.py` implements purchases for
a **fixed itinerary with supplied cumulative driving distances**. It has no
Django, database, geocoder, or HTTP dependencies. Approximate station projections
from Phase 5 must not be passed to it as if they were driving distances.

```python
from fuel.planning import ItineraryStation, plan_fuel_trip

plan = plan_fuel_trip(
    200,
    [ItineraryStation("expensive", 40, 5), ItineraryStation("cheaper", 120, 3)],
    initial_fuel_gallons=15,
)
print(plan.as_dict())
```

This example skips the expensive stop, arrives at mile 120 with 3 gallons,
buys 5 gallons for $15, and reaches mile 200 with zero gallons. With the default
full starting tank, this 200-mile trip requires no purchase.

The planner looks ahead to the first cheaper-or-equal-price reachable station
and buys only any shortfall. If none is reachable, it fills up as needed, capped
by the fuel needed to finish. It tracks arrival/departure fuel, remaining range,
consumption, purchases, and ending fuel. Starting fuel is excluded from cost.
Each purchase cost rounds to cents with `ROUND_HALF_UP`; the total sums those
rounded costs. Zero reserve is assumed and exact-range legs are allowed.

Fuel balances must reconcile: starting + purchased = consumed + ending.
Tests include hand calculations and an independent exhaustive fuel-state check
on small fixed itineraries. The planner does not choose alternative detours or
prove optimality across road-access choices. Detour evaluation and end-to-end
optimization are the remaining integration work; no cost result is attached to
the route endpoint yet.

Run all current tests without writing to the live database:

```powershell
.\.venv312\Scripts\python.exe manage.py test fuel.tests routing.tests stations.tests --settings=fuel_api.test_settings --noinput
```
