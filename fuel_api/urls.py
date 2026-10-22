"""URL configuration for the fuel-optimized route API."""

from django.urls import path

from routing.views import main_route

urlpatterns = [
    path("api/routes/main/", main_route, name="main-route"),
]
