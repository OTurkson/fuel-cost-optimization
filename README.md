# Fuel-Optimized Route API

A Django REST API for planning driving routes and estimating fuel costs across
the United States using supplied fuel-station prices.

The API currently returns route geometry, driving distance, and optional nearby
station candidates. Partial-refueling logic is implemented separately; detour
and fuel-cost integration is in progress. Prices are static demo values treated
as USD per US gallon.

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

Import the [Postman collection](postman/main-routing.postman_collection.json),
or send `POST /api/routes/main/` with a JSON body:

```json
{
  "start": {"longitude": -96.7970, "latitude": 32.7767},
  "destination": {"longitude": -95.3698, "latitude": 29.7604}
}
```

Add `"candidate_search": {"corridor_miles": 5, "limit": 50}` to include nearby
stations from the prepared station dataset.
