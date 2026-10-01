"""Tests for the Nominatim geocoding adapter with mocked HTTP."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.utils import timezone

from routes.models import GeocodeCache
from routes.services.geocoding import (
    GeocodingError,
    NonUSLocationError,
    geocode_location,
    normalize_query,
)


def _make_response(status_code=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or []
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _make_client(responses):
    """Create a mock httpx.Client that returns responses in order."""
    client = MagicMock(spec=httpx.Client)
    client.get = MagicMock(side_effect=responses)
    return client


class TestNormalizeQuery:
    def test_casefold(self):
        assert normalize_query("New York, NY") == "new york, ny"

    def test_whitespace(self):
        assert normalize_query("  New   York  ") == "new york"


@pytest.mark.django_db
class TestGeocodeLocation:
    def test_cache_hit(self):
        """A fresh cache entry should return without any HTTP call."""
        GeocodeCache.objects.create(
            normalized_query="new york, ny",
            latitude=Decimal("40.7128"),
            longitude=Decimal("-74.0060"),
            display_name="New York, NY, USA",
            country_code="us",
            status=GeocodeCache.Status.RESOLVED,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = MagicMock(spec=httpx.Client)
        result = geocode_location("New York, NY", client=client)
        assert result.cached is True
        assert result.latitude == 40.7128
        client.get.assert_not_called()

    def test_cache_miss_success(self):
        """A cache miss should make exactly one HTTP call and cache the result."""
        api_response = [
            {
                "lat": "41.8781",
                "lon": "-87.6298",
                "display_name": "Chicago, IL, USA",
                "country_code": "us",
                "type": "city",
            }
        ]
        client = _make_client([_make_response(200, api_response)])
        result = geocode_location("Chicago, IL", client=client)
        assert result.cached is False
        assert result.latitude == 41.8781
        assert result.country_code == "us"
        client.get.assert_called_once()

        # Verify cache was written
        cached = GeocodeCache.objects.get(normalized_query="chicago, il")
        assert cached.status == GeocodeCache.Status.RESOLVED
        assert float(cached.latitude) == 41.8781

    def test_non_us_rejection(self):
        """A non-US result should raise NonUSLocationError."""
        api_response = [
            {
                "lat": "51.5074",
                "lon": "-0.1278",
                "display_name": "London, UK",
                "country_code": "gb",
            }
        ]
        client = _make_client([_make_response(200, api_response)])
        with pytest.raises(NonUSLocationError):
            geocode_location("London", client=client)

    def test_empty_response_unresolved(self):
        """An empty API response should raise NonUSLocationError."""
        client = _make_client([_make_response(200, [])])
        with pytest.raises(NonUSLocationError):
            geocode_location("Nowhere", client=client)

    def test_timeout_retry_then_success(self):
        """A timeout followed by success should retry and succeed."""
        responses = [
            httpx.TimeoutException("timeout"),
            _make_response(
                200, [{"lat": "40.0", "lon": "-75.0", "country_code": "us"}]
            ),
        ]
        client = _make_client(responses)
        with patch("routes.services.geocoding.time.sleep"):
            result = geocode_location("Someplace", client=client)
        assert result.latitude == 40.0
        assert client.get.call_count == 2

    def test_5xx_retry_exhaustion(self):
        """Repeated 5xx responses should eventually raise GeocodingError."""
        responses = [_make_response(503), _make_response(503), _make_response(503)]
        client = _make_client(responses)
        with (
            patch("routes.services.geocoding.time.sleep"),
            pytest.raises(GeocodingError),
        ):
            geocode_location("Someplace", client=client)
        assert client.get.call_count == 3

    def test_expired_cache_triggers_refresh(self):
        """An expired cache entry should trigger a new HTTP call."""
        GeocodeCache.objects.create(
            normalized_query="old query",
            latitude=Decimal("40.0"),
            longitude=Decimal("-75.0"),
            display_name="Old",
            country_code="us",
            status=GeocodeCache.Status.RESOLVED,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        api_response = [
            {"lat": "41.0", "lon": "-76.0", "display_name": "New", "country_code": "us"}
        ]
        client = _make_client([_make_response(200, api_response)])
        result = geocode_location("old query", client=client)
        assert result.latitude == 41.0
        client.get.assert_called_once()
