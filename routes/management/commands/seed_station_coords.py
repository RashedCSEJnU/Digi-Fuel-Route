"""Management command to seed station coordinates from embedded US city lookup.

This is an offline, no-API-call geocoding approach that assigns city centroid
coordinates to stations using the US ZIP Code database bundled in this command.
It runs instantly and works without any external providers.

City-level accuracy is sufficient for route corridor filtering (station selection
within 10–20 miles of a route). The geocode_stations command can later refine
specific stations using Nominatim at the full address level.
"""

import json
import logging
import os

import httpx
from django.core.management.base import BaseCommand
from django.db import transaction

from routes.models import FuelStation

logger = logging.getLogger("routes.import")

# URL of the public US ZIP code database (MIT-licensed, OpenAddresses derivative)
US_CITIES_URL = (
    "https://raw.githubusercontent.com/millbj92/US-Zip-Codes-JSON/master/USCities.json"
)

# Local cache path for the downloaded data
_LOCAL_CACHE = os.path.join(
    os.path.dirname(__file__), "_us_city_coords_cache.json"
)


def _build_city_lookup(raw: list[dict]) -> dict[tuple[str, str], tuple[float, float]]:
    """Build (CITY_UPPER, STATE_UPPER) → (avg_lat, avg_lon) from raw ZIP data."""
    from collections import defaultdict

    bucket: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for entry in raw:
        try:
            lat = float(entry["latitude"])
            lon = float(entry["longitude"])
            city = str(entry.get("city", "")).strip().upper()
            state = str(entry.get("state", "")).strip().upper()
            if city and state:
                bucket[(city, state)].append((lat, lon))
        except (TypeError, ValueError, KeyError):
            pass

    return {
        key: (
            sum(c[0] for c in coords) / len(coords),
            sum(c[1] for c in coords) / len(coords),
        )
        for key, coords in bucket.items()
    }


def _load_or_download_lookup(stdout) -> dict[tuple[str, str], tuple[float, float]]:
    """Load the city lookup from local cache, or download and cache it."""
    if os.path.exists(_LOCAL_CACHE):
        stdout.write("Loading city coordinates from local cache...")
        with open(_LOCAL_CACHE) as fh:
            raw_lookup = json.load(fh)
        # Convert string keys back to tuples
        return {
            tuple(k.split("|", 1)): tuple(v)  # type: ignore[return-value]
            for k, v in raw_lookup.items()
        }

    stdout.write(f"Downloading US city coordinates from {US_CITIES_URL}...")
    client = httpx.Client(timeout=30.0)
    try:
        resp = client.get(US_CITIES_URL)
        resp.raise_for_status()
        raw = resp.json()
    finally:
        client.close()

    lookup = _build_city_lookup(raw)
    stdout.write(f"Built lookup with {len(lookup)} unique city+state entries.")

    # Cache to local file for subsequent runs
    serializable = {f"{city}|{state}": [lat, lon] for (city, state), (lat, lon) in lookup.items()}
    with open(_LOCAL_CACHE, "w") as fh:
        json.dump(serializable, fh)
    stdout.write(f"Saved cache to {_LOCAL_CACHE}")

    return lookup


class Command(BaseCommand):
    help = (
        "Seed fuel station coordinates from the US city centroid lookup. "
        "Zero API calls after the first run (data is cached locally). "
        "City-level accuracy; run geocode_stations afterwards for higher precision."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--state",
            type=str,
            default=None,
            help="Only seed stations in this state (e.g. IL)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite coordinates for already-geocoded stations",
        )

    def handle(self, *args, **options):
        state_filter = options["state"]
        force = options["force"]

        lookup = _load_or_download_lookup(self.stdout)

        qs = FuelStation.objects.all()
        if not force:
            qs = qs.filter(latitude__isnull=True)
        if state_filter:
            qs = qs.filter(state__iexact=state_filter)

        self.stdout.write(f"Seeding coordinates for {qs.count()} stations...")

        resolved = 0
        unresolved = 0

        with transaction.atomic():
            # Group by city+state to minimise DB round-trips
            city_state_pairs = qs.values_list("city", "state").distinct()
            for city, state in city_state_pairs:
                key = (city.strip().upper(), state.strip().upper())
                coords = lookup.get(key)
                station_qs = FuelStation.objects.filter(city=city, state=state)
                if not force:
                    station_qs = station_qs.filter(latitude__isnull=True)

                if coords:
                    lat, lon = coords
                    count = station_qs.update(
                        latitude=lat,
                        longitude=lon,
                        geocode_status=FuelStation.GeocodeStatus.RESOLVED,
                        geocode_failure_reason="",
                    )
                    resolved += count
                else:
                    count = station_qs.update(
                        geocode_status=FuelStation.GeocodeStatus.UNRESOLVED,
                        geocode_failure_reason="city_not_in_us_zip_database",
                    )
                    unresolved += count

        total = FuelStation.objects.filter(latitude__isnull=False).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Resolved {resolved} stations, {unresolved} unresolved. "
                f"Total geocoded: {total} / {FuelStation.objects.count()}"
            )
        )
