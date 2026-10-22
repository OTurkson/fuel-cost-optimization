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

The station model, geocoding status fields, and reproducible cleaned-data
import command are now in place. The next implementation phase is geocoding
the retained stations. Add `MAPBOX_ACCESS_TOKEN` to the ignored `.env` file,
then start with a small sample:

```powershell
.\.venv312\Scripts\python.exe manage.py geocode_stations --limit 50
```

The command skips successful stations, stores the provider query and
relevance, and records failed or ambiguous results for review. It processes
only 50 stations by default; increase `--limit` only after reviewing the
sample. Use `--retry-failed` explicitly when retrying non-successful results.

The initial Mapbox v6 sample returned 13 precise matches and 37 ambiguous
street/place results. Those ambiguous records are intentionally not assigned
coordinates. Do not run the full 6,738-station batch until the provider is
confirmed to return station-level results for this dataset; Mapbox v6 does not
return POI features from its Geocoding API.
