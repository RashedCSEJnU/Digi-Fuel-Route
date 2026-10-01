"""Tests for the import_fuel_prices management command."""

import json
import os
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from routes.models import FuelStation

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.mark.django_db
class TestImportCommand:
    def test_dry_run_no_db_changes(self):
        out = StringIO()
        call_command(
            "import_fuel_prices",
            os.path.join(FIXTURES, "sample.csv"),
            "--dry-run",
            stdout=out,
        )
        assert FuelStation.objects.count() == 0
        assert "DRY RUN" in out.getvalue()

    def test_import_creates_stations(self):
        out = StringIO()
        call_command(
            "import_fuel_prices",
            os.path.join(FIXTURES, "sample.csv"),
            stdout=out,
        )
        assert FuelStation.objects.count() == 4  # 6 rows - 2 duplicates

    def test_idempotent_reimport(self):
        path = os.path.join(FIXTURES, "sample.csv")
        call_command("import_fuel_prices", path, stdout=StringIO())
        count_after_first = FuelStation.objects.count()
        call_command("import_fuel_prices", path, stdout=StringIO())
        count_after_second = FuelStation.objects.count()
        assert count_after_first == count_after_second

    def test_report_file(self, tmp_path):
        report_path = tmp_path / "report.json"
        call_command(
            "import_fuel_prices",
            os.path.join(FIXTURES, "sample.csv"),
            "--report",
            str(report_path),
            stdout=StringIO(),
        )
        report = json.loads(report_path.read_text())
        assert report["rows_read"] == 6
        assert report["rows_accepted"] == 4
        assert report["rows_duplicate"] == 2
        assert report["rows_invalid"] == 0

    def test_limit_option(self):
        out = StringIO()
        call_command(
            "import_fuel_prices",
            os.path.join(FIXTURES, "sample.csv"),
            "--limit",
            "3",
            stdout=out,
        )
        assert FuelStation.objects.count() == 3

    def test_missing_file_raises(self):
        with pytest.raises(CommandError):
            call_command(
                "import_fuel_prices", "/nonexistent/path.csv", stdout=StringIO()
            )

    def test_invalid_rows_reported(self):
        report_path = "/tmp/test_import_report.json"
        call_command(
            "import_fuel_prices",
            os.path.join(FIXTURES, "sample_invalid.csv"),
            "--report",
            report_path,
            stdout=StringIO(),
        )
        with open(report_path) as fh:
            report = json.load(fh)
        assert report["rows_invalid"] == 4
        assert report["rows_accepted"] == 2
        os.unlink(report_path)
