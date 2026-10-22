"""Django settings for the fuel-optimized route API."""

from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

# Keep local credentials convenient for development without adding a dotenv
# dependency; production environments can still provide normal environment
# variables directly.
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.is_file():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "stations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "fuel_api.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

WSGI_APPLICATION = "fuel_api.wsgi.application"
ASGI_APPLICATION = "fuel_api.asgi.application"

USE_POSTGIS = os.environ.get("USE_POSTGIS", "0").lower() in {"1", "true", "yes"}
if USE_POSTGIS:
    # Django GIS needs the native GDAL DLL on Windows; winget's GDAL package
    # installs it outside the Python environment.
    gdal_library = Path(
        os.environ.get("GDAL_LIBRARY_PATH", r"C:\Program Files\GDAL\gdal.dll")
    )
    if gdal_library.is_file():
        GDAL_LIBRARY_PATH = str(gdal_library)
        os.environ.setdefault("PATH", "")
        os.environ["PATH"] = f"{gdal_library.parent};{os.environ['PATH']}"
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(gdal_library.parent))
    INSTALLED_APPS.insert(1, "django.contrib.gis")
if USE_POSTGIS:
    DATABASES = {
        "default": {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "NAME": os.environ.get("DB_NAME", os.environ.get("POSTGRES_DB", "fuel_cost")),
            "USER": os.environ.get("DB_USER", os.environ.get("POSTGRES_USER", "postgres")),
            "PASSWORD": os.environ.get(
                "DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
            ),
            "HOST": os.environ.get(
                "DB_HOST", os.environ.get("POSTGRES_HOST", "localhost")
            ),
            "PORT": os.environ.get("DB_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}
