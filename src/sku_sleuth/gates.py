"""Declarative quality gates. Policy only — measurement lives in evaluate."""

from __future__ import annotations

import tomllib
from pathlib import Path

from sku_sleuth.models import dump_json, sha256_json

GATE_VERSION = "2"


def load_thresholds(path: Path = Path("gates.toml")) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def run_gate(eval_report: dict, manifest: dict, thresholds: dict) -> dict:
    if manifest["counts"]["total"] <= 0:
        raise ValueError("manifest total must be positive")
    expected_binding = {
        "batch_id": manifest["batch_id"],
        "decoded_sha256": manifest["decoded_sha256"],
        "rejects_sha256": manifest["rejects_sha256"],
        "manifest_sha256": sha256_json(manifest),
    }
    for name, expected in expected_binding.items():
        if eval_report.get(name) != expected:
            raise ValueError(f"evaluation/manifest binding mismatch: {name}")

    counts = manifest["counts"]
    quarantine_rate = round(counts["quarantined"] / counts["total"], 4)
    checks_spec = [
        ("flag_precision", eval_report["flag"]["precision"], ">="),
        ("category_accuracy", eval_report["category_accuracy"], ">="),
        ("coverage", eval_report["coverage"], ">="),
        ("error_rows", counts["errored"], "<="),
        ("quarantine_rate", quarantine_rate, "<="),
    ]
    checks = []
    for name, actual, op in checks_spec:
        threshold = thresholds[name]
        passed = actual >= threshold if op == ">=" else actual <= threshold
        checks.append(
            {
                "name": name,
                "actual": actual,
                "threshold": threshold,
                "op": op,
                "passed": passed,
            }
        )
    return {
        "gate_version": GATE_VERSION,
        "passed": all(c["passed"] for c in checks),
        "batch_id": eval_report["batch_id"],
        "decoded_sha256": eval_report["decoded_sha256"],
        "artifact_binding": {
            "batch_id": eval_report["batch_id"],
            "run_id": manifest["run_id"],
            "raw_sha256": manifest["raw_sha256"],
            "decoded_sha256": eval_report["decoded_sha256"],
            "rejects_sha256": eval_report["rejects_sha256"],
            "schema_report_sha256": manifest["schema_report_sha256"],
            "manifest_sha256": eval_report["manifest_sha256"],
            "eval_report_sha256": sha256_json(eval_report),
            "gold_sha256": eval_report["gold_sha256"],
            "evaluation_input_sha256": eval_report["evaluation_input_sha256"],
            "thresholds_sha256": sha256_json(thresholds),
        },
        "checks": checks,
    }


def write_gate_report(report: dict, path: Path) -> None:
    dump_json(report, path)
