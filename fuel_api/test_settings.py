"""Offline tests use an in-memory database and never migrate PostgreSQL."""

from .settings import *  # noqa: F403

USE_POSTGIS = False
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "django.contrib.gis"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
# Production migrations contain PostGIS fields. Sync the JSON fallback model
# directly into the temporary SQLite test database instead.
MIGRATION_MODULES = {"stations": None}
MAPBOX_ACCESS_TOKEN = ""
TOMTOM_API_KEY = ""
ORS_API_KEY = ""
