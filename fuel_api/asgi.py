"""ASGI config for the fuel-optimized route API."""

import os

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuel_api.settings")

application = get_asgi_application()
