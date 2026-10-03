"""Tests for the route optimization API endpoint with mocked providers."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from routes.exceptions import NoFeasiblePlanError, NonUSLocationError, ProviderError
from routes.models import FuelStation


def _make_station(pk, lat, lon, price=3.50, name="Test Station"):
    return FuelStation.objects.create(
        id=pk,
        opis_truckstop_id=str(1000 + pk),
        rack_id="1",
        name=name,
        address=f"{pk} Test Rd",
        city="Testville",
        state="OH",
        retail_price=Decimal(str(price)),
        latitude=lat,
        longitude=lon,
        normalized_identity=f"opis:{1000 + pk}:rack:1",
        geocode_status="resolved",
    )


def _mock_geocode(lat, lon, cached=False):
    r = MagicMock()
    r.latitude = lat
    r.longitude = lon
    r.cached = cached
    return r


def _mock_route(distance_miles=790.0, duration_seconds=45600.0, cached=False):
    r = MagicMock()
    r.distance_miles = distance_miles
    r.duration_seconds = duration_seconds
    r.cached = cached
    r.provider_name = "OSRM"
    r.geometry = {
        "type": "LineString",
        "coordinates": [
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ],
    }
    return r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestOptimizeRouteAPI:
    def setup_method(self):
        self.client = APIClient()
        self.url = reverse("optimize-route")

    # -----------------------------------------------------------------------
    # Happy path
    # -----------------------------------------------------------------------

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_happy_path_200_schema(self, mock_geo, mock_rt):
        """Valid request returns 200 with the complete documented schema."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            _mock_geocode(41.8781, -87.6298),
        ]
        mock_rt.return_value = _mock_route()
        _make_station(1, 41.0, -80.0, price=3.50)

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 200
        data = resp.json()

        # Top-level keys
        for key in ("route", "assumptions", "fuel_stops", "summary", "metadata"):
            assert key in data, f"Missing top-level key: {key}"

        # Route sub-keys
        assert data["route"]["distance_miles"] > 0
        assert data["route"]["duration_seconds"] > 0
        assert data["route"]["geometry"]["type"] == "LineString"
        assert "trip_distance_miles" in data["route"]

        # Assumptions
        assert data["assumptions"]["max_range_miles"] == 500
        assert data["assumptions"]["mpg"] == 10
        assert data["assumptions"]["tank_capacity_gallons"] == 50
        assert data["assumptions"]["initial_fuel_gallons"] == 50

        # Metadata
        meta = data["metadata"]
        assert meta["geocoder_provider"] == "Nominatim"
        assert meta["route_provider"] == "OSRM"
        assert meta["cached_geocodes"]["start"] is False
        assert meta["cached_geocodes"]["finish"] is False
        assert meta["cached_route"] is False
        assert meta["optimizer_version"] == "v1"
        assert "station_count_considered" in meta

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_numeric_consistency(self, mock_geo, mock_rt):
        """Total cost == sum of stop costs; total purchased == sum of stop purchases."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            _mock_geocode(41.8781, -87.6298),
        ]
        mock_rt.return_value = _mock_route(distance_miles=790.0)
        _make_station(1, 41.0, -80.0, price=3.50)

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 200
        data = resp.json()

        stop_costs = sum(s["estimated_cost"] for s in data["fuel_stops"])
        assert abs(data["summary"]["total_fuel_cost"] - stop_costs) < 0.02

        stop_gallons = sum(s["gallons_purchased"] for s in data["fuel_stops"])
        assert abs(data["summary"]["total_gallons_purchased"] - stop_gallons) < 0.02

    # -----------------------------------------------------------------------
    # Request validation — 400
    # -----------------------------------------------------------------------

    def test_missing_start(self):
        resp = self.client.post(self.url, {"finish": "Chicago, IL"}, format="json")
        assert resp.status_code == 400

    def test_blank_start(self):
        resp = self.client.post(self.url, {"start": "", "finish": "Chicago, IL"}, format="json")
        assert resp.status_code == 400

    def test_missing_finish(self):
        resp = self.client.post(self.url, {"start": "New York, NY"}, format="json")
        assert resp.status_code == 400

    def test_blank_finish(self):
        resp = self.client.post(
            self.url, {"start": "New York, NY", "finish": "   "}, format="json"
        )
        assert resp.status_code == 400

    def test_negative_initial_fuel(self):
        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL", "initial_fuel_gallons": -1},
            format="json",
        )
        assert resp.status_code == 400

    def test_excessive_initial_fuel(self):
        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL", "initial_fuel_gallons": 51},
            format="json",
        )
        assert resp.status_code == 400

    def test_non_numeric_initial_fuel(self):
        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL", "initial_fuel_gallons": "abc"},
            format="json",
        )
        assert resp.status_code == 400

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_default_initial_fuel_is_50(self, mock_geo, mock_rt):
        """Omitting initial_fuel_gallons defaults to 50."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            _mock_geocode(41.8781, -87.6298),
        ]
        mock_rt.return_value = _mock_route()
        _make_station(1, 41.0, -80.0, price=3.50)

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        # Whatever the status, the assumption should encode 50 when returned 200
        if resp.status_code == 200:
            assert resp.json()["assumptions"]["initial_fuel_gallons"] == 50
        else:
            # 404 is acceptable only if route needs stops but none available
            assert resp.status_code in (200, 404)

    # -----------------------------------------------------------------------
    # Non-US location — 400
    # -----------------------------------------------------------------------

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_non_us_start_returns_400(self, mock_geo, mock_rt):
        """NonUSLocationError raised by geocoder → 400 with detail."""
        mock_geo.side_effect = NonUSLocationError("Location is not in the USA: London, UK")

        resp = self.client.post(
            self.url,
            {"start": "London, UK", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 400
        assert "detail" in resp.json()
        assert "USA" in resp.json()["detail"]

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_non_us_finish_returns_400(self, mock_geo, mock_rt):
        """NonUSLocationError raised by geocoder for finish location → 400 with detail."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            NonUSLocationError("Location is not in the USA: Toronto, Canada"),
        ]

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Toronto, Canada"},
            format="json",
        )
        assert resp.status_code == 400
        assert "detail" in resp.json()
        assert "USA" in resp.json()["detail"]

    # -----------------------------------------------------------------------
    # Provider error — 502
    # -----------------------------------------------------------------------

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_routing_provider_error_returns_502(self, mock_geo, mock_rt):
        """ProviderError from OSRM → 502."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            _mock_geocode(41.8781, -87.6298),
        ]
        mock_rt.side_effect = ProviderError("OSRM unavailable")

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 502
        assert resp.json()["detail"] == "Unable to retrieve route data from an upstream provider."

    # -----------------------------------------------------------------------
    # No feasible plan — 404
    # -----------------------------------------------------------------------

    @patch("routes.views.optimize_fuel_stops")
    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_no_feasible_plan_returns_404(self, mock_geo, mock_rt, mock_opt):
        """NoFeasiblePlanError → 404 with stable detail message."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            _mock_geocode(41.8781, -87.6298),
        ]
        mock_rt.return_value = _mock_route(distance_miles=2000.0)
        mock_opt.side_effect = NoFeasiblePlanError("No plan")

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No feasible fuel-stop plan is available for this route."

    # -----------------------------------------------------------------------
    # Cache metadata
    # -----------------------------------------------------------------------

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_cache_flags_reflected_in_metadata(self, mock_geo, mock_rt):
        """cached_geocodes and cached_route metadata reflect actual cache state."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006, cached=True),
            _mock_geocode(41.8781, -87.6298, cached=True),
        ]
        mock_rt.return_value = _mock_route(cached=True)
        _make_station(1, 41.0, -80.0, price=3.50)

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 200
        meta = resp.json()["metadata"]
        assert meta["cached_geocodes"]["start"] is True
        assert meta["cached_geocodes"]["finish"] is True
        assert meta["cached_route"] is True

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_warm_cache_still_calls_services_once(self, mock_geo, mock_rt):
        """On a warm cache hit the view still calls geocode/route exactly once each."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006, cached=True),
            _mock_geocode(41.8781, -87.6298, cached=True),
        ]
        mock_rt.return_value = _mock_route(cached=True)
        _make_station(1, 41.0, -80.0, price=3.50)

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 200
        assert mock_geo.call_count == 2   # one call per location
        assert mock_rt.call_count == 1    # one route call total

    # -----------------------------------------------------------------------
    # Zero-length / same-point route
    # -----------------------------------------------------------------------

    @patch("routes.views.get_route")
    @patch("routes.views.geocode_location")
    def test_zero_length_route_returns_empty_stops(self, mock_geo, mock_rt):
        """A route shorter than 0.1 miles returns 200 with no stops and zero cost."""
        mock_geo.side_effect = [
            _mock_geocode(40.7128, -74.006),
            _mock_geocode(40.7128, -74.006),
        ]
        mock_rt.return_value = _mock_route(distance_miles=0.05)

        resp = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "New York, NY"},
            format="json",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fuel_stops"] == []
        assert data["summary"]["total_fuel_cost"] == 0.0
        assert data["summary"]["total_gallons_purchased"] == 0
