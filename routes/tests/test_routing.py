"""Tests for the OSRM routing adapter with mocked HTTP."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.utils import timezone

from routes.exceptions import NoRouteError, ProviderError
from routes.models import RouteCache
from routes.services.routing import get_route


def _make_response(status_code=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _make_client(responses):
    client = MagicMock(spec=httpx.Client)
    client.get = MagicMock(side_effect=responses)
    return client


def _valid_osrm_response():
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": 1272199.9,
                "duration": 53440.6,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [-74.006, 40.7128],
                        [-73.9, 40.8],
                        [-87.6298, 41.8781],
                    ],
                },
            }
        ],
    }


@pytest.mark.django_db
class TestGetRoute:
    def test_cache_hit(self):
        """A fresh cache entry should return without any HTTP call."""
        from routes.services.routing import _make_cache_key

        key = _make_cache_key(40.7128, -74.0060, 41.8781, -87.6298)
        RouteCache.objects.create(
            cache_key=key,
            origin_latitude=Decimal("40.7128"),
            origin_longitude=Decimal("-74.0060"),
            destination_latitude=Decimal("41.8781"),
            destination_longitude=Decimal("-87.6298"),
            profile="driving",
            provider_name="OSRM",
            distance_miles=790.4,
            duration_seconds=45600.0,
            geometry={
                "type": "LineString",
                "coordinates": [[-74.0, 40.7], [-87.6, 41.8]],
            },
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = MagicMock(spec=httpx.Client)
        result = get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)
        assert result.cached is True
        assert result.distance_miles == 790.4
        client.get.assert_not_called()

    def test_cache_miss_success(self):
        """A cache miss should make exactly one HTTP call and cache the result."""
        client = _make_client([_make_response(200, _valid_osrm_response())])
        result = get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)
        assert result.cached is False
        assert result.distance_miles > 0
        assert result.duration_seconds > 0
        assert result.geometry["type"] == "LineString"
        client.get.assert_called_once()

        # Verify cache was written
        assert RouteCache.objects.count() == 1

    def test_no_route_error(self):
        """OSRM NoRoute should raise NoRouteError."""
        client = _make_client([_make_response(200, {"code": "NoRoute", "routes": []})])
        with pytest.raises(NoRouteError):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)

    def test_malformed_geometry(self):
        """Invalid geometry should raise ProviderError."""
        bad = _valid_osrm_response()
        bad["routes"][0]["geometry"] = {"type": "Point", "coordinates": [0, 0]}
        client = _make_client([_make_response(200, bad)])
        with pytest.raises(ProviderError):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)

    def test_empty_routes(self):
        """Empty routes list should raise ProviderError."""
        client = _make_client([_make_response(200, {"code": "Ok", "routes": []})])
        with pytest.raises(ProviderError):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)

    def test_zero_distance(self):
        """Zero distance should raise ProviderError."""
        bad = _valid_osrm_response()
        bad["routes"][0]["distance"] = 0
        client = _make_client([_make_response(200, bad)])
        with pytest.raises(ProviderError):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)

    def test_timeout_retry_then_success(self):
        """A timeout followed by success should retry and succeed."""
        responses = [
            httpx.TimeoutException("timeout"),
            _make_response(200, _valid_osrm_response()),
        ]
        client = _make_client(responses)
        with patch("routes.services.routing.time.sleep"):
            result = get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)
        assert result.distance_miles > 0
        assert client.get.call_count == 2

    def test_5xx_retry_exhaustion(self):
        """Repeated 5xx responses should eventually raise ProviderError."""
        responses = [_make_response(503), _make_response(503), _make_response(503)]
        client = _make_client(responses)
        with (
            patch("routes.services.routing.time.sleep"),
            pytest.raises(ProviderError),
        ):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)
        assert client.get.call_count == 3

    def test_expired_cache_triggers_refresh(self):
        """An expired cache entry should trigger a new HTTP call."""
        RouteCache.objects.create(
            cache_key="expired_key",
            origin_latitude=Decimal("40.7128"),
            origin_longitude=Decimal("-74.0060"),
            destination_latitude=Decimal("41.8781"),
            destination_longitude=Decimal("-87.6298"),
            profile="driving",
            provider_name="OSRM",
            distance_miles=790.4,
            duration_seconds=45600.0,
            geometry={
                "type": "LineString",
                "coordinates": [[-74.0, 40.7], [-87.6, 41.8]],
            },
            expires_at=timezone.now() - timedelta(hours=1),
        )
        client = _make_client([_make_response(200, _valid_osrm_response())])
        result = get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)
        assert result.cached is False
        client.get.assert_called_once()

    def test_cache_key_directional(self):
        """A->B and B->A should have different cache keys."""
        from routes.services.routing import _make_cache_key

        key_ab = _make_cache_key(40.7128, -74.0060, 41.8781, -87.6298)
        key_ba = _make_cache_key(41.8781, -87.6298, 40.7128, -74.0060)
        assert key_ab != key_ba

    def test_malformed_coordinate(self):
        """Non-numeric coordinate should raise ProviderError."""
        bad = _valid_osrm_response()
        bad["routes"][0]["geometry"]["coordinates"] = [["abc", "def"], [0, 0]]
        client = _make_client([_make_response(200, bad)])
        with pytest.raises(ProviderError):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)

    def test_coordinate_out_of_range(self):
        """Out-of-range coordinate should raise ProviderError."""
        bad = _valid_osrm_response()
        bad["routes"][0]["geometry"]["coordinates"] = [[200, 40.7], [-87.6, 41.8]]
        client = _make_client([_make_response(200, bad)])
        with pytest.raises(ProviderError):
            get_route(40.7128, -74.0060, 41.8781, -87.6298, client=client)
