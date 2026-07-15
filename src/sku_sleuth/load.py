"""Bundle verification, serialized loading, lineage, and reconciliation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sku_sleuth.compare import compare_batches
from sku_sleuth.models import Decode, sha256_json, sha256_jsonl_rows
from sku_sleuth.schema import inspect_raw_csv_bytes
from sku_sleuth.validate import validate_decode

LOAD_VERSION = "3"
DECODE_REQUIRED = {
    "row_id",
    "sku",
    "category",
    "subcategory",
    "attributes",
    "is_winter_rated",
    "tier",
    "evidence",
}
REJECT_REQUIRED = {"row_id", "sku", "title", "bucket", "reason"}
REJECT_BUCKETS = {"abstained", "quarantined", "errored"}
THRESHOLD_KEYS = {
    "flag_precision",
    "category_accuracy",
    "coverage",
    "error_rows",
    "quarantine_rate",
}
SCHEMA_PROJECTION_FIELDS = (
    "passed",
    "missing_required_fields",
    "extra_fields",
    "drift_status",
    "drift_events",
    "missing_value_counts",
    "null_value_counts",
    "effective_content_sha256",
    "row_count",
)
DRIFT_STATUSES = {"none", "additive", "breaking"}
DRIFT_EVENT_FIELDS = {
    "missing_required_field": True,
    "additive_field": True,
    "duplicate_header_field": True,
    "unlabeled_extra_values": False,
    "unnamed_header_field": True,
    "invalid_utf8": False,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, loaded_at TEXT NOT NULL, inserted INTEGER NOT NULL,
    decoded_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (
    row_id TEXT PRIMARY KEY, sku TEXT NOT NULL, category TEXT NOT NULL,
    subcategory TEXT NOT NULL, attributes_json TEXT NOT NULL,
    is_winter_rated INTEGER NOT NULL CHECK (is_winter_rated IN (0, 1)),
    tier TEXT NOT NULL, evidence TEXT NOT NULL, batch_id TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id));
CREATE TABLE IF NOT EXISTS batch_evidence (
    batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
    eval_report_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
    decoded_sha256 TEXT NOT NULL, rejects_sha256 TEXT NOT NULL,
    schema_report_sha256 TEXT NOT NULL, gold_sha256 TEXT NOT NULL,
    incoming_rows INTEGER NOT NULL, snapshot_rows INTEGER NOT NULL,
    batch_rows_sha256 TEXT NOT NULL, outcome_rows INTEGER NOT NULL,
    outcomes_sha256 TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id));
CREATE TABLE IF NOT EXISTS gate_decisions (
    decision_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, decided_at TEXT NOT NULL,
    gate_report_sha256 TEXT NOT NULL, thresholds_sha256 TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id));
CREATE TABLE IF NOT EXISTS batch_lineage (
    batch_id TEXT PRIMARY KEY, parent_batch_id TEXT, added INTEGER NOT NULL,
    removed INTEGER NOT NULL, changed INTEGER NOT NULL, unchanged INTEGER NOT NULL,
    comparison_sha256 TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id),
    FOREIGN KEY(parent_batch_id) REFERENCES batches(batch_id));
CREATE TABLE IF NOT EXISTS batch_rows (
    batch_id TEXT NOT NULL, row_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    origin_batch_id TEXT NOT NULL, PRIMARY KEY(batch_id, row_id),
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id),
    FOREIGN KEY(origin_batch_id) REFERENCES batches(batch_id));
CREATE TABLE IF NOT EXISTS batch_outcomes (
    batch_id TEXT NOT NULL, row_id TEXT NOT NULL, state TEXT NOT NULL,
    payload_json TEXT NOT NULL, PRIMARY KEY(batch_id, row_id),
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id));
CREATE TABLE IF NOT EXISTS load_reconciliation (
    batch_id TEXT PRIMARY KEY, reconciled_at TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    expected_rows INTEGER NOT NULL, verified_rows INTEGER NOT NULL,
    missing_rows INTEGER NOT NULL, unexpected_rows INTEGER NOT NULL,
    payload_mutations INTEGER NOT NULL, provenance_mutations INTEGER NOT NULL,
    database_schema_mutations INTEGER NOT NULL, metadata_mutations INTEGER NOT NULL,
    source_rowset_sha256 TEXT NOT NULL, database_rowset_sha256 TEXT NOT NULL,
    batch_rows_sha256 TEXT NOT NULL, outcome_rows INTEGER NOT NULL,
    outcomes_sha256 TEXT NOT NULL, decision_count INTEGER NOT NULL,
    decisions_sha256 TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id));
"""

_EXPECTED_SCHEMA_COLUMNS = {
    "batches": ("batch_id", "loaded_at", "inserted", "decoded_sha256"),
    "products": (
        "row_id",
        "sku",
        "category",
        "subcategory",
        "attributes_json",
        "is_winter_rated",
        "tier",
        "evidence",
        "batch_id",
    ),
    "batch_evidence": (
        "batch_id",
        "run_id",
        "manifest_sha256",
        "eval_report_sha256",
        "raw_sha256",
        "decoded_sha256",
        "rejects_sha256",
        "schema_report_sha256",
        "gold_sha256",
        "incoming_rows",
        "snapshot_rows",
        "batch_rows_sha256",
        "outcome_rows",
        "outcomes_sha256",
    ),
    "gate_decisions": (
        "decision_id",
        "batch_id",
        "decided_at",
        "gate_report_sha256",
        "thresholds_sha256",
        "passed",
    ),
    "batch_lineage": (
        "batch_id",
        "parent_batch_id",
        "added",
        "removed",
        "changed",
        "unchanged",
        "comparison_sha256",
    ),
    "batch_rows": ("batch_id", "row_id", "payload_sha256", "origin_batch_id"),
    "batch_outcomes": ("batch_id", "row_id", "state", "payload_json"),
    "load_reconciliation": (
        "batch_id",
        "reconciled_at",
        "passed",
        "expected_rows",
        "verified_rows",
        "missing_rows",
        "unexpected_rows",
        "payload_mutations",
        "provenance_mutations",
        "database_schema_mutations",
        "metadata_mutations",
        "source_rowset_sha256",
        "database_rowset_sha256",
        "batch_rows_sha256",
        "outcome_rows",
        "outcomes_sha256",
        "decision_count",
        "decisions_sha256",
    ),
}


class BundleError(ValueError):
    def __init__(self, reason: str, details=None):
        self.reason = reason
        self.details = details
        super().__init__(reason)


def _refused(reason: str, batch_id: str | None = None, details=None) -> dict:
    result = {"status": "refused", "reason": reason, "inserted": 0, "batch_id": batch_id}
    if details is not None:
        result["details"] = details
    return result


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError as exc:
        raise BundleError(f"missing_{label}") from exc
    except OSError as exc:
        raise BundleError(f"unreadable_{label}", str(exc)) from exc


def _read_json(blob: bytes, label: str):
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleError(f"malformed_{label}") from exc


def _read_jsonl(blob: bytes, label: str) -> list[dict]:
    rows: list[dict] = []
    try:
        for line in blob.decode().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise BundleError(f"malformed_{label}", "each JSONL value must be an object")
            rows.append(row)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleError(f"malformed_{label}") from exc
    return rows


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise BundleError(f"malformed_{label}", "expected an object")
    return value


def _list(value, label: str) -> list:
    if not isinstance(value, list):
        raise BundleError(f"malformed_{label}", "expected an array")
    return value


def _string(value, label: str, *, sha256: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise BundleError(f"malformed_{label}", "expected a non-empty string")
    if sha256 and (len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
        raise BundleError(f"malformed_{label}", "expected a lowercase SHA-256 digest")
    return value


def _integer(value, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise BundleError(f"malformed_{label}", f"expected an integer >= {minimum}")
    return value


def _boolean(value, label: str) -> bool:
    if type(value) is not bool:
        raise BundleError(f"malformed_{label}", "expected a boolean")
    return value


def _number(value, label: str, *, minimum: float | None = None) -> int | float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise BundleError(f"malformed_{label}", "expected a finite number")
    if minimum is not None and value < minimum:
        raise BundleError(f"malformed_{label}", f"expected a number >= {minimum}")
    return value


def _strings(value, label: str) -> list[str]:
    values = _list(value, label)
    for index, item in enumerate(values):
        if not isinstance(item, str):
            raise BundleError(f"malformed_{label}[{index}]", "expected a string")
    return values


def _integer_map(value, label: str) -> dict:
    values = _object(value, label)
    for name, item in values.items():
        if not isinstance(name, str):
            raise BundleError(f"malformed_{label}", "expected string keys")
        _integer(item, f"{label}.{name}")
    return values


def _hash_map(value, label: str, *, required: set[str] | None = None) -> dict:
    values = _object(value, label)
    if required is not None and not required <= set(values):
        raise BundleError(f"malformed_{label}", "missing required hashes")
    for name, item in values.items():
        if not isinstance(name, str):
            raise BundleError(f"malformed_{label}", "expected string keys")
        _string(item, f"{label}.{name}", sha256=True)
    return values


def _counts(value, label: str) -> dict:
    values = _object(value, label)
    for name in ("total", "decoded", "abstained", "quarantined", "errored"):
        _integer(values.get(name), f"{label}.{name}")
    return values


def _drift_status(value, label: str) -> str:
    status = _string(value, label)
    if status not in DRIFT_STATUSES:
        raise BundleError(f"malformed_{label}", "unknown drift status")
    return status


def _drift_events(value, label: str) -> list[dict]:
    events = _list(value, label)
    for index, event in enumerate(events):
        event_label = f"{label}[{index}]"
        event = _object(event, event_label)
        kind = _string(event.get("kind"), f"{event_label}.kind")
        if kind not in DRIFT_EVENT_FIELDS:
            raise BundleError(f"malformed_{event_label}.kind", "unknown drift event")
        expected_keys = {"kind", "field"} if DRIFT_EVENT_FIELDS[kind] else {"kind"}
        if set(event) != expected_keys:
            raise BundleError(f"malformed_{event_label}", "unexpected or missing event fields")
        if DRIFT_EVENT_FIELDS[kind]:
            _string(event.get("field"), f"{event_label}.field")
    return events


def _schema_projection(schema_report: dict) -> dict:
    return {name: schema_report[name] for name in SCHEMA_PROJECTION_FIELDS}


def _index_unique(rows: list[dict], label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        row_id = row.get("row_id")
        if not isinstance(row_id, str) or not row_id:
            raise BundleError("schema_contract_failed", f"{label}: invalid row_id")
        if row_id in out:
            raise BundleError("duplicate_row_id", {"artifact": label, "row_id": row_id})
        out[row_id] = row
    return out


def _validate_decoded(rows: list[dict]) -> list[str]:
    extras: set[str] = set()
    for row in rows:
        missing = DECODE_REQUIRED - set(row)
        if missing:
            raise BundleError(
                "schema_contract_failed",
                {"artifact": "decoded", "missing_fields": sorted(missing)},
            )
        extras.update(set(row) - DECODE_REQUIRED)
        string_fields = ("row_id", "sku", "category", "subcategory", "tier", "evidence")
        if any(not isinstance(row[key], str) or not row[key] for key in string_fields):
            raise BundleError("schema_contract_failed", "decoded string field is null or invalid")
        if not isinstance(row["attributes"], dict):
            raise BundleError("schema_contract_failed", "decoded attributes must be an object")
        if type(row["is_winter_rated"]) is not bool:
            raise BundleError("schema_contract_failed", "is_winter_rated must be a bool")
        decode = Decode(**{key: row[key] for key in DECODE_REQUIRED})
        errors = validate_decode(decode)
        if errors:
            raise BundleError(
                "schema_contract_failed",
                {"artifact": "decoded", "row_id": row["row_id"], "errors": errors},
            )
    return sorted(extras)


def _validate_rejects(rows: list[dict]) -> list[str]:
    extras: set[str] = set()
    for row in rows:
        missing = REJECT_REQUIRED - set(row)
        if missing:
            raise BundleError(
                "schema_contract_failed",
                {"artifact": "rejects", "missing_fields": sorted(missing)},
            )
        extras.update(set(row) - REJECT_REQUIRED)
        string_fields = ("row_id", "sku", "title", "bucket", "reason")
        if any(not isinstance(row[key], str) for key in string_fields):
            raise BundleError("schema_contract_failed", "reject string field is null or invalid")
        if row["bucket"] not in REJECT_BUCKETS:
            raise BundleError("schema_contract_failed", "unknown reject bucket")
    return sorted(extras)


def _validate_thresholds(thresholds) -> dict:
    thresholds = _object(thresholds, "thresholds")
    if set(thresholds) != THRESHOLD_KEYS:
        raise BundleError("malformed_thresholds", "unexpected or missing threshold names")
    for name, value in thresholds.items():
        if type(value) not in {int, float} or not math.isfinite(value):
            raise BundleError("malformed_thresholds", f"{name} must be a finite number")
    return thresholds


def _validate_report_shapes(schema_report, manifest, eval_report, gate) -> None:
    schema_report = _object(schema_report, "schema_report")
    _boolean(schema_report.get("passed"), "schema_report.passed")
    _string(schema_report.get("schema_version"), "schema_report.schema_version")
    _strings(schema_report.get("required_fields"), "schema_report.required_fields")
    _strings(schema_report.get("observed_fields"), "schema_report.observed_fields")
    _strings(
        schema_report.get("missing_required_fields"),
        "schema_report.missing_required_fields",
    )
    _strings(schema_report.get("extra_fields"), "schema_report.extra_fields")
    _drift_status(schema_report.get("drift_status"), "schema_report.drift_status")
    _drift_events(schema_report.get("drift_events"), "schema_report.drift_events")
    _integer(schema_report.get("row_count"), "schema_report.row_count")
    _strings(schema_report.get("duplicate_row_ids"), "schema_report.duplicate_row_ids")
    _integer_map(schema_report.get("missing_value_counts"), "schema_report.missing_value_counts")
    _integer_map(schema_report.get("null_value_counts"), "schema_report.null_value_counts")
    _strings(schema_report.get("failures"), "schema_report.failures")
    _string(schema_report.get("raw_sha256"), "schema_report.raw_sha256", sha256=True)
    _string(
        schema_report.get("effective_content_sha256"),
        "schema_report.effective_content_sha256",
        sha256=True,
    )
    _string(schema_report.get("contract_sha256"), "schema_report.contract_sha256", sha256=True)

    manifest = _object(manifest, "manifest")
    for name in ("batch_id", "run_id", "raw_sha256", "decoded_sha256", "rejects_sha256"):
        _string(manifest.get(name), f"manifest.{name}", sha256=True)
    _string(manifest.get("schema_report_sha256"), "manifest.schema_report_sha256", sha256=True)
    for name in ("decoder_version", "decode_schema_version", "ruleset"):
        _string(manifest.get(name), f"manifest.{name}")
    _integer(manifest.get("total"), "manifest.total")
    _counts(manifest.get("counts"), "manifest.counts")
    _integer_map(manifest.get("tier_invalid"), "manifest.tier_invalid")
    identity = _object(manifest.get("identity"), "manifest.identity")
    contract = _object(identity.get("contract"), "manifest.identity.contract")
    for name in ("decoder_version", "decode_schema_version", "raw_schema_version"):
        _string(contract.get(name), f"manifest.identity.contract.{name}")
    _hash_map(
        identity.get("inputs"),
        "manifest.identity.inputs",
        required={"raw_content_sha256", "catalog_content_sha256", "brands_content_sha256"},
    )
    configuration = _object(identity.get("configuration"), "manifest.identity.configuration")
    _string(configuration.get("ruleset"), "manifest.identity.configuration.ruleset")
    _object(configuration.get("model"), "manifest.identity.configuration.model")
    _hash_map(identity.get("implementation"), "manifest.identity.implementation")
    manifest_schema = _object(manifest.get("schema"), "manifest.schema")
    _boolean(manifest_schema.get("passed"), "manifest.schema.passed")
    _strings(
        manifest_schema.get("missing_required_fields"),
        "manifest.schema.missing_required_fields",
    )
    _strings(manifest_schema.get("extra_fields"), "manifest.schema.extra_fields")
    _drift_status(manifest_schema.get("drift_status"), "manifest.schema.drift_status")
    _drift_events(manifest_schema.get("drift_events"), "manifest.schema.drift_events")
    _integer_map(
        manifest_schema.get("missing_value_counts"), "manifest.schema.missing_value_counts"
    )
    _integer_map(manifest_schema.get("null_value_counts"), "manifest.schema.null_value_counts")
    _string(
        manifest_schema.get("effective_content_sha256"),
        "manifest.schema.effective_content_sha256",
        sha256=True,
    )
    _integer(manifest_schema.get("row_count"), "manifest.schema.row_count")

    eval_report = _object(eval_report, "eval_report")
    for name in (
        "batch_id",
        "decoded_sha256",
        "rejects_sha256",
        "manifest_sha256",
        "gold_sha256",
        "evaluation_input_sha256",
    ):
        _string(eval_report.get(name), f"eval_report.{name}", sha256=True)
    _string(eval_report.get("evaluation_version"), "eval_report.evaluation_version")
    _number(eval_report.get("coverage"), "eval_report.coverage", minimum=0)
    _number(eval_report.get("category_accuracy"), "eval_report.category_accuracy", minimum=0)
    _number(
        eval_report.get("conditional_category_accuracy"),
        "eval_report.conditional_category_accuracy",
        minimum=0,
    )
    _counts(eval_report.get("counts"), "eval_report.counts")
    _integer_map(eval_report.get("tier_invalid"), "eval_report.tier_invalid")
    flag = _object(eval_report.get("flag"), "eval_report.flag")
    for name in ("tp", "fp", "fn"):
        _integer(flag.get(name), f"eval_report.flag.{name}")
    for name in ("precision", "recall"):
        _number(flag.get(name), f"eval_report.flag.{name}", minimum=0)
    classification = _object(eval_report.get("classification"), "eval_report.classification")
    for name in ("expected_decode_rows", "decoded_expected_rows", "category_correct"):
        _integer(classification.get(name), f"eval_report.classification.{name}")
    for name in ("end_to_end_category_accuracy", "conditional_category_accuracy"):
        _number(classification.get(name), f"eval_report.classification.{name}", minimum=0)
    categories = _object(eval_report.get("categories"), "eval_report.categories")
    for category, stats in categories.items():
        _string(category, "eval_report.categories key")
        stats = _object(stats, f"eval_report.categories.{category}")
        for name in ("precision", "recall", "f1"):
            _number(stats.get(name), f"eval_report.categories.{category}.{name}", minimum=0)
        _integer(stats.get("support"), f"eval_report.categories.{category}.support")
    attributes = _object(eval_report.get("attributes"), "eval_report.attributes")
    for attribute, stats in attributes.items():
        _string(attribute, "eval_report.attributes key")
        stats = _object(stats, f"eval_report.attributes.{attribute}")
        _integer(stats.get("correct"), f"eval_report.attributes.{attribute}.correct")
        _integer(stats.get("total"), f"eval_report.attributes.{attribute}.total")
        _number(stats.get("accuracy"), f"eval_report.attributes.{attribute}.accuracy", minimum=0)
    tiers = _object(eval_report.get("tiers"), "eval_report.tiers")
    for tier, stats in tiers.items():
        _string(tier, "eval_report.tiers key")
        stats = _object(stats, f"eval_report.tiers.{tier}")
        for name in ("rows", "gold_rows", "gold_correct"):
            _integer(stats.get(name), f"eval_report.tiers.{tier}.{name}")
    for index, pair in enumerate(
        _list(eval_report.get("confusion_pairs"), "eval_report.confusion_pairs")
    ):
        pair = _list(pair, f"eval_report.confusion_pairs[{index}]")
        if len(pair) != 3:
            raise BundleError(
                f"malformed_eval_report.confusion_pairs[{index}]", "expected three values"
            )
        _string(pair[0], f"eval_report.confusion_pairs[{index}][0]")
        _string(pair[1], f"eval_report.confusion_pairs[{index}][1]")
        _integer(pair[2], f"eval_report.confusion_pairs[{index}][2]", minimum=1)
    gold = _object(eval_report.get("gold"), "eval_report.gold")
    for name in (
        "total",
        "decoded",
        "abstained",
        "quarantined",
        "unexpected_decodes",
        "correct_abstains",
    ):
        _integer(gold.get(name), f"eval_report.gold.{name}")

    gate = _object(gate, "gate_report")
    _boolean(gate.get("passed"), "gate_report.passed")
    _string(gate.get("batch_id"), "gate_report.batch_id", sha256=True)
    _string(gate.get("decoded_sha256"), "gate_report.decoded_sha256", sha256=True)
    binding = _object(gate.get("artifact_binding"), "gate_report.artifact_binding")
    for name in (
        "batch_id",
        "run_id",
        "raw_sha256",
        "decoded_sha256",
        "rejects_sha256",
        "schema_report_sha256",
        "manifest_sha256",
        "eval_report_sha256",
        "gold_sha256",
        "evaluation_input_sha256",
        "thresholds_sha256",
    ):
        _string(binding.get(name), f"gate_report.artifact_binding.{name}", sha256=True)
    checks = _list(gate.get("checks"), "gate_report.checks")
    if not checks:
        raise BundleError("malformed_gate_report.checks", "must not be empty")
    for index, check in enumerate(checks):
        check = _object(check, f"gate_report.checks[{index}]")
        _string(check.get("name"), f"gate_report.checks[{index}].name")
        _string(check.get("op"), f"gate_report.checks[{index}].op")
        _boolean(check.get("passed"), f"gate_report.checks[{index}].passed")
        for name in ("actual", "threshold"):
            _number(check.get(name), f"gate_report.checks[{index}].{name}")


def _validate_comparison_shape(comparison) -> dict:
    comparison = _object(comparison, "comparison_report")
    _string(comparison.get("comparison_version"), "comparison_report.comparison_version")
    for name in ("baseline_batch_id", "current_batch_id"):
        value = comparison.get(name)
        if value is not None:
            _string(value, f"comparison_report.{name}", sha256=True)
    for side in ("baseline", "current"):
        _hash_map(
            comparison.get(f"{side}_artifacts"),
            f"comparison_report.{side}_artifacts",
            required={"decoded_sha256", "rejects_sha256"},
        )
    for section, names in (
        ("counts", {"added", "removed", "changed", "unchanged"}),
        (
            "partition",
            {
                "baseline_total",
                "current_total",
                "baseline_identity",
                "current_identity",
                "union_total",
                "union_identity",
            },
        ),
        ("change_types", {"classification", "tier", "winter_flag"}),
    ):
        values = _object(comparison.get(section), f"comparison_report.{section}")
        if set(values) != names:
            raise BundleError(f"malformed_comparison_report.{section}", "unexpected keys")
        _integer_map(values, f"comparison_report.{section}")
    _integer_map(comparison.get("transitions"), "comparison_report.transitions")
    _integer_map(comparison.get("field_change_counts"), "comparison_report.field_change_counts")
    _strings(comparison.get("added_row_ids"), "comparison_report.added_row_ids")
    _strings(comparison.get("removed_row_ids"), "comparison_report.removed_row_ids")
    for index, row in enumerate(
        _list(comparison.get("changed_rows"), "comparison_report.changed_rows")
    ):
        row = _object(row, f"comparison_report.changed_rows[{index}]")
        _string(row.get("row_id"), f"comparison_report.changed_rows[{index}].row_id")
        changes = _object(row.get("changes"), f"comparison_report.changed_rows[{index}].changes")
        if not changes:
            raise BundleError(
                f"malformed_comparison_report.changed_rows[{index}].changes",
                "must not be empty",
            )
        for field, delta in changes.items():
            _string(field, f"comparison_report.changed_rows[{index}].changes key")
            delta = _object(
                delta,
                f"comparison_report.changed_rows[{index}].changes.{field}",
            )
            if set(delta) != {"before", "after"}:
                raise BundleError(
                    f"malformed_comparison_report.changed_rows[{index}].changes.{field}",
                    "expected before and after",
                )
    digest = _string(
        comparison.get("comparison_sha256"),
        "comparison_report.comparison_sha256",
        sha256=True,
    )
    payload = {name: value for name, value in comparison.items() if name != "comparison_sha256"}
    if digest != sha256_json(payload):
        raise BundleError("comparison_self_hash_mismatch")
    return comparison


def _verify_bundle(
    *,
    raw_path: Path,
    decoded_path: Path,
    rejects_path: Path,
    schema_report_path: Path,
    manifest_path: Path,
    eval_report_path: Path,
    gold_path: Path,
    gate_report_path: Path,
    thresholds: dict,
    comparison_report_path: Path | None,
) -> dict:
    thresholds = _validate_thresholds(thresholds)
    blobs = {
        "raw": _read_bytes(raw_path, "raw"),
        "decoded": _read_bytes(decoded_path, "decoded"),
        "rejects": _read_bytes(rejects_path, "rejects"),
        "schema_report": _read_bytes(schema_report_path, "schema_report"),
        "manifest": _read_bytes(manifest_path, "manifest"),
        "eval_report": _read_bytes(eval_report_path, "eval_report"),
        "gold": _read_bytes(gold_path, "gold"),
        "gate_report": _read_bytes(gate_report_path, "gate_report"),
    }
    decoded = _read_jsonl(blobs["decoded"], "decoded")
    rejects = _read_jsonl(blobs["rejects"], "rejects")
    gold = _read_jsonl(blobs["gold"], "gold")
    schema_report = _read_json(blobs["schema_report"], "schema_report")
    manifest = _read_json(blobs["manifest"], "manifest")
    eval_report = _read_json(blobs["eval_report"], "eval_report")
    gate = _read_json(blobs["gate_report"], "gate_report")
    _validate_report_shapes(schema_report, manifest, eval_report, gate)
    batch_id = gate["batch_id"]
    if gate["passed"] is not True:
        raise BundleError("gate_failed", batch_id)
    if not all(check["passed"] is True for check in gate["checks"]):
        raise BundleError("gate_verdict_inconsistent", batch_id)

    decoded_by_id = _index_unique(decoded, "decoded")
    rejects_by_id = _index_unique(rejects, "rejects")
    gold_by_id = _index_unique(gold, "gold")
    overlap = sorted(set(decoded_by_id) & set(rejects_by_id))
    if overlap:
        raise BundleError("decoded_reject_overlap", overlap[0])
    decoded_extras = _validate_decoded(decoded)
    rejects_extras = _validate_rejects(rejects)

    if schema_report["passed"] is not True:
        raise BundleError("schema_contract_failed", "raw schema report did not pass")
    expected_schema_report = inspect_raw_csv_bytes(blobs["raw"])
    if sha256_json(expected_schema_report) != sha256_json(schema_report):
        raise BundleError("schema_report_mismatch")
    expected_projection = _schema_projection(schema_report)
    manifest_projection = manifest["schema"]
    if sha256_json(manifest_projection) != sha256_json(expected_projection):
        mismatches = sorted(
            name
            for name in set(manifest_projection) | set(expected_projection)
            if name not in manifest_projection
            or name not in expected_projection
            or sha256_json(manifest_projection[name]) != sha256_json(expected_projection[name])
        )
        raise BundleError("schema_projection_mismatch", mismatches)
    raw_reader = csv.DictReader(io.StringIO(blobs["raw"].decode("utf-8"), newline=""))
    raw_by_id = {row["row_id"]: row for row in raw_reader}
    raw_ids = set(raw_by_id)
    outcome_ids = set(decoded_by_id) | set(rejects_by_id)
    missing_outcomes = sorted(raw_ids - outcome_ids)
    unexpected_outcomes = sorted(outcome_ids - raw_ids)
    if missing_outcomes or unexpected_outcomes:
        raise BundleError(
            "source_outcome_row_set_mismatch",
            {
                "missing_outcome_row_ids": missing_outcomes,
                "unexpected_outcome_row_ids": unexpected_outcomes,
            },
        )
    sku_mismatches = sorted(
        row_id
        for row_id in outcome_ids
        if (decoded_by_id.get(row_id) or rejects_by_id[row_id])["sku"] != raw_by_id[row_id]["sku"]
    )
    if sku_mismatches:
        raise BundleError("source_outcome_sku_mismatch", sku_mismatches)
    reject_title_mismatches = sorted(
        row_id
        for row_id, row in rejects_by_id.items()
        if row["title"] != raw_by_id[row_id]["title"]
    )
    if reject_title_mismatches:
        raise BundleError("source_reject_title_mismatch", reject_title_mismatches)
    missing_gold = sorted(set(gold_by_id) - raw_ids)
    if missing_gold:
        raise BundleError("source_gold_row_set_mismatch", missing_gold)
    gold_title_mismatches = sorted(
        row_id
        for row_id, row in gold_by_id.items()
        if row.get("title") != raw_by_id[row_id]["title"]
    )
    if gold_title_mismatches:
        raise BundleError("source_gold_title_mismatch", gold_title_mismatches)
    if schema_report["raw_sha256"] != _sha256(blobs["raw"]):
        raise BundleError("raw_hash_mismatch")
    if manifest["raw_sha256"] != _sha256(blobs["raw"]):
        raise BundleError("raw_hash_mismatch")
    if sha256_json(manifest["identity"]) != manifest["batch_id"]:
        raise BundleError("batch_identity_mismatch")
    manifest_contract = manifest["identity"]["contract"]
    manifest_configuration = manifest["identity"]["configuration"]
    projection_pairs = {
        "total": (manifest["total"], manifest["counts"]["total"]),
        "decoder_version": (manifest["decoder_version"], manifest_contract["decoder_version"]),
        "decode_schema_version": (
            manifest["decode_schema_version"],
            manifest_contract["decode_schema_version"],
        ),
        "ruleset": (manifest["ruleset"], manifest_configuration["ruleset"]),
        "raw_schema_version": (
            manifest_contract["raw_schema_version"],
            schema_report["schema_version"],
        ),
    }
    projection_mismatches = sorted(
        name
        for name, (observed, expected) in projection_pairs.items()
        if sha256_json(observed) != sha256_json(expected)
    )
    if projection_mismatches:
        raise BundleError("manifest_projection_mismatch", projection_mismatches)
    identity_inputs = _object(manifest["identity"].get("inputs"), "manifest.identity.inputs")
    if identity_inputs.get("raw_content_sha256") != expected_schema_report.get(
        "effective_content_sha256"
    ):
        raise BundleError("raw_identity_mismatch")

    actual = {
        "batch_id": manifest["batch_id"],
        "raw_sha256": _sha256(blobs["raw"]),
        "decoded_sha256": _sha256(blobs["decoded"]),
        "rejects_sha256": _sha256(blobs["rejects"]),
        "schema_report_sha256": sha256_json(schema_report),
        "manifest_sha256": sha256_json(manifest),
        "eval_report_sha256": sha256_json(eval_report),
        "gold_sha256": _sha256(blobs["gold"]),
        "thresholds_sha256": sha256_json(thresholds),
        "gate_report_sha256": sha256_json(gate),
    }
    binding = gate["artifact_binding"]
    for name in (
        "batch_id",
        "raw_sha256",
        "decoded_sha256",
        "rejects_sha256",
        "schema_report_sha256",
        "manifest_sha256",
        "eval_report_sha256",
        "gold_sha256",
        "thresholds_sha256",
    ):
        if binding[name] != actual[name]:
            raise BundleError("artifact_binding_mismatch", name)
    if gate["decoded_sha256"] != actual["decoded_sha256"]:
        raise BundleError("artifact_binding_mismatch", "decoded_sha256")
    for name in ("batch_id", "decoded_sha256", "rejects_sha256", "manifest_sha256", "gold_sha256"):
        if eval_report[name] != actual[name]:
            raise BundleError("evaluation_binding_mismatch", name)

    run_id = sha256_json(
        {
            "batch_id": manifest["batch_id"],
            "decoded_sha256": actual["decoded_sha256"],
            "rejects_sha256": actual["rejects_sha256"],
        }
    )
    if manifest["run_id"] != run_id:
        raise BundleError("run_identity_mismatch")
    if binding["run_id"] != run_id:
        raise BundleError("artifact_binding_mismatch", "run_id")

    counts = manifest["counts"]
    if counts["decoded"] != len(decoded):
        raise BundleError("count_mismatch", "decoded")
    bucket_counts = {bucket: 0 for bucket in REJECT_BUCKETS}
    for row in rejects:
        bucket_counts[row["bucket"]] += 1
    for bucket, count in bucket_counts.items():
        if counts[bucket] != count:
            raise BundleError("count_mismatch", bucket)
    if counts["total"] != len(decoded) + len(rejects):
        raise BundleError("count_mismatch", "total")

    from sku_sleuth.evaluate import evaluate_batch
    from sku_sleuth.gates import run_gate

    try:
        expected_eval = evaluate_batch(decoded, rejects, gold, manifest)
        expected_gate = run_gate(expected_eval, manifest, thresholds)
    except Exception as exc:  # converted to a structured refusal at the trust boundary
        raise BundleError("bundle_recompute_failed", f"{type(exc).__name__}: {exc}") from exc
    if sha256_json(expected_eval) != sha256_json(eval_report):
        raise BundleError("evaluation_report_mismatch")
    if sha256_json(expected_gate) != sha256_json(gate):
        raise BundleError("gate_report_mismatch")

    comparison = None
    if comparison_report_path is not None:
        comparison = _validate_comparison_shape(
            _read_json(
                _read_bytes(comparison_report_path, "comparison_report"), "comparison_report"
            )
        )

    decision_id = _expected_decision_id(
        batch_id=batch_id,
        run_id=run_id,
        manifest_sha256=actual["manifest_sha256"],
        eval_report_sha256=actual["eval_report_sha256"],
        raw_sha256=actual["raw_sha256"],
        decoded_sha256=actual["decoded_sha256"],
        rejects_sha256=actual["rejects_sha256"],
        schema_report_sha256=actual["schema_report_sha256"],
        gold_sha256=actual["gold_sha256"],
        gate_report_sha256=actual["gate_report_sha256"],
        thresholds_sha256=actual["thresholds_sha256"],
    )
    return {
        "batch_id": batch_id,
        "run_id": run_id,
        "decision_id": decision_id,
        "decoded": decoded,
        "rejects": rejects,
        "manifest": manifest,
        "actual": actual,
        "comparison": comparison,
        "schema_evidence": {
            "raw_extra_fields": schema_report.get("extra_fields", []),
            "decoded_extra_fields": decoded_extras,
            "rejects_extra_fields": rejects_extras,
        },
    }


def _payload(row: dict) -> dict:
    return {
        "row_id": row["row_id"],
        "sku": row["sku"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "attributes": row["attributes"],
        "is_winter_rated": row["is_winter_rated"],
        "tier": row["tier"],
        "evidence": row["evidence"],
    }


def _db_products(conn: sqlite3.Connection) -> tuple[dict[str, dict], dict[str, list[str]]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT row_id, sku, category, subcategory, attributes_json, "
        "is_winter_rated, tier, evidence, batch_id FROM products"
    ).fetchall()
    products: dict[str, dict] = {}
    schema_errors: dict[str, list[str]] = {}
    for row in rows:
        row_id = str(row["row_id"])
        errors: list[str] = []
        try:
            attributes = json.loads(row["attributes_json"])
            if not isinstance(attributes, dict):
                errors.append("attributes_json_not_object")
                attributes = {"__invalid_database_value__": row["attributes_json"]}
        except (json.JSONDecodeError, TypeError):
            errors.append("attributes_json_malformed")
            attributes = {"__invalid_database_value__": row["attributes_json"]}
        winter_value = row["is_winter_rated"]
        if type(winter_value) is int and winter_value in (0, 1):
            winter = winter_value == 1
        else:
            errors.append("is_winter_rated_out_of_domain")
            winter = {"__invalid_database_value__": winter_value}
        products[row_id] = {
            "payload": {
                "row_id": row_id,
                "sku": row["sku"],
                "category": row["category"],
                "subcategory": row["subcategory"],
                "attributes": attributes,
                "is_winter_rated": winter,
                "tier": row["tier"],
                "evidence": row["evidence"],
            },
            "origin_batch_id": row["batch_id"],
        }
        if errors:
            schema_errors[row_id] = errors
    return products, schema_errors


def _snapshot_records(snapshot: dict[str, dict]) -> list[dict]:
    return [
        {
            "row_id": row_id,
            "payload_sha256": sha256_json(snapshot[row_id]["payload"]),
            "origin_batch_id": snapshot[row_id]["origin_batch_id"],
        }
        for row_id in sorted(snapshot)
    ]


def _outcome_records(decoded: list[dict], rejects: list[dict]) -> list[dict]:
    records = [{"row_id": row["row_id"], "state": "decoded", "payload": row} for row in decoded]
    records.extend(
        {"row_id": row["row_id"], "state": row["bucket"], "payload": row} for row in rejects
    )
    return sorted(records, key=lambda row: row["row_id"])


def _verified_outcomes(conn: sqlite3.Connection, batch_id: str) -> tuple[list[dict], list[dict]]:
    conn.row_factory = sqlite3.Row
    evidence = conn.execute(
        "SELECT run_id, decoded_sha256, rejects_sha256, incoming_rows, "
        "outcome_rows, outcomes_sha256 "
        "FROM batch_evidence WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    batch = conn.execute(
        "SELECT decoded_sha256 FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    reconciliation = conn.execute(
        "SELECT outcome_rows, outcomes_sha256 FROM load_reconciliation WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT row_id, state, payload_json FROM batch_outcomes WHERE batch_id = ? ORDER BY row_id",
        (batch_id,),
    ).fetchall()
    reasons: list[str] = []
    if evidence is None:
        reasons.append("missing_batch_evidence")
    if batch is None:
        reasons.append("missing_batch_registry")
    if reconciliation is None:
        reasons.append("missing_reconciliation_anchor")
    decoded: list[dict] = []
    rejects: list[dict] = []
    records: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            reasons.append(f"malformed_outcome_payload:{row['row_id']}")
            continue
        if not isinstance(payload, dict):
            reasons.append(f"non_object_outcome_payload:{row['row_id']}")
            continue
        if payload.get("row_id") != row["row_id"]:
            reasons.append(f"outcome_row_id_mismatch:{row['row_id']}")
        record = {"row_id": row["row_id"], "state": row["state"], "payload": payload}
        records.append(record)
        if row["state"] == "decoded":
            decoded.append(payload)
        elif row["state"] in REJECT_BUCKETS:
            rejects.append(payload)
        else:
            reasons.append(f"invalid_outcome_state:{row['row_id']}")
    records_sha256 = sha256_json(records)
    if evidence is not None:
        if evidence["outcome_rows"] != len(rows):
            reasons.append("batch_evidence_outcome_count_mismatch")
        if evidence["outcomes_sha256"] != records_sha256:
            reasons.append("batch_evidence_outcome_hash_mismatch")
        if evidence["decoded_sha256"] != sha256_jsonl_rows(decoded):
            reasons.append("decoded_outcome_hash_mismatch")
        if evidence["rejects_sha256"] != sha256_jsonl_rows(rejects):
            reasons.append("rejects_outcome_hash_mismatch")
        if evidence["incoming_rows"] != len(decoded):
            reasons.append("batch_evidence_incoming_count_mismatch")
        expected_run_id = sha256_json(
            {
                "batch_id": batch_id,
                "decoded_sha256": evidence["decoded_sha256"],
                "rejects_sha256": evidence["rejects_sha256"],
            }
        )
        if evidence["run_id"] != expected_run_id:
            reasons.append("batch_evidence_run_id_mismatch")
        if batch is not None and batch["decoded_sha256"] != evidence["decoded_sha256"]:
            reasons.append("batch_registry_decoded_hash_mismatch")
    if reconciliation is not None:
        if reconciliation["outcome_rows"] != len(rows):
            reasons.append("reconciliation_outcome_count_mismatch")
        if reconciliation["outcomes_sha256"] != records_sha256:
            reasons.append("reconciliation_outcome_hash_mismatch")
    if reasons:
        raise BundleError("lineage_metadata_mismatch", reasons)
    return decoded, rejects


def _snapshot_state(conn: sqlite3.Connection, batch_id: str) -> dict:
    evidence = conn.execute(
        "SELECT snapshot_rows, batch_rows_sha256 FROM batch_evidence WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    anchor = conn.execute(
        "SELECT expected_rows, batch_rows_sha256 FROM load_reconciliation WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT row_id, payload_sha256, origin_batch_id FROM batch_rows "
        "WHERE batch_id = ? ORDER BY row_id",
        (batch_id,),
    ).fetchall()
    records = [dict(row) for row in rows]
    digest = sha256_json(records)
    reasons: list[str] = []
    if evidence is None:
        reasons.append("missing_batch_evidence")
    else:
        if evidence["snapshot_rows"] != len(records):
            reasons.append("batch_evidence_snapshot_count_mismatch")
        if evidence["batch_rows_sha256"] != digest:
            reasons.append("batch_evidence_snapshot_hash_mismatch")
    if anchor is None:
        reasons.append("missing_reconciliation_anchor")
    else:
        if anchor["expected_rows"] != len(records):
            reasons.append("reconciliation_snapshot_count_mismatch")
        if anchor["batch_rows_sha256"] != digest:
            reasons.append("reconciliation_snapshot_hash_mismatch")
    return {
        "records": records,
        "by_id": {record["row_id"]: record for record in records},
        "sha256": digest,
        "reasons": reasons,
    }


def _previous_batch_id(conn: sqlite3.Connection, batch_id: str) -> str | None:
    row = conn.execute(
        "SELECT batch_id FROM batches WHERE rowid < "
        "(SELECT rowid FROM batches WHERE batch_id = ?) ORDER BY rowid DESC LIMIT 1",
        (batch_id,),
    ).fetchone()
    return row[0] if row else None


def _replayed_comparison(
    conn: sqlite3.Connection,
    parent_batch_id: str | None,
    batch_id: str,
    current_decoded: list[dict],
    current_rejects: list[dict],
    *,
    current_manifest: dict | None = None,
) -> dict:
    if parent_batch_id is None:
        baseline_decoded: list[dict] = []
        baseline_rejects: list[dict] = []
    else:
        baseline_decoded, baseline_rejects = _verified_outcomes(conn, parent_batch_id)
    return compare_batches(
        baseline_decoded,
        current_decoded,
        baseline_rejects=baseline_rejects,
        current_rejects=current_rejects,
        baseline_batch_id=parent_batch_id,
        current_batch_id=batch_id,
        current_manifest=current_manifest,
    )


def _canonical_comparison(
    conn: sqlite3.Connection,
    parent_batch_id: str | None,
    bundle: dict,
) -> dict:
    expected = _replayed_comparison(
        conn,
        parent_batch_id,
        bundle["batch_id"],
        bundle["decoded"],
        bundle["rejects"],
        current_manifest=bundle["manifest"],
    )
    supplied = bundle["comparison"]
    if supplied is not None and sha256_json(supplied) != sha256_json(expected):
        raise BundleError(
            "comparison_replay_mismatch",
            {"expected_parent_batch_id": parent_batch_id},
        )
    return expected


def _canonical_lineage(
    conn: sqlite3.Connection,
    batch_id: str,
    decoded: list[dict],
    rejects: list[dict],
    *,
    current_manifest: dict | None = None,
) -> tuple[dict, dict]:
    current = _snapshot_state(conn, batch_id)
    reasons = list(current["reasons"])
    parent_batch_id = _previous_batch_id(conn, batch_id)
    if parent_batch_id is None:
        parent_records: dict[str, dict] = {}
    else:
        parent = _snapshot_state(conn, parent_batch_id)
        reasons.extend(f"parent:{reason}" for reason in parent["reasons"])
        parent_records = parent["by_id"]

    incoming = {row["row_id"]: _payload(row) for row in decoded}
    incoming_ids = set(incoming)
    parent_ids = set(parent_records)
    added_ids = sorted(incoming_ids - parent_ids)
    removed_ids = sorted(parent_ids - incoming_ids)
    unchanged_ids = sorted(incoming_ids & parent_ids)
    conflicts = sorted(
        row_id
        for row_id in unchanged_ids
        if parent_records[row_id]["payload_sha256"] != sha256_json(incoming[row_id])
    )
    if conflicts:
        reasons.append("lineage_payload_conflict")

    expected_records = dict(parent_records)
    for row_id in added_ids:
        expected_records[row_id] = {
            "row_id": row_id,
            "payload_sha256": sha256_json(incoming[row_id]),
            "origin_batch_id": batch_id,
        }
    if (
        sha256_json([expected_records[row_id] for row_id in sorted(expected_records)])
        != current["sha256"]
    ):
        reasons.append("lineage_snapshot_membership_mismatch")
    if reasons:
        raise BundleError("lineage_metadata_mismatch", sorted(set(reasons)))

    comparison = _replayed_comparison(
        conn,
        parent_batch_id,
        batch_id,
        decoded,
        rejects,
        current_manifest=current_manifest,
    )
    return (
        {
            "parent_batch_id": parent_batch_id,
            "added": len(added_ids),
            "removed": len(removed_ids),
            "changed": 0,
            "unchanged": len(unchanged_ids),
            "comparison_sha256": comparison["comparison_sha256"],
        },
        comparison,
    )


def _lineage_mismatches(stored: sqlite3.Row | None, expected: dict) -> list[str]:
    if stored is None:
        return ["missing_lineage"]
    return sorted(
        name for name, value in expected.items() if sha256_json(stored[name]) != sha256_json(value)
    )


def _expected_decision_id(
    *,
    batch_id: str,
    run_id: str,
    manifest_sha256: str,
    eval_report_sha256: str,
    raw_sha256: str,
    decoded_sha256: str,
    rejects_sha256: str,
    schema_report_sha256: str,
    gold_sha256: str,
    gate_report_sha256: str,
    thresholds_sha256: str,
) -> str:
    return sha256_json(
        {
            "batch_id": batch_id,
            "run_id": run_id,
            "manifest_sha256": manifest_sha256,
            "eval_report_sha256": eval_report_sha256,
            "raw_sha256": raw_sha256,
            "decoded_sha256": decoded_sha256,
            "rejects_sha256": rejects_sha256,
            "schema_report_sha256": schema_report_sha256,
            "gold_sha256": gold_sha256,
            "gate_report_sha256": gate_report_sha256,
            "thresholds_sha256": thresholds_sha256,
        }
    )


def _decision_records(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT decision_id, batch_id, gate_report_sha256, thresholds_sha256, passed "
        "FROM gate_decisions WHERE batch_id = ? ORDER BY decision_id",
        (batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _decision_set_anchor(conn: sqlite3.Connection, batch_id: str) -> tuple[int, str]:
    records = _decision_records(conn, batch_id)
    return len(records), sha256_json(records)


def _update_decision_set_anchor(conn: sqlite3.Connection, batch_id: str) -> None:
    count, digest = _decision_set_anchor(conn, batch_id)
    conn.execute(
        "UPDATE load_reconciliation SET decision_count = ?, decisions_sha256 = ? "
        "WHERE batch_id = ?",
        (count, digest, batch_id),
    )


def _decision_metadata_reasons(
    conn: sqlite3.Connection, batch_id: str, evidence: sqlite3.Row | None
) -> list[str]:
    if evidence is None:
        return ["gate_decision_missing_batch_evidence"]
    rows = _decision_records(conn, batch_id)
    reasons: list[str] = []
    if not rows:
        reasons.append("missing_gate_decision")
    for row in rows:
        expected_id = _expected_decision_id(
            batch_id=batch_id,
            run_id=evidence["run_id"],
            manifest_sha256=evidence["manifest_sha256"],
            eval_report_sha256=evidence["eval_report_sha256"],
            raw_sha256=evidence["raw_sha256"],
            decoded_sha256=evidence["decoded_sha256"],
            rejects_sha256=evidence["rejects_sha256"],
            schema_report_sha256=evidence["schema_report_sha256"],
            gold_sha256=evidence["gold_sha256"],
            gate_report_sha256=row["gate_report_sha256"],
            thresholds_sha256=row["thresholds_sha256"],
        )
        if row["decision_id"] != expected_id:
            reasons.append(f"gate_decision_id_mismatch:{row['decision_id']}")
        if row["batch_id"] != batch_id:
            reasons.append(f"gate_decision_batch_link_mismatch:{row['decision_id']}")
        if row["passed"] != 1:
            reasons.append(f"gate_decision_passed_mismatch:{row['decision_id']}")

    # A single-field batch-link edit moves the row out of the query above.
    # Scan other links and identify decisions whose canonical ID still binds
    # this batch's measurement evidence.
    foreign_rows = conn.execute(
        "SELECT decision_id, batch_id, gate_report_sha256, thresholds_sha256 "
        "FROM gate_decisions WHERE batch_id <> ?",
        (batch_id,),
    ).fetchall()
    for row in foreign_rows:
        candidate = _expected_decision_id(
            batch_id=batch_id,
            run_id=evidence["run_id"],
            manifest_sha256=evidence["manifest_sha256"],
            eval_report_sha256=evidence["eval_report_sha256"],
            raw_sha256=evidence["raw_sha256"],
            decoded_sha256=evidence["decoded_sha256"],
            rejects_sha256=evidence["rejects_sha256"],
            schema_report_sha256=evidence["schema_report_sha256"],
            gold_sha256=evidence["gold_sha256"],
            gate_report_sha256=row["gate_report_sha256"],
            thresholds_sha256=row["thresholds_sha256"],
        )
        if row["decision_id"] == candidate:
            reasons.append(f"gate_decision_batch_link_mismatch:{row['decision_id']}")
    return reasons


def _reconcile_anchored(conn: sqlite3.Connection, batch_id: str) -> dict:
    conn.row_factory = sqlite3.Row
    batch = conn.execute(
        "SELECT batch_id, inserted, decoded_sha256 FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return {"passed": False, "reason": "unknown_batch", "batch_id": batch_id}
    evidence = conn.execute(
        "SELECT * FROM batch_evidence WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    anchor = conn.execute(
        "SELECT * FROM load_reconciliation WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    snapshot = _snapshot_state(conn, batch_id)
    row_records_sha256 = snapshot["sha256"]
    metadata_reasons: list[str] = list(snapshot["reasons"])
    decoded: list[dict] | None = None
    rejects: list[dict] | None = None
    try:
        decoded, rejects = _verified_outcomes(conn, batch_id)
    except BundleError as exc:
        metadata_reasons.extend(exc.details or [exc.reason])

    expected_lineage: dict | None = None
    if decoded is not None and rejects is not None:
        try:
            expected_lineage, _ = _canonical_lineage(conn, batch_id, decoded, rejects)
        except BundleError as exc:
            metadata_reasons.extend(exc.details or [exc.reason])
        else:
            stored_lineage = conn.execute(
                "SELECT parent_batch_id, added, removed, changed, unchanged, comparison_sha256 "
                "FROM batch_lineage WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            for field in _lineage_mismatches(stored_lineage, expected_lineage):
                metadata_reasons.append(f"batch_lineage_{field}_mismatch")
            if batch["inserted"] != expected_lineage["added"]:
                metadata_reasons.append("batch_registry_inserted_mismatch")

    if evidence is not None:
        metadata_reasons.extend(_decision_metadata_reasons(conn, batch_id, evidence))
        for name in (
            "manifest_sha256",
            "eval_report_sha256",
            "raw_sha256",
            "decoded_sha256",
            "rejects_sha256",
            "schema_report_sha256",
            "gold_sha256",
            "batch_rows_sha256",
            "outcomes_sha256",
        ):
            value = evidence[name]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                metadata_reasons.append(f"batch_evidence_{name}_malformed")
    decision_count, decisions_sha256 = _decision_set_anchor(conn, batch_id)
    if anchor is not None:
        if anchor["decision_count"] != decision_count:
            metadata_reasons.append("reconciliation_decision_count_mismatch")
        if anchor["decisions_sha256"] != decisions_sha256:
            metadata_reasons.append("reconciliation_decisions_hash_mismatch")

    actual, database_schema_errors = _db_products(conn)
    expected_ids = set(snapshot["by_id"])
    actual_ids = set(actual)
    latest = conn.execute("SELECT batch_id FROM batches ORDER BY rowid DESC LIMIT 1").fetchone()
    missing = sorted(expected_ids - actual_ids)
    extra_ids = sorted(actual_ids - expected_ids)
    if latest is None or latest[0] == batch_id:
        unexpected = extra_ids
    else:
        latest_snapshot = _snapshot_state(conn, latest[0])
        if latest_snapshot["reasons"]:
            metadata_reasons.extend(
                f"latest_snapshot:{reason}" for reason in latest_snapshot["reasons"]
            )
            unexpected = extra_ids
        else:
            unexpected = [
                row_id
                for row_id in extra_ids
                if row_id not in latest_snapshot["by_id"]
                or latest_snapshot["by_id"][row_id]["payload_sha256"]
                != sha256_json(actual[row_id]["payload"])
                or latest_snapshot["by_id"][row_id]["origin_batch_id"]
                != actual[row_id]["origin_batch_id"]
            ]
    row_by_id = snapshot["by_id"]
    payload_mutations = sorted(
        row_id
        for row_id in expected_ids & actual_ids
        if row_by_id[row_id]["payload_sha256"] != sha256_json(actual[row_id]["payload"])
    )
    provenance_mutations = sorted(
        row_id
        for row_id in expected_ids & actual_ids
        if row_by_id[row_id]["origin_batch_id"] != actual[row_id]["origin_batch_id"]
    )
    database_schema_mutations = {
        row_id: errors for row_id, errors in sorted(database_schema_errors.items())
    }
    mutated_expected = (
        set(payload_mutations)
        | set(provenance_mutations)
        | (set(database_schema_mutations) & expected_ids)
    )
    verified_rows = len(expected_ids) - len(missing) - len(mutated_expected)
    snapshot_actual = {row_id: actual[row_id] for row_id in sorted(expected_ids & actual_ids)}

    primary_metadata_reasons = sorted(set(metadata_reasons))
    core_passed = not (
        primary_metadata_reasons
        or missing
        or unexpected
        or payload_mutations
        or provenance_mutations
        or database_schema_mutations
    )
    if anchor is not None:
        expected_summary = {
            "passed": 1,
            "expected_rows": len(expected_ids),
            "verified_rows": len(expected_ids),
            "missing_rows": 0,
            "unexpected_rows": 0,
            "payload_mutations": 0,
            "provenance_mutations": 0,
            "database_schema_mutations": 0,
            "metadata_mutations": 0,
            "source_rowset_sha256": row_records_sha256,
            "batch_rows_sha256": row_records_sha256,
            "decision_count": decision_count,
            "decisions_sha256": decisions_sha256,
        }
        expected_schema_mutations = set(database_schema_mutations) & expected_ids
        if not (missing or payload_mutations or provenance_mutations or expected_schema_mutations):
            expected_summary["database_rowset_sha256"] = sha256_json(snapshot_actual)
        if decoded is not None and rejects is not None:
            outcome_records = _outcome_records(decoded, rejects)
            expected_summary["outcome_rows"] = len(outcome_records)
            expected_summary["outcomes_sha256"] = sha256_json(outcome_records)
        for field, expected in expected_summary.items():
            if sha256_json(anchor[field]) != sha256_json(expected):
                metadata_reasons.append(f"load_reconciliation_{field}_mismatch")

    metadata_reasons = sorted(set(metadata_reasons))
    passed = core_passed and not (set(metadata_reasons) - set(primary_metadata_reasons))
    return {
        "passed": passed,
        "reason": None if passed else "reconciliation_failed",
        "batch_id": batch_id,
        "expected_rows": len(expected_ids),
        "verified_rows": verified_rows,
        "missing_row_ids": missing,
        "unexpected_row_ids": unexpected,
        "payload_mutation_row_ids": payload_mutations,
        "provenance_mutation_row_ids": provenance_mutations,
        "database_schema_mutations": database_schema_mutations,
        "metadata_mutation_reasons": sorted(set(metadata_reasons)),
        "source_rowset_sha256": row_records_sha256,
        "database_rowset_sha256": sha256_json(actual),
    }


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _initialize_or_validate_registry(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        # DDL is one transaction so a failed initialization cannot leave a
        # partially materialized registry behind.
        conn.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA}\nCOMMIT;")
        return
    if tables != set(_EXPECTED_SCHEMA_COLUMNS):
        raise sqlite3.OperationalError("existing registry has unexpected or missing tables")
    for table, expected in _EXPECTED_SCHEMA_COLUMNS.items():
        observed = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")'))
        if observed != expected:
            raise sqlite3.OperationalError(f"existing registry table {table!r} is incompatible")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def reconcile_batch(db_path: Path, batch_id: str) -> dict:
    """Read-only verification of products plus persisted snapshot metadata."""
    if not Path(db_path).exists():
        return {"passed": False, "reason": "missing_database", "batch_id": batch_id}
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error as exc:
        return {
            "passed": False,
            "reason": "unreadable_database",
            "batch_id": batch_id,
            "details": str(exc),
        }
    try:
        return _reconcile_anchored(conn, batch_id)
    except sqlite3.Error as exc:
        return {
            "passed": False,
            "reason": "database_schema_missing",
            "batch_id": batch_id,
            "details": str(exc),
        }
    finally:
        conn.close()


def _measurement_mismatches(existing: sqlite3.Row, bundle: dict) -> list[str]:
    expected = {
        "run_id": bundle["run_id"],
        "manifest_sha256": bundle["actual"]["manifest_sha256"],
        "eval_report_sha256": bundle["actual"]["eval_report_sha256"],
        "raw_sha256": bundle["actual"]["raw_sha256"],
        "decoded_sha256": bundle["actual"]["decoded_sha256"],
        "rejects_sha256": bundle["actual"]["rejects_sha256"],
        "schema_report_sha256": bundle["actual"]["schema_report_sha256"],
        "gold_sha256": bundle["actual"]["gold_sha256"],
    }
    return [name for name, value in expected.items() if existing[name] != value]


def _record_decision(conn: sqlite3.Connection, bundle: dict, now: str) -> bool:
    existing = conn.execute(
        "SELECT batch_id, gate_report_sha256, thresholds_sha256, passed "
        "FROM gate_decisions WHERE decision_id = ?",
        (bundle["decision_id"],),
    ).fetchone()
    if existing:
        expected = {
            "batch_id": bundle["batch_id"],
            "gate_report_sha256": bundle["actual"]["gate_report_sha256"],
            "thresholds_sha256": bundle["actual"]["thresholds_sha256"],
            "passed": 1,
        }
        mismatches = sorted(
            name
            for name, value in expected.items()
            if sha256_json(existing[name]) != sha256_json(value)
        )
        if mismatches:
            raise BundleError("gate_decision_mismatch", mismatches)
        return False
    conn.execute(
        "INSERT INTO gate_decisions VALUES (?,?,?,?,?,?)",
        (
            bundle["decision_id"],
            bundle["batch_id"],
            now,
            bundle["actual"]["gate_report_sha256"],
            bundle["actual"]["thresholds_sha256"],
            1,
        ),
    )
    return True


def load_batch(
    decoded_path: Path,
    gate_report_path: Path,
    db_path: Path,
    *,
    raw_path: Path,
    rejects_path: Path,
    schema_report_path: Path,
    manifest_path: Path,
    eval_report_path: Path,
    gold_path: Path,
    thresholds: dict,
    comparison_report_path: Path | None = None,
) -> dict:
    """Verify one immutable bundle, then serialize state checks and writes."""
    try:
        bundle = _verify_bundle(
            raw_path=raw_path,
            decoded_path=decoded_path,
            rejects_path=rejects_path,
            schema_report_path=schema_report_path,
            manifest_path=manifest_path,
            eval_report_path=eval_report_path,
            gold_path=gold_path,
            gate_report_path=gate_report_path,
            thresholds=thresholds,
            comparison_report_path=comparison_report_path,
        )
    except BundleError as exc:
        batch_id = exc.details if exc.reason == "gate_failed" else None
        return _refused(exc.reason, batch_id, None if batch_id else exc.details)
    except Exception as exc:  # malformed evidence must never escape as an unstructured crash
        return _refused("malformed_bundle", details=f"{type(exc).__name__}: {exc}")

    batch_id = bundle["batch_id"]
    try:
        conn = _connect_rw(db_path)
    except sqlite3.Error as exc:
        return _refused("unreadable_database", batch_id, str(exc))
    try:
        _initialize_or_validate_registry(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(UTC).isoformat(timespec="seconds")
            existing = conn.execute(
                "SELECT * FROM batch_evidence WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if existing is not None:
                mismatches = _measurement_mismatches(existing, bundle)
                if mismatches:
                    conn.rollback()
                    reason = (
                        "batch_id_conflict" if "run_id" in mismatches else "measurement_conflict"
                    )
                    return _refused(reason, batch_id, mismatches)
                try:
                    expected_lineage, comparison = _canonical_lineage(
                        conn,
                        batch_id,
                        bundle["decoded"],
                        bundle["rejects"],
                        current_manifest=bundle["manifest"],
                    )
                except BundleError as exc:
                    conn.rollback()
                    return _refused(exc.reason, batch_id, exc.details)
                supplied = bundle["comparison"]
                if supplied is not None and sha256_json(supplied) != sha256_json(comparison):
                    conn.rollback()
                    return _refused(
                        "comparison_replay_mismatch",
                        batch_id,
                        {"expected_parent_batch_id": expected_lineage["parent_batch_id"]},
                    )
                stored_lineage = conn.execute(
                    "SELECT parent_batch_id, added, removed, changed, unchanged, "
                    "comparison_sha256 FROM batch_lineage WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                lineage_mismatches = _lineage_mismatches(stored_lineage, expected_lineage)
                if lineage_mismatches:
                    conn.rollback()
                    return _refused("lineage_metadata_mismatch", batch_id, lineage_mismatches)
                decision_exists = conn.execute(
                    "SELECT 1 FROM gate_decisions WHERE decision_id = ?",
                    (bundle["decision_id"],),
                ).fetchone()
                if decision_exists:
                    try:
                        _record_decision(conn, bundle, now)
                    except BundleError as exc:
                        conn.rollback()
                        return _refused(exc.reason, batch_id, exc.details)
                reconciliation = _reconcile_anchored(conn, batch_id)
                if not reconciliation["passed"]:
                    conn.rollback()
                    return _refused("reconciliation_failed", batch_id, reconciliation)
                decision_recorded = False
                if not decision_exists:
                    try:
                        decision_recorded = _record_decision(conn, bundle, now)
                    except BundleError as exc:
                        conn.rollback()
                        return _refused(exc.reason, batch_id, exc.details)
                    _update_decision_set_anchor(conn, batch_id)
                    reconciliation = _reconcile_anchored(conn, batch_id)
                    if not reconciliation["passed"]:
                        conn.rollback()
                        return _refused("reconciliation_failed", batch_id, reconciliation)
                if decision_recorded:
                    conn.commit()
                else:
                    conn.rollback()
                return {
                    "status": "noop",
                    "reason": None,
                    "inserted": 0,
                    "batch_id": batch_id,
                    "run_id": bundle["run_id"],
                    "decision_id": bundle["decision_id"],
                    "decision_recorded": decision_recorded,
                    "comparison": comparison,
                    "reconciliation": reconciliation,
                    "schema_evidence": bundle["schema_evidence"],
                }

            legacy = conn.execute(
                "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if legacy:
                conn.rollback()
                return _refused("legacy_batch_evidence_missing", batch_id)

            parent = conn.execute(
                "SELECT batch_id FROM batches ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            parent_batch_id = parent[0] if parent else None
            if parent_batch_id is not None:
                parent_reconciliation = _reconcile_anchored(conn, parent_batch_id)
                if not parent_reconciliation["passed"]:
                    conn.rollback()
                    return _refused(
                        "parent_reconciliation_failed",
                        batch_id,
                        parent_reconciliation,
                    )

            before, database_errors = _db_products(conn)
            if database_errors:
                conn.rollback()
                return _refused("database_integrity_failed", batch_id, database_errors)
            incoming = {row["row_id"]: _payload(row) for row in bundle["decoded"]}
            changed = sorted(
                row_id
                for row_id in set(before) & set(incoming)
                if sha256_json(before[row_id]["payload"]) != sha256_json(incoming[row_id])
            )
            if changed:
                conn.rollback()
                return _refused("row_content_conflict", batch_id, changed[:20])
            added = sorted(set(incoming) - set(before))
            unchanged = sorted(set(incoming) & set(before))
            removed = sorted(set(before) - set(incoming))
            try:
                comparison = _canonical_comparison(conn, parent_batch_id, bundle)
            except BundleError as exc:
                conn.rollback()
                return _refused(exc.reason, batch_id, exc.details)

            expected_db = dict(before)
            for row_id in added:
                expected_db[row_id] = {"payload": incoming[row_id], "origin_batch_id": batch_id}
            snapshot_records = _snapshot_records(expected_db)
            batch_rows_sha256 = sha256_json(snapshot_records)
            outcome_records = _outcome_records(bundle["decoded"], bundle["rejects"])
            outcomes_sha256 = sha256_json(outcome_records)

            conn.execute(
                "INSERT INTO batches VALUES (?,?,?,?)",
                (batch_id, now, len(added), bundle["actual"]["decoded_sha256"]),
            )
            for row_id in added:
                row = incoming[row_id]
                conn.execute(
                    "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        row_id,
                        row["sku"],
                        row["category"],
                        row["subcategory"],
                        json.dumps(row["attributes"], sort_keys=True),
                        1 if row["is_winter_rated"] else 0,
                        row["tier"],
                        row["evidence"],
                        batch_id,
                    ),
                )
            conn.execute(
                "INSERT INTO batch_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    bundle["run_id"],
                    bundle["actual"]["manifest_sha256"],
                    bundle["actual"]["eval_report_sha256"],
                    bundle["actual"]["raw_sha256"],
                    bundle["actual"]["decoded_sha256"],
                    bundle["actual"]["rejects_sha256"],
                    bundle["actual"]["schema_report_sha256"],
                    bundle["actual"]["gold_sha256"],
                    len(incoming),
                    len(snapshot_records),
                    batch_rows_sha256,
                    len(outcome_records),
                    outcomes_sha256,
                ),
            )
            conn.execute(
                "INSERT INTO batch_lineage VALUES (?,?,?,?,?,?,?)",
                (
                    batch_id,
                    parent_batch_id,
                    len(added),
                    len(removed),
                    0,
                    len(unchanged),
                    comparison["comparison_sha256"],
                ),
            )
            for record in snapshot_records:
                conn.execute(
                    "INSERT INTO batch_rows VALUES (?,?,?,?)",
                    (
                        batch_id,
                        record["row_id"],
                        record["payload_sha256"],
                        record["origin_batch_id"],
                    ),
                )
            for record in outcome_records:
                conn.execute(
                    "INSERT INTO batch_outcomes VALUES (?,?,?,?)",
                    (
                        batch_id,
                        record["row_id"],
                        record["state"],
                        json.dumps(record["payload"], sort_keys=True, separators=(",", ":")),
                    ),
                )

            actual_db, database_errors = _db_products(conn)
            expected_ids, actual_ids = set(expected_db), set(actual_db)
            missing = sorted(expected_ids - actual_ids)
            unexpected = sorted(actual_ids - expected_ids)
            payload_mutations = sorted(
                row_id
                for row_id in expected_ids & actual_ids
                if sha256_json(expected_db[row_id]["payload"])
                != sha256_json(actual_db[row_id]["payload"])
            )
            provenance_mutations = sorted(
                row_id
                for row_id in expected_ids & actual_ids
                if expected_db[row_id]["origin_batch_id"] != actual_db[row_id]["origin_batch_id"]
            )
            immediate_passed = not (
                missing
                or unexpected
                or payload_mutations
                or provenance_mutations
                or database_errors
            )
            _record_decision(conn, bundle, now)
            decision_count, decisions_sha256 = _decision_set_anchor(conn, batch_id)
            conn.execute(
                "INSERT INTO load_reconciliation VALUES " "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    now,
                    int(immediate_passed),
                    len(snapshot_records),
                    len(snapshot_records)
                    - len(missing)
                    - len(set(payload_mutations) | set(provenance_mutations)),
                    len(missing),
                    len(unexpected),
                    len(payload_mutations),
                    len(provenance_mutations),
                    len(database_errors),
                    0,
                    batch_rows_sha256,
                    sha256_json(actual_db),
                    batch_rows_sha256,
                    len(outcome_records),
                    outcomes_sha256,
                    decision_count,
                    decisions_sha256,
                ),
            )
            reconciliation = _reconcile_anchored(conn, batch_id)
            if not reconciliation["passed"]:
                conn.rollback()
                return _refused("reconciliation_failed", batch_id, reconciliation)
            conn.commit()

            lineage_result = {
                "parent_batch_id": parent_batch_id,
                "added": len(added),
                "removed": len(removed),
                "changed": 0,
                "unchanged": len(unchanged),
                "comparison_sha256": comparison["comparison_sha256"],
            }
            return {
                "status": "loaded",
                "reason": None,
                "inserted": len(added),
                "batch_id": batch_id,
                "run_id": bundle["run_id"],
                "decision_id": bundle["decision_id"],
                "decision_recorded": True,
                "lineage": lineage_result,
                "comparison": comparison,
                "reconciliation": reconciliation,
                "schema_evidence": bundle["schema_evidence"],
            }
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            return _refused("database_constraint_failed", batch_id, str(exc))
        except BundleError as exc:
            conn.rollback()
            return _refused(exc.reason, batch_id, exc.details)
        except Exception:
            conn.rollback()
            raise
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        return _refused("database_schema_incompatible", batch_id, str(exc))
    finally:
        conn.close()


def read_registry(db_path: Path) -> list[dict]:
    if not Path(db_path).exists():
        return []
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT b.batch_id, b.loaded_at, b.inserted, e.incoming_rows, "
            "e.snapshot_rows, l.parent_batch_id, l.removed, r.passed AS reconciled, "
            "(SELECT COUNT(*) FROM gate_decisions d WHERE d.batch_id = b.batch_id) AS decisions "
            "FROM batches b LEFT JOIN batch_evidence e ON e.batch_id = b.batch_id "
            "LEFT JOIN batch_lineage l ON l.batch_id = b.batch_id "
            "LEFT JOIN load_reconciliation r ON r.batch_id = b.batch_id ORDER BY b.rowid"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
