"""Deterministic decoded/rejected batch comparison evidence."""

from __future__ import annotations

from pathlib import Path

from sku_sleuth.models import dump_json, sha256_json, sha256_jsonl_rows

COMPARISON_VERSION = "1"
_MISSING = object()
DECODE_FIELDS = (
    "sku",
    "category",
    "subcategory",
    "is_winter_rated",
    "tier",
    "evidence",
)
REJECT_FIELDS = ("sku", "title", "bucket", "reason")


def _index(rows: list[dict], label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        row_id = row.get("row_id")
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"{label} contains a missing or invalid row_id")
        if row_id in out:
            raise ValueError(f"duplicate row_id {row_id!r} in {label}")
        out[row_id] = row
    return out


def _outcomes(decoded: list[dict], rejects: list[dict], label: str) -> dict[str, dict]:
    decoded_by_id = _index(decoded, f"{label} decoded")
    rejects_by_id = _index(rejects, f"{label} rejects")
    overlap = sorted(set(decoded_by_id) & set(rejects_by_id))
    if overlap:
        raise ValueError(f"row_id {overlap[0]!r} is decoded and rejected in {label}")
    outcomes = {row_id: {"state": "decoded", "row": row} for row_id, row in decoded_by_id.items()}
    for row_id, row in rejects_by_id.items():
        outcomes[row_id] = {"state": row.get("bucket", "rejected"), "row": row}
    return outcomes


def _field_values(outcome: dict) -> dict:
    row = outcome["row"]
    fields: dict = {"outcome": outcome["state"]}
    keys = DECODE_FIELDS if outcome["state"] == "decoded" else REJECT_FIELDS
    for key in keys:
        fields[key] = row[key] if key in row else _MISSING
    if outcome["state"] == "decoded":
        attributes = row.get("attributes", _MISSING)
        if attributes is _MISSING:
            fields["attributes"] = _MISSING
        elif not isinstance(attributes, dict):
            raise ValueError("decoded attributes must be an object")
        else:
            for key, value in sorted(attributes.items()):
                fields[f"attributes.{key}"] = value
    return fields


def _same(left, right) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return sha256_json(left) == sha256_json(right)


def _render(value):
    return {"presence": "missing"} if value is _MISSING else value


def compare_batches(
    baseline_decoded: list[dict],
    current_decoded: list[dict],
    *,
    baseline_rejects: list[dict] | None = None,
    current_rejects: list[dict] | None = None,
    baseline_batch_id: str | None = None,
    current_batch_id: str | None = None,
    baseline_manifest: dict | None = None,
    current_manifest: dict | None = None,
) -> dict:
    baseline_rejects = baseline_rejects or []
    current_rejects = current_rejects or []
    before = _outcomes(baseline_decoded, baseline_rejects, "baseline")
    after = _outcomes(current_decoded, current_rejects, "current")
    baseline_hashes = {
        "decoded_sha256": sha256_jsonl_rows(baseline_decoded),
        "rejects_sha256": sha256_jsonl_rows(baseline_rejects),
    }
    current_hashes = {
        "decoded_sha256": sha256_jsonl_rows(current_decoded),
        "rejects_sha256": sha256_jsonl_rows(current_rejects),
    }
    for label, manifest, hashes, explicit_id in (
        ("baseline", baseline_manifest, baseline_hashes, baseline_batch_id),
        ("current", current_manifest, current_hashes, current_batch_id),
    ):
        if manifest is None:
            continue
        for artifact, digest in hashes.items():
            if manifest.get(artifact) != digest:
                raise ValueError(f"{label} {artifact} does not match its manifest")
        if explicit_id is not None and manifest.get("batch_id") != explicit_id:
            raise ValueError(f"{label} batch_id does not match its manifest")
    before_ids, after_ids = set(before), set(after)
    added = sorted(after_ids - before_ids)
    removed = sorted(before_ids - after_ids)
    common = sorted(before_ids & after_ids)

    changed_rows: list[dict] = []
    unchanged = 0
    field_change_counts: dict[str, int] = {}
    transition_counts: dict[str, int] = {}
    classification_changes = tier_changes = flag_changes = 0
    for row_id in common:
        old_outcome, new_outcome = before[row_id], after[row_id]
        transition = f"{old_outcome['state']}_to_{new_outcome['state']}"
        transition_counts[transition] = transition_counts.get(transition, 0) + 1
        old_fields = _field_values(old_outcome)
        new_fields = _field_values(new_outcome)
        changes: dict[str, dict] = {}
        for field in sorted(set(old_fields) | set(new_fields)):
            old, new = old_fields.get(field, _MISSING), new_fields.get(field, _MISSING)
            if not _same(old, new):
                changes[field] = {"before": _render(old), "after": _render(new)}
                field_change_counts[field] = field_change_counts.get(field, 0) + 1
        if not changes:
            unchanged += 1
            continue
        if old_outcome["state"] == new_outcome["state"] == "decoded":
            if "category" in changes or "subcategory" in changes:
                classification_changes += 1
            if "tier" in changes:
                tier_changes += 1
            if "is_winter_rated" in changes:
                flag_changes += 1
        changed_rows.append({"row_id": row_id, "changes": changes})

    report = {
        "comparison_version": COMPARISON_VERSION,
        "baseline_batch_id": baseline_batch_id,
        "current_batch_id": current_batch_id,
        "baseline_artifacts": {
            **baseline_hashes,
        },
        "current_artifacts": {
            **current_hashes,
        },
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed_rows),
            "unchanged": unchanged,
        },
        "partition": {
            "baseline_total": len(before),
            "current_total": len(after),
            "baseline_identity": len(removed) + len(changed_rows) + unchanged,
            "current_identity": len(added) + len(changed_rows) + unchanged,
            "union_total": len(before_ids | after_ids),
            "union_identity": len(added) + len(removed) + len(changed_rows) + unchanged,
        },
        "change_types": {
            "classification": classification_changes,
            "tier": tier_changes,
            "winter_flag": flag_changes,
        },
        "transitions": dict(sorted(transition_counts.items())),
        "field_change_counts": dict(sorted(field_change_counts.items())),
        "added_row_ids": added,
        "removed_row_ids": removed,
        "changed_rows": changed_rows,
    }
    report["comparison_sha256"] = sha256_json(report)
    return report


def write_comparison_report(report: dict, path: Path) -> None:
    dump_json(report, path)
