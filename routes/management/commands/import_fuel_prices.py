"""Management command to import fuel price CSV data."""

import json
import logging
import os

import httpx
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routes.models import FuelStation
from routes.services.geocoding import geocode_station_address
from routes.services.importer import (
    ImportReport,
    deduplicate_rows,
    parse_csv_rows,
)

logger = logging.getLogger("routes.import")


class Command(BaseCommand):
    help = "Import fuel prices from CSV, with optional geocoding."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the CSV file")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report without writing to the database",
        )
        parser.add_argument(
            "--geocode",
            action="store_true",
            help="Geocode unresolved station addresses via Nominatim",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Skip stations that already have resolved coordinates",
        )
        parser.add_argument(
            "--report",
            type=str,
            default=None,
            help="Path to write a JSON import report",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit number of CSV rows to process (for testing)",
        )

    def handle(self, *args, **options):
        csv_file = options["csv_file"]
        dry_run = options["dry_run"]
        do_geocode = options["geocode"]
        resume = options["resume"]
        report_path = options["report"]
        limit = options["limit"]

        if not os.path.isfile(csv_file):
            raise CommandError(f"CSV file not found: {csv_file}")

        report = ImportReport(source=csv_file)

        self.stdout.write(f"Parsing {csv_file}...")
        parsed_rows, malformed = parse_csv_rows(csv_file, limit=limit)
        report.rows_read = len(parsed_rows)
        report.malformed_samples = malformed[:10]

        if malformed and not parsed_rows:
            raise CommandError(f"CSV header mismatch or empty file: {malformed[0]}")

        # Separate valid and invalid
        valid_rows = [r for r in parsed_rows if r.is_valid]
        invalid_rows = [r for r in parsed_rows if not r.is_valid]
        report.rows_invalid = len(invalid_rows)

        # Deduplicate
        unique_rows, dup_count = deduplicate_rows(valid_rows)
        report.rows_duplicate = dup_count
        report.rows_accepted = len(unique_rows)

        self.stdout.write(
            f"Read {report.rows_read} rows, "
            f"{report.rows_accepted} unique valid, "
            f"{report.rows_duplicate} duplicates, "
            f"{report.rows_invalid} invalid"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database changes"))
            if report_path:
                self._write_report(report, report_path)
            return

        # Persist stations
        self.stdout.write("Persisting stations...")
        with transaction.atomic():
            for row in unique_rows:
                _, created = self._upsert_station(row)
                if created:
                    report.rows_inserted += 1
                else:
                    report.rows_updated += 1

        # Geocode if requested
        if do_geocode:
            self.stdout.write("Geocoding unresolved stations...")
            self._geocode_stations(resume=resume, report=report)

        if report_path:
            self._write_report(report, report_path)

        self.stdout.write(
            self.style.SUCCESS(
                f"Import complete: {report.rows_inserted} inserted, "
                f"{report.rows_updated} updated, "
                f"{report.rows_duplicate} duplicates, "
                f"{report.rows_invalid} invalid"
            )
        )

    def _upsert_station(self, row) -> tuple[FuelStation, bool]:
        """Insert or update a station from a parsed row."""
        defaults = {
            "opis_truckstop_id": row.opis_truckstop_id,
            "rack_id": row.rack_id,
            "name": row.name,
            "address": row.address,
            "city": row.city,
            "state": row.state,
            "retail_price": row.retail_price,
            "source_row_number": row.source_row_number,
        }
        station, created = FuelStation.objects.update_or_create(
            normalized_identity=row.normalized_identity,
            defaults=defaults,
        )
        return station, created

    def _geocode_stations(self, resume: bool, report: ImportReport):
        """Geocode stations with pending or failed status."""
        queryset = FuelStation.objects.filter(
            latitude__isnull=True,
            longitude__isnull=True,
        )
        if resume:
            queryset = queryset.exclude(
                geocode_status=FuelStation.GeocodeStatus.RESOLVED
            )

        total = queryset.count()
        self.stdout.write(f"Geocoding {total} stations...")

        client = httpx.Client(
            timeout=(2.0, 8.0),
        )
        try:
            for i, station in enumerate(queryset.iterator(), start=1):
                if i % 10 == 0:
                    self.stdout.write(f"  {i}/{total}...")
                result = geocode_station_address(
                    address=station.address,
                    city=station.city,
                    state=station.state,
                    client=client,
                    delay_seconds=1.0,
                )
                if result is not None:
                    station.latitude = result.latitude
                    station.longitude = result.longitude
                    station.geocode_status = FuelStation.GeocodeStatus.RESOLVED
                    station.geocode_failure_reason = ""
                    report.geocode_resolved += 1
                else:
                    station.geocode_status = FuelStation.GeocodeStatus.UNRESOLVED
                    station.geocode_failure_reason = "not_found_or_non_us"
                    report.geocode_unresolved += 1
                station.save(
                    update_fields=[
                        "latitude",
                        "longitude",
                        "geocode_status",
                        "geocode_failure_reason",
                        "updated_at",
                    ]
                )
        finally:
            client.close()

    def _write_report(self, report: ImportReport, path: str):
        """Write the import report to a JSON file."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        self.stdout.write(f"Report written to {path}")
