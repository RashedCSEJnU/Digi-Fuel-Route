"""Tests for CSV parsing, normalization, and deduplication."""

import os
from decimal import Decimal

from routes.services.importer import (
    deduplicate_rows,
    normalize_identity,
    normalize_state,
    normalize_whitespace,
    parse_csv_rows,
    parse_price,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestNormalizeWhitespace:
    def test_collapses_internal_whitespace(self):
        assert normalize_whitespace("  hello   world  ") == "hello world"

    def test_handles_none(self):
        assert normalize_whitespace(None) == ""

    def test_handles_empty(self):
        assert normalize_whitespace("") == ""


class TestNormalizeState:
    def test_uppercases(self):
        assert normalize_state("az") == "AZ"

    def test_strips(self):
        assert normalize_state("  ca  ") == "CA"


class TestNormalizeIdentity:
    def test_opis_rack_preferred(self):
        identity = normalize_identity("7", "307", "Some Address", "Some City", "OK")
        assert identity == "opis:7:rack:307"

    def test_fallback_to_address(self):
        identity = normalize_identity("", "", "123 Main St", "Phoenix", "AZ")
        assert identity == "addr:123 main st:phoenix:AZ"

    def test_case_insensitive_fallback(self):
        identity = normalize_identity("", "", "123 MAIN ST", "PHOENIX", "az")
        assert identity == "addr:123 main st:phoenix:AZ"


class TestParsePrice:
    def test_valid_price(self):
        assert parse_price("3.00733333") == Decimal("3.00733333")

    def test_zero_price(self):
        assert parse_price("0") is None

    def test_negative_price(self):
        assert parse_price("-1.50") is None

    def test_non_numeric(self):
        assert parse_price("abc") is None

    def test_empty(self):
        assert parse_price("") is None

    def test_none(self):
        assert parse_price(None) is None


class TestParseCsvRows:
    def test_sample_csv(self):
        path = os.path.join(FIXTURES, "sample.csv")
        rows, malformed = parse_csv_rows(path)
        assert len(rows) == 6
        assert len(malformed) == 0

    def test_bom_csv(self):
        path = os.path.join(FIXTURES, "sample_bom.csv")
        rows, _ = parse_csv_rows(path)
        assert len(rows) == 1
        assert rows[0].name == "TEST STOP"

    def test_invalid_rows(self):
        path = os.path.join(FIXTURES, "sample_invalid.csv")
        rows, _ = parse_csv_rows(path)
        assert len(rows) == 6
        valid = [r for r in rows if r.is_valid]
        invalid = [r for r in rows if not r.is_valid]
        assert len(valid) == 2
        assert len(invalid) == 4

    def test_limit(self):
        path = os.path.join(FIXTURES, "sample.csv")
        rows, _ = parse_csv_rows(path, limit=3)
        assert len(rows) == 3

    def test_header_mismatch(self):
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Wrong,Headers,Here\n1,2,3\n")
            f.flush()
            rows, malformed = parse_csv_rows(f.name)
        assert len(rows) == 0
        assert len(malformed) == 1
        assert malformed[0]["error"] == "header_mismatch"
        os.unlink(f.name)


class TestDeduplicateRows:
    def test_deduplicates_same_identity(self):
        path = os.path.join(FIXTURES, "sample.csv")
        rows, _ = parse_csv_rows(path)
        valid = [r for r in rows if r.is_valid]
        unique, dup_count = deduplicate_rows(valid)
        # 6 rows, 2 duplicates (PILOT #1243 and CHEAP STOP)
        assert len(unique) == 4
        assert dup_count == 2

    def test_keeps_cheapest_price(self):
        path = os.path.join(FIXTURES, "sample.csv")
        rows, _ = parse_csv_rows(path)
        valid = [r for r in rows if r.is_valid]
        unique, _ = deduplicate_rows(valid)
        # Find the CHEAP STOP entry
        cheap = [r for r in unique if "CHEAP" in r.name]
        assert len(cheap) == 1
        assert cheap[0].retail_price == Decimal("2.99")

    def test_invalid_rows_excluded(self):
        path = os.path.join(FIXTURES, "sample_invalid.csv")
        rows, _ = parse_csv_rows(path)
        valid = [r for r in rows if r.is_valid]
        unique, dup_count = deduplicate_rows(valid)
        assert len(unique) == 2
        assert dup_count == 0
