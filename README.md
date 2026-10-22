# Fuel-Optimized Route API

A Django REST API for planning driving routes and estimating fuel costs across
the United States using supplied fuel-station prices.

The API returns route geometry, recommended fuel stops, detour mileage, partial
fuel purchases, and estimated total expenditure. It assumes a full 50-gallon
starting tank and 10 MPG. Prices are static demo values treated as USD per US
gallon; intermediate driving distances and fuel balances are estimates.

Built with Python, Django REST Framework, PostgreSQL/PostGIS, openrouteservice
for routing, and Mapbox and TomTom integrations for station geocoding. TomTom
is the default geocoding provider.

## Setup

Requirements: Python 3.12+, PostgreSQL with PostGIS 3.6+, and GDAL.

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

Create `.env` using [.env.example](.env.example). Set `USE_POSTGIS=1`, your
`DB_*` connection values, `ORS_API_KEY` for routing, and `TOMTOM_API_KEY` for
station data preparation.

Enable PostGIS in the project database:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

## Run

```powershell
.\.venv312\Scripts\python.exe manage.py migrate
.\.venv312\Scripts\python.exe manage.py runserver
```

The API runs at `http://127.0.0.1:8000`.

## Usage

Import the [Postman collection](postman/fuel-optimization.postman_collection.json),
send a request, and open the response **Visualize** tab for the route map.
The principal endpoint is `POST /api/routes/optimize/`:

```json
{
  "start": {"longitude": -96.7970, "latitude": 32.7767},
  "destination": {"longitude": -95.3698, "latitude": 29.7604}
}
```

Alternatively, supply `start_address` and `destination_address` strings.
Trips within 500 miles require no fuel purchases with the starting full tank.
Longer trips use successfully geocoded stations from the prepared dataset.

Run the offline tests:

```powershell
.\.venv312\Scripts\python.exe manage.py test --settings=fuel_api.test_settings
```

## Challenges encountered

- **Geocoding API consumption:** Resolving thousands of station addresses required
  controlling request volume. Bounded batches, request delays, and saved progress
  allow processing to resume without repeating successful lookups.
- **Provider differences:** Mapbox and TomTom required different query formats,
  response parsing, and matching rules. Mapbox queries use address details, while
  TomTom queries also include station names to help identify truck stops.
- **Ambiguous matches:** Highway addresses, similarly named stations, and broad
  street-level results did not always identify a precise location. Uncertain
  matches are recorded for review rather than assigned guessed coordinates.
