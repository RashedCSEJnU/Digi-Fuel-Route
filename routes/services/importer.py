"""CSV parsing, normalization, and deduplication for fuel price data."""

import csv
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

EXPECTED_HEADERS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]


def normalize_whitespace(value: Any) -> str:
    """Trim and collapse internal whitespace."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_state(value: Any) -> str:
    """Uppercase and strip state code."""
    return normalize_whitespace(value).upper()


def normalize_identity(
    opis_id: str, rack_id: str, address: str, city: str, state: str
) -> str:
    """Build a normalized identity key for deduplication.

    Prefer OPIS ID + Rack ID when both are present; fall back to
    normalized address/city/state.
    """
    opis_id = normalize_whitespace(opis_id)
    rack_id = normalize_whitespace(rack_id)
    if opis_id and rack_id:
        return f"opis:{opis_id}:rack:{rack_id}"
    addr = normalize_whitespace(address).lower()
    cty = normalize_whitespace(city).lower()
    st = normalize_state(state)
    return f"addr:{addr}:{cty}:{st}"


def parse_price(raw: Any) -> Decimal | None:
    """Parse a retail price string into a Decimal, or None if invalid."""
    if raw is None:
        return None
    text = normalize_whitespace(raw)
    if not text:
        return None
    try:
        price = Decimal(text)
    except InvalidOperation:
        return None
    if price <= 0:
        return None
    return price


@dataclass
class ParsedRow:
    """A single normalized CSV row ready for deduplication."""

    opis_truckstop_id: str
    rack_id: str
    name: str
    address: str
    city: str
    state: str
    postal_code: str
    retail_price: Decimal | None
    normalized_identity: str
    source_row_number: int
    is_valid: bool
    invalid_reason: str = ""


@dataclass
class ImportReport:
    """Summary of an import run."""

    source: str = ""
    rows_read: int = 0
    rows_accepted: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_duplicate: int = 0
    rows_invalid: int = 0
    rows_skipped_cache: int = 0
    geocode_resolved: int = 0
    geocode_unresolved: int = 0
    geocode_failed: int = 0
    malformed_samples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "rows_read": self.rows_read,
            "rows_accepted": self.rows_accepted,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "rows_duplicate": self.rows_duplicate,
            "rows_invalid": self.rows_invalid,
            "rows_skipped_cache": self.rows_skipped_cache,
            "geocode_resolved": self.geocode_resolved,
            "geocode_unresolved": self.geocode_unresolved,
            "geocode_failed": self.geocode_failed,
            "malformed_samples": self.malformed_samples,
        }


def parse_csv_rows(
    file_path: str,
    limit: int | None = None,
) -> tuple[list[ParsedRow], list[dict]]:
    """Parse and normalize CSV rows.

    Returns (parsed_rows, malformed_samples).
    """
    parsed: list[ParsedRow] = []
    malformed: list[dict] = []

    with open(file_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return parsed, [{"error": "empty_file"}]

        # Validate headers
        actual = [normalize_whitespace(h) for h in reader.fieldnames]
        expected = [normalize_whitespace(h) for h in EXPECTED_HEADERS]
        if actual != expected:
            return parsed, [
                {"error": "header_mismatch", "expected": expected, "actual": actual}
            ]

        for row_num, raw in enumerate(reader, start=2):
            if limit is not None and len(parsed) >= limit:
                break

            opis_id = normalize_whitespace(raw.get("OPIS Truckstop ID", ""))
            rack_id = normalize_whitespace(raw.get("Rack ID", ""))
            name = normalize_whitespace(raw.get("Truckstop Name", ""))
            address = normalize_whitespace(raw.get("Address", ""))
            city = normalize_whitespace(raw.get("City", ""))
            state = normalize_state(raw.get("State", ""))
            price_raw = raw.get("Retail Price", "")

            identity = normalize_identity(opis_id, rack_id, address, city, state)
            price = parse_price(price_raw)

            is_valid = True
            invalid_reason = ""
            if not name and not address:
                is_valid = False
                invalid_reason = "missing_name_and_address"
            elif price is None:
                is_valid = False
                invalid_reason = "invalid_price"

            parsed.append(
                ParsedRow(
                    opis_truckstop_id=opis_id,
                    rack_id=rack_id,
                    name=name,
                    address=address,
                    city=city,
                    state=state,
                    postal_code="",
                    retail_price=price,
                    normalized_identity=identity,
                    source_row_number=row_num,
                    is_valid=is_valid,
                    invalid_reason=invalid_reason,
                )
            )

    return parsed, malformed


def deduplicate_rows(rows: list[ParsedRow]) -> tuple[list[ParsedRow], int]:
    """Consolidate duplicate identities, keeping the cheapest valid price.

    Returns (unique_rows, duplicate_count).
    """
    best: dict[str, ParsedRow] = {}
    duplicate_count = 0

    for row in rows:
        if not row.is_valid:
            continue
        key = row.normalized_identity
        if key not in best:
            best[key] = row
        else:
            duplicate_count += 1
            existing = best[key]
            # Keep the cheaper price; tie-break on lower source row number
            if row.retail_price is not None and (
                existing.retail_price is None
                or row.retail_price < existing.retail_price
                or (
                    row.retail_price == existing.retail_price
                    and row.source_row_number < existing.source_row_number
                )
            ):
                best[key] = row

    return list(best.values()), duplicate_count
