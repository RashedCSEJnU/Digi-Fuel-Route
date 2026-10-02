"""Nominatim geocoding adapter with persistent cache, rate limiting, and retries."""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone

from routes.exceptions import NonUSLocationError  # canonical – used by DRF handler
from routes.models import GeocodeCache

logger = logging.getLogger("routes.geocoding")


class GeocodingError(Exception):
    """Raised when geocoding fails due to provider or network issues."""


# Re-export so existing callers that do
#   from routes.services.geocoding import NonUSLocationError
# continue to work without change.
__all__ = [
    "GeocodeResult",
    "GeocodingError",
    "NonUSLocationError",
    "geocode_location",
    "geocode_station_address",
    "normalize_query",
]


@dataclass
class GeocodeResult:
    latitude: float
    longitude: float
    display_name: str
    country_code: str
    provider_response_subset: dict
    cached: bool = False


def normalize_query(query: str) -> str:
    """Normalize a location string for cache lookup."""
    import re

    return re.sub(r"\s+", " ", query).strip().casefold()


def _get_cached(query: str) -> GeocodeCache | None:
    """Look up a fresh cache entry, returning None if absent or expired."""
    normalized = normalize_query(query)
    now = timezone.now()
    entry = GeocodeCache.objects.filter(normalized_query=normalized).first()
    if entry is None:
        return None
    if entry.expires_at is not None and entry.expires_at <= now:
        return None
    return entry


def _save_cache(
    query: str,
    result: GeocodeResult | None,
    status: str,
    ttl_hours: int,
) -> GeocodeCache:
    """Persist a geocode result to the cache."""
    normalized = normalize_query(query)
    expires_at = timezone.now() + timedelta(hours=ttl_hours) if ttl_hours > 0 else None

    defaults = {
        "latitude": result.latitude if result else None,
        "longitude": result.longitude if result else None,
        "display_name": result.display_name if result else "",
        "country_code": result.country_code if result else "",
        "provider_response_subset": result.provider_response_subset if result else {},
        "status": status,
        "expires_at": expires_at,
    }

    entry, _ = GeocodeCache.objects.update_or_create(
        normalized_query=normalized,
        defaults=defaults,
    )
    return entry


def _make_request(query: str, client: httpx.Client) -> dict:
    """Make a single Nominatim search request with retries."""
    base_url = settings.NOMINATIM_BASE_URL.rstrip("/")
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
        "addressdetails": 1,
    }
    headers = {"User-Agent": settings.GEOCODER_USER_AGENT}

    max_retries = 2
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.get(
                f"{base_url}/search",
                params=params,
                headers=headers,
                timeout=(
                    settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
                    settings.PROVIDER_READ_TIMEOUT_SECONDS,
                ),
            )
            if response.status_code >= 500 and attempt < max_retries:
                wait = min(2**attempt, 4)
                logger.warning(
                    "Nominatim 5xx on attempt %d, retrying in %ds", attempt + 1, wait
                )
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except (
            httpx.TimeoutException,
            httpx.HTTPStatusError,
            httpx.RequestError,
        ) as exc:
            last_error = exc
            if attempt < max_retries:
                wait = min(2**attempt, 4)
                logger.warning(
                    "Nominatim request error on attempt %d: %s, retrying in %ds",
                    attempt + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
            else:
                break

    raise GeocodingError(f"Nominatim request failed: {last_error}")


def geocode_location(
    query: str,
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> GeocodeResult:
    """Geocode a location string to US coordinates.

    Uses the persistent cache when available. Makes at most one provider
    request on a cache miss.
    """
    if use_cache:
        cached = _get_cached(query)
        if cached is not None:
            if cached.status == GeocodeCache.Status.RESOLVED:
                logger.info("Geocode cache hit for query: %s", query)
                return GeocodeResult(
                    latitude=float(cached.latitude),
                    longitude=float(cached.longitude),
                    display_name=cached.display_name,
                    country_code=cached.country_code,
                    provider_response_subset=cached.provider_response_subset,
                    cached=True,
                )
            elif cached.status == GeocodeCache.Status.UNRESOLVED:
                logger.info("Geocode cache hit (unresolved) for query: %s", query)
                raise NonUSLocationError(f"Location not found or not in USA: {query}")

    logger.info("Geocode cache miss for query: %s", query)

    close_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=(
                settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
                settings.PROVIDER_READ_TIMEOUT_SECONDS,
            )
        )

    try:
        data = _make_request(query, client)
    except GeocodingError:
        _save_cache(
            query, None, GeocodeCache.Status.FAILED, settings.GEOCODE_CACHE_TTL_HOURS
        )
        raise
    finally:
        if close_client:
            client.close()

    if not data or not isinstance(data, list) or len(data) == 0:
        _save_cache(
            query,
            None,
            GeocodeCache.Status.UNRESOLVED,
            settings.GEOCODE_CACHE_TTL_HOURS,
        )
        raise NonUSLocationError(f"Location not found or not in USA: {query}")

    item = data[0]
    # country_code is at top level only when addressdetails=1 is NOT used;
    # with addressdetails=1 it lives inside item["address"]["country_code"].
    # Support both locations for forward/backward compatibility.
    country_code = (
        item.get("country_code")
        or item.get("address", {}).get("country_code")
        or ""
    ).lower()
    if country_code != "us":
        _save_cache(
            query,
            None,
            GeocodeCache.Status.UNRESOLVED,
            settings.GEOCODE_CACHE_TTL_HOURS,
        )
        raise NonUSLocationError(f"Location is not in the USA: {query}")

    result = GeocodeResult(
        latitude=float(item["lat"]),
        longitude=float(item["lon"]),
        display_name=item.get("display_name", ""),
        country_code=country_code,
        provider_response_subset={
            "lat": item.get("lat"),
            "lon": item.get("lon"),
            "display_name": item.get("display_name", ""),
            "type": item.get("type", ""),
            "class": item.get("class", ""),
        },
        cached=False,
    )

    _save_cache(
        query, result, GeocodeCache.Status.RESOLVED, settings.GEOCODE_CACHE_TTL_HOURS
    )
    return result


def geocode_station_address(
    address: str,
    city: str,
    state: str,
    client: httpx.Client,
    delay_seconds: float = 1.0,
) -> GeocodeResult | None:
    """Geocode a station address with rate limiting.

    Returns None if the address cannot be resolved to US coordinates.
    """
    full_query = f"{address}, {city}, {state}"
    try:
        result = geocode_location(full_query, client=client, use_cache=True)
        return result
    except NonUSLocationError:
        return None
    except GeocodingError:
        return None
    finally:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
