# Fuel-Optimized Route API

This project implements the demonstration API described in
[the project brief](priv/PROJECT_BRIEF.md). Development is being completed
phase by phase so that data preparation, geocoding, routing, optimization, and
API integration remain independently testable.

## Phase 1: environment setup

The repository contains a Django 5.2 project with Django REST Framework and
the PostgreSQL driver. Until PostgreSQL/PostGIS is configured, the project uses
SQLite locally so Django checks and the development server can be verified.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py runserver
```

Set `USE_POSTGIS=1` and the `POSTGRES_*` variables from
[.env.example](.env.example) when running against PostgreSQL with PostGIS.
No API keys or credentials belong in the repository.

The next implementation phase will add the station model, geocoding status,
and reproducible import of the already-cleaned station data.
