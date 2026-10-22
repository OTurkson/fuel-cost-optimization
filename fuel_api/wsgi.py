"""WSGI config for the fuel-optimized route API."""

import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuel_api.settings")

application = get_wsgi_application()
