"""API views for the Fuel Route Optimization service."""

import logging

from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from routes.models import FuelStation
from routes.serializers import OptimizeRouteRequestSerializer
from routes.services.geocoding import geocode_location
from routes.services.optimizer import optimize_fuel_stops
from routes.services.routing import get_route

logger = logging.getLogger("routes.api")


class OptimizeRouteView(APIView):
    """POST /api/v1/routes/optimize/

    Accepts start/finish location strings and optional initial fuel,
    returns route geometry, fuel stops, and cost summary.
    """

    def post(self, request):
        serializer = OptimizeRouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        start_query = serializer.validated_data["start"]
        finish_query = serializer.validated_data["finish"]
        initial_fuel = float(serializer.validated_data["initial_fuel_gallons"])

        # Geocode start and finish (cached).
        # NonUSLocationError propagates; DRF exception handler maps it to 400.
        start_result = geocode_location(start_query)
        finish_result = geocode_location(finish_query)

        # Get route (cached).
        # ProviderError / NoRouteError propagate; handler maps them to 502.
        route_result = get_route(
            origin_lat=start_result.latitude,
            origin_lon=start_result.longitude,
            dest_lat=finish_result.latitude,
            dest_lon=finish_result.longitude,
        )

        # Short / zero-length route: no stops needed.
        if route_result.distance_miles < 0.1:
            return Response(self._build_zero_response(
                route_result, start_result, finish_result, initial_fuel
            ))

        # Query stations with resolved coordinates only and optimize locally.
        station_list = list(
            FuelStation.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
                retail_price__isnull=False,
            )
        )

        # NoFeasiblePlanError propagates; handler maps it to 404.
        result = optimize_fuel_stops(
            stations=station_list,
            route_geometry=route_result.geometry,
            initial_fuel_gallons=initial_fuel,
        )

        return Response(self._build_response(
            route_result, start_result, finish_result, result, initial_fuel
        ))

    # ------------------------------------------------------------------
    # Response builders
    # ------------------------------------------------------------------

    def _build_response(self, route_result, start_result, finish_result, result, initial_fuel):
        """Build the full optimization response payload."""
        stops_data = []
        for stop in result.stops:
            stops_data.append({
                "sequence": stop.sequence,
                "station": {
                    "id": stop.station.id,
                    "name": stop.station.name,
                    "address": stop.station.address,
                    "city": stop.station.city,
                    "state": stop.station.state,
                    "coordinates": {
                        "latitude": float(stop.station.latitude),
                        "longitude": float(stop.station.longitude),
                    },
                },
                "price_per_gallon": float(stop.price_per_gallon),
                "route_mile": round(stop.route_mile, 2),
                "detour_miles": round(stop.detour_miles, 2),
                "fuel_before_stop_gallons": round(stop.fuel_before_stop_gallons, 2),
                "gallons_purchased": round(stop.gallons_purchased, 2),
                "fuel_after_stop_gallons": round(stop.fuel_after_stop_gallons, 2),
                "estimated_cost": float(stop.estimated_cost),
            })

        return {
            "route": {
                "distance_miles": round(route_result.distance_miles, 2),
                "trip_distance_miles": round(result.trip_distance_miles, 2),
                "duration_seconds": round(route_result.duration_seconds, 2),
                "geometry": route_result.geometry,
            },
            "assumptions": {
                "max_range_miles": settings.MAX_RANGE_MILES,
                "mpg": settings.MPG,
                "tank_capacity_gallons": settings.TANK_CAPACITY_GALLONS,
                "initial_fuel_gallons": initial_fuel,
                "fuel_price_unit": "USD per gallon",
                "detour_distance_method": (
                    "estimated round-trip distance from nearest route point"
                ),
            },
            "fuel_stops": stops_data,
            "summary": {
                "total_fuel_consumed_gallons": round(result.total_fuel_consumed_gallons, 2),
                "total_gallons_purchased": round(result.total_gallons_purchased, 2),
                "total_fuel_cost": float(result.total_fuel_cost),
                "currency": "USD",
            },
            "metadata": {
                "geocoder_provider": "Nominatim",
                "route_provider": route_result.provider_name,
                "cached_geocodes": {
                    "start": start_result.cached,
                    "finish": finish_result.cached,
                },
                "cached_route": route_result.cached,
                "station_count_considered": result.station_count_considered,
                "optimizer_version": "v1",
            },
        }

    def _build_zero_response(self, route_result, start_result, finish_result, initial_fuel):
        """Build a response for a zero-length or very short route."""
        return {
            "route": {
                "distance_miles": round(route_result.distance_miles, 2),
                "trip_distance_miles": round(route_result.distance_miles, 2),
                "duration_seconds": round(route_result.duration_seconds, 2),
                "geometry": route_result.geometry,
            },
            "assumptions": {
                "max_range_miles": settings.MAX_RANGE_MILES,
                "mpg": settings.MPG,
                "tank_capacity_gallons": settings.TANK_CAPACITY_GALLONS,
                "initial_fuel_gallons": initial_fuel,
                "fuel_price_unit": "USD per gallon",
                "detour_distance_method": (
                    "estimated round-trip distance from nearest route point"
                ),
            },
            "fuel_stops": [],
            "summary": {
                "total_fuel_consumed_gallons": 0,
                "total_gallons_purchased": 0,
                "total_fuel_cost": 0.0,
                "currency": "USD",
            },
            "metadata": {
                "geocoder_provider": "Nominatim",
                "route_provider": route_result.provider_name,
                "cached_geocodes": {
                    "start": start_result.cached,
                    "finish": finish_result.cached,
                },
                "cached_route": route_result.cached,
                "station_count_considered": 0,
                "optimizer_version": "v1",
            },
        }
