"""Management command to geocode fuel stations at city level for fast bulk resolution."""

import logging
import time

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routes.models import FuelStation

logger = logging.getLogger("routes.import")


class Command(BaseCommand):
    help = (
        "Geocode fuel stations at the city+state level (one Nominatim call per "
        "unique city, assigning the city centroid to all stations in that city). "
        "Much faster than full-address geocoding and sufficient for corridor filtering."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of unique cities to geocode (for testing)",
        )
        parser.add_argument(
            "--state",
            type=str,
            default=None,
            help="Only geocode stations in this state (e.g. IL)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.1,
            help="Delay between Nominatim requests in seconds (default: 1.1)",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        state_filter = options["state"]
        delay = options["delay"]

        qs = FuelStation.objects.filter(latitude__isnull=True)
        if state_filter:
            qs = qs.filter(state__iexact=state_filter)

        # Collect unique city+state pairs
        city_pairs = (
            qs.values_list("city", "state").distinct().order_by("state", "city")
        )
        if limit:
            city_pairs = city_pairs[:limit]

        city_list = list(city_pairs)
        self.stdout.write(
            f"Geocoding {len(city_list)} unique city+state pairs "
            f"(delay={delay}s per request)..."
        )

        base_url = settings.NOMINATIM_BASE_URL.rstrip("/")
        headers = {"User-Agent": settings.GEOCODER_USER_AGENT}
        city_coords: dict[tuple[str, str], tuple[float, float] | None] = {}

        client = httpx.Client(timeout=(2.0, 8.0))
        try:
            for idx, (city, state) in enumerate(city_list, 1):
                query = f"{city}, {state}, USA"
                try:
                    resp = client.get(
                        f"{base_url}/search",
                        params={
                            "q": query,
                            "format": "json",
                            "limit": 1,
                            "countrycodes": "us",
                            "addressdetails": 1,
                        },
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data:
                        lat = float(data[0]["lat"])
                        lon = float(data[0]["lon"])
                        city_coords[(city, state)] = (lat, lon)
                        self.stdout.write(f"  [{idx}/{len(city_list)}] {city}, {state}: {lat:.4f}, {lon:.4f}")
                    else:
                        city_coords[(city, state)] = None
                        self.stdout.write(
                            self.style.WARNING(f"  [{idx}/{len(city_list)}] {city}, {state}: NOT FOUND")
                        )
                except Exception as exc:  # noqa: BLE001
                    city_coords[(city, state)] = None
                    self.stdout.write(
                        self.style.ERROR(f"  [{idx}/{len(city_list)}] {city}, {state}: ERROR {exc}")
                    )
                finally:
                    if idx < len(city_list):
                        time.sleep(delay)
        finally:
            client.close()

        # Apply coordinates to stations
        resolved = 0
        unresolved = 0
        with transaction.atomic():
            for (city, state), coords in city_coords.items():
                station_qs = FuelStation.objects.filter(city=city, state=state, latitude__isnull=True)
                if coords:
                    lat, lon = coords
                    station_qs.update(
                        latitude=lat,
                        longitude=lon,
                        geocode_status=FuelStation.GeocodeStatus.RESOLVED,
                        geocode_failure_reason="",
                    )
                    count = station_qs.count()
                    resolved += count
                else:
                    station_qs.update(
                        geocode_status=FuelStation.GeocodeStatus.UNRESOLVED,
                        geocode_failure_reason="city_not_found",
                    )
                    count = station_qs.count()
                    unresolved += count

        total_with_coords = FuelStation.objects.filter(latitude__isnull=False).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Resolved {resolved} stations, {unresolved} unresolved. "
                f"Total geocoded stations: {total_with_coords}"
            )
        )
