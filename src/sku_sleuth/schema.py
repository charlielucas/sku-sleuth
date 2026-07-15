"""Versioned input schema contract and additive-drift evidence."""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from pathlib import Path

from sku_sleuth.models import dump_json, sha256_json

RAW_SCHEMA_VERSION = "1"
REQUIRED_RAW_FIELDS = ("row_id", "sku", "title")


def _field_evidence(fieldnames: tuple[str | None, ...]) -> tuple[tuple[str, ...], list[str]]:
    """Return strict, deterministic evidence labels for CSV header fields."""
    observed: list[str] = []
    unnamed: list[str] = []
    for index, field in enumerate(fieldnames, start=1):
        if field is not None and field.strip():
            observed.append(field)
            continue
        placeholder = f"__unnamed_column_{index}__"
        observed.append(placeholder)
        unnamed.append(placeholder)
    return tuple(observed), unnamed


class SchemaContractError(ValueError):
    """Raised after a failing schema report has been written."""

    def __init__(self, report: dict):
        self.report = report
        reasons = ", ".join(report["failures"])
        super().__init__(f"raw input violates schema contract: {reasons}")


def inspect_raw_csv_bytes(blob: bytes) -> dict:
    """Return contract evidence from one immutable read of the source bytes."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "schema_version": RAW_SCHEMA_VERSION,
            "raw_sha256": hashlib.sha256(blob).hexdigest(),
            "required_fields": list(REQUIRED_RAW_FIELDS),
            "observed_fields": [],
            "missing_required_fields": list(REQUIRED_RAW_FIELDS),
            "extra_fields": [],
            "drift_status": "breaking",
            "drift_events": [{"kind": "invalid_utf8"}],
            "row_count": 0,
            "duplicate_row_ids": [],
            "missing_value_counts": {},
            "null_value_counts": {},
            "effective_content_sha256": None,
            "failures": ["invalid_utf8"],
            "passed": False,
            "contract_sha256": sha256_json(
                {
                    "schema_version": RAW_SCHEMA_VERSION,
                    "required_fields": list(REQUIRED_RAW_FIELDS),
                }
            ),
        }
    reader = csv.DictReader(io.StringIO(text, newline=""))
    raw_observed = tuple(reader.fieldnames or ())
    observed, unnamed_fields = _field_evidence(raw_observed)
    rows = list(reader)

    missing = sorted(set(REQUIRED_RAW_FIELDS) - set(observed))
    extra = sorted(set(observed) - set(REQUIRED_RAW_FIELDS))
    duplicate_fields = sorted(field for field, count in Counter(observed).items() if count > 1)
    failures: list[str] = []
    if missing:
        failures.append("missing_required_fields")
    if not rows:
        failures.append("empty_input")
    if duplicate_fields:
        failures.append("duplicate_header_fields")
    if unnamed_fields:
        failures.append("unnamed_header_fields")
    if any(None in row for row in rows):
        failures.append("unlabeled_extra_values")

    row_ids: list[str] = []
    if "row_id" in observed:
        row_ids = [str(row.get("row_id") or "") for row in rows]
    id_counts = Counter(row_ids)
    duplicate_ids = sorted(rid for rid, count in id_counts.items() if rid and count > 1)
    if duplicate_ids:
        failures.append("duplicate_row_ids")
    if any(not rid.strip() for rid in row_ids):
        failures.append("missing_row_ids")

    missing_value_counts = {
        field: sum(row.get(field) is None for row in rows)
        for field in REQUIRED_RAW_FIELDS
        if field in observed
    }
    null_value_counts = {
        field: sum(row.get(field) == "" for row in rows)
        for field in REQUIRED_RAW_FIELDS
        if field in observed
    }
    if any(missing_value_counts.values()):
        failures.append("missing_required_values")

    drift_status = (
        "breaking"
        if missing
        or duplicate_fields
        or unnamed_fields
        or "unlabeled_extra_values" in failures
        or "empty_input" in failures
        else ("additive" if extra else "none")
    )
    drift_events = [
        *({"kind": "missing_required_field", "field": field} for field in missing),
        *(
            {"kind": "additive_field", "field": field}
            for field in extra
            if field not in unnamed_fields
        ),
        *({"kind": "duplicate_header_field", "field": field} for field in duplicate_fields),
        *({"kind": "unnamed_header_field", "field": field} for field in unnamed_fields),
    ]
    if "unlabeled_extra_values" in failures:
        drift_events.append({"kind": "unlabeled_extra_values"})
    effective_content_sha256 = None
    if (
        not missing
        and not duplicate_fields
        and not unnamed_fields
        and not any(missing_value_counts.values())
    ):
        effective_content_sha256 = sha256_json(
            sorted(
                ({field: row[field] for field in REQUIRED_RAW_FIELDS} for row in rows),
                key=lambda row: row["row_id"],
            )
        )
    report = {
        "schema_version": RAW_SCHEMA_VERSION,
        "raw_sha256": hashlib.sha256(blob).hexdigest(),
        "required_fields": list(REQUIRED_RAW_FIELDS),
        "observed_fields": list(observed),
        "missing_required_fields": missing,
        "extra_fields": extra,
        "drift_status": drift_status,
        "drift_events": drift_events,
        "row_count": len(rows),
        "duplicate_row_ids": duplicate_ids,
        "missing_value_counts": missing_value_counts,
        "null_value_counts": null_value_counts,
        "effective_content_sha256": effective_content_sha256,
        "failures": failures,
        "passed": not failures,
    }
    report["contract_sha256"] = sha256_json(
        {
            "schema_version": RAW_SCHEMA_VERSION,
            "required_fields": list(REQUIRED_RAW_FIELDS),
        }
    )
    return report


def inspect_raw_csv(path: Path) -> dict:
    """Return deterministic contract evidence without decoding any rows."""
    path = Path(path)
    return inspect_raw_csv_bytes(path.read_bytes())


def validate_raw_csv(path: Path, report_path: Path) -> dict:
    report = inspect_raw_csv(path)
    dump_json(report, report_path)
    if not report["passed"]:
        raise SchemaContractError(report)
    return report
