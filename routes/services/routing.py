"""OSRM routing adapter with persistent cache, retry, and validation."""

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone

from routes.exceptions import NoRouteError, ProviderError
from routes.models import RouteCache

logger = logging.getLogger("routes.routing")

METERS_PER_MILE = 1609.344


@dataclass
class RouteResult:
    """Internal route result from OSRM."""

    distance_miles: float
    duration_seconds: float
    geometry: dict  # GeoJSON LineString
    cached: bool = False
    provider_name: str = "OSRM"


def _make_cache_key(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    profile: str = "driving",
) -> str:
    """Build a deterministic cache key for a directional route."""
    raw = f"{origin_lat:.7f},{origin_lon:.7f}->{dest_lat:.7f},{dest_lon:.7f}:{profile}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_cached(cache_key: str) -> RouteCache | None:
    """Look up a fresh route cache entry."""
    now = timezone.now()
    entry = RouteCache.objects.filter(cache_key=cache_key).first()
    if entry is None:
        return None
    if entry.expires_at is not None and entry.expires_at <= now:
        return None
    return entry


def _save_cache(
    cache_key: str,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    profile: str,
    distance_miles: float,
    duration_seconds: float,
    geometry: dict,
    ttl_hours: int,
) -> RouteCache:
    """Persist a route result to the cache."""
    expires_at = timezone.now() + timedelta(hours=ttl_hours) if ttl_hours > 0 else None

    defaults = {
        "origin_latitude": origin_lat,
        "origin_longitude": origin_lon,
        "destination_latitude": dest_lat,
        "destination_longitude": dest_lon,
        "profile": profile,
        "provider_name": "OSRM",
        "distance_miles": distance_miles,
        "duration_seconds": duration_seconds,
        "geometry": geometry,
        "expires_at": expires_at,
    }

    entry, _ = RouteCache.objects.update_or_create(
        cache_key=cache_key,
        defaults=defaults,
    )
    return entry


def _validate_geometry(geometry: dict) -> None:
    """Validate that the geometry is a GeoJSON LineString with >= 2 points."""
    if not isinstance(geometry, dict):
        raise ProviderError("OSRM geometry is not a dict")
    if geometry.get("type") != "LineString":
        raise ProviderError(
            f"OSRM geometry type is not LineString: {geometry.get('type')}"
        )
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ProviderError("OSRM geometry has fewer than 2 coordinates")
    for point in coords:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ProviderError("OSRM geometry contains invalid coordinate")
        lon, lat = point[0], point[1]
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            raise ProviderError("OSRM geometry contains non-numeric coordinate")
        if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
            raise ProviderError("OSRM geometry coordinate out of range")


def _make_request(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    profile: str,
    client: httpx.Client,
) -> dict:
    """Make a single OSRM route request with retries."""
    base_url = settings.OSRM_BASE_URL.rstrip("/")
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "alternatives": "false",
    }

    max_retries = 2
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.get(
                f"{base_url}/route/v1/{profile}/{coords}",
                params=params,
                timeout=(
                    settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
                    settings.PROVIDER_READ_TIMEOUT_SECONDS,
                ),
            )
            if response.status_code >= 500 and attempt < max_retries:
                wait = min(2**attempt, 4)
                logger.warning(
                    "OSRM 5xx on attempt %d, retrying in %ds", attempt + 1, wait
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
                    "OSRM request error on attempt %d: %s, retrying in %ds",
                    attempt + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
            else:
                break

    raise ProviderError(f"OSRM request failed: {last_error}")


def get_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    profile: str = "driving",
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> RouteResult:
    """Get a driving route between two points.

    Uses the persistent cache when available. Makes at most one OSRM
    request on a cache miss.
    """
    cache_key = _make_cache_key(origin_lat, origin_lon, dest_lat, dest_lon, profile)

    if use_cache:
        cached = _get_cached(cache_key)
        if cached is not None:
            logger.info("Route cache hit for key %s...", cache_key[:12])
            return RouteResult(
                distance_miles=cached.distance_miles,
                duration_seconds=cached.duration_seconds,
                geometry=cached.geometry,
                cached=True,
                provider_name=cached.provider_name,
            )

    logger.info("Route cache miss for key %s...", cache_key[:12])

    close_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=(
                settings.PROVIDER_CONNECT_TIMEOUT_SECONDS,
                settings.PROVIDER_READ_TIMEOUT_SECONDS,
            )
        )

    try:
        data = _make_request(
            origin_lat, origin_lon, dest_lat, dest_lon, profile, client
        )
    finally:
        if close_client:
            client.close()

    # Validate OSRM response
    code = data.get("code")
    if code != "Ok":
        if code == "NoRoute":
            raise NoRouteError(
                "OSRM could not find a route between the specified locations"
            )
        raise ProviderError(f"OSRM returned error code: {code}")

    routes = data.get("routes")
    if not routes or not isinstance(routes, list) or len(routes) == 0:
        raise ProviderError("OSRM response contains no routes")

    route = routes[0]
    distance_m = route.get("distance")
    duration_s = route.get("duration")
    geometry = route.get("geometry")

    if distance_m is None or duration_s is None:
        raise ProviderError("OSRM route missing distance or duration")
    if distance_m <= 0:
        raise ProviderError("OSRM route has zero or negative distance")

    _validate_geometry(geometry)

    distance_miles = distance_m / METERS_PER_MILE

    result = RouteResult(
        distance_miles=distance_miles,
        duration_seconds=duration_s,
        geometry=geometry,
        cached=False,
        provider_name="OSRM",
    )

    _save_cache(
        cache_key,
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        profile,
        distance_miles,
        duration_s,
        geometry,
        settings.ROUTE_CACHE_TTL_HOURS,
    )
    return result
