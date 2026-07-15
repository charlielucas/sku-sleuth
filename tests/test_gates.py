from pathlib import Path

from sku_sleuth.gates import load_thresholds, run_gate, write_gate_report
from sku_sleuth.models import sha256_json


def report(manifest_value, flag_p=0.99, cat=0.95, cov=0.90):
    value = {
        "batch_id": "b" * 64,
        "decoded_sha256": "d" * 64,
        "rejects_sha256": "r" * 64,
        "manifest_sha256": sha256_json(manifest_value),
        "gold_sha256": "g" * 64,
        "evaluation_input_sha256": "i" * 64,
        "flag": {"precision": flag_p, "recall": 0.9, "tp": 49, "fp": 1, "fn": 5},
        "category_accuracy": cat,
        "coverage": cov,
    }
    return value


def manifest(errored=0, quarantined=30, total=1200):
    return {
        "batch_id": "b" * 64,
        "run_id": "u" * 64,
        "raw_sha256": "a" * 64,
        "decoded_sha256": "d" * 64,
        "rejects_sha256": "r" * 64,
        "schema_report_sha256": "s" * 64,
        "counts": {
            "total": total,
            "decoded": total - quarantined - errored,
            "abstained": 0,
            "quarantined": quarantined,
            "errored": errored,
        },
    }


def test_thresholds_load():
    t = load_thresholds(Path("gates.toml"))
    assert t == {
        "flag_precision": 0.97,
        "category_accuracy": 0.93,
        "coverage": 0.80,
        "error_rows": 0,
        "quarantine_rate": 0.10,
    }


def test_all_pass():
    m = manifest()
    g = run_gate(report(m), m, load_thresholds())
    assert g["passed"] is True
    assert [c["name"] for c in g["checks"]] == [
        "flag_precision",
        "category_accuracy",
        "coverage",
        "error_rows",
        "quarantine_rate",
    ]
    assert all(c["passed"] for c in g["checks"])
    assert g["batch_id"] == "b" * 64 and g["decoded_sha256"] == "d" * 64


def test_one_fp_passes_two_fps_fail():
    # with ~40 decoded TPs: one FP ≈ 0.975 >= 0.97, two FPs ≈ 0.952 < 0.97
    # (synthetic values below probe the same boundary)
    m = manifest()
    assert run_gate(report(m, flag_p=0.975), m, load_thresholds())["passed"] is True
    assert run_gate(report(m, flag_p=0.952), m, load_thresholds())["passed"] is False


def test_boundary_is_inclusive():
    assert (
        run_gate(
            report(manifest(), flag_p=0.97, cat=0.93, cov=0.80),
            manifest(),
            load_thresholds(),
        )["passed"]
        is True
    )


def test_errors_block():
    m = manifest(errored=1)
    g = run_gate(report(m), m, load_thresholds())
    assert g["passed"] is False
    check = next(c for c in g["checks"] if c["name"] == "error_rows")
    assert check["actual"] == 1 and check["passed"] is False


def test_quarantine_ceiling():
    m = manifest(quarantined=200)
    g = run_gate(report(m), m, load_thresholds())
    assert g["passed"] is False


def test_thresholds_are_a_parameter():
    strict = dict(load_thresholds(), flag_precision=1.0)
    m = manifest()
    assert run_gate(report(m, flag_p=0.99), m, strict)["passed"] is False


def test_report_written_byte_stable(tmp_path):
    m = manifest()
    g = run_gate(report(m), m, load_thresholds())
    p1, p2 = tmp_path / "1.json", tmp_path / "2.json"
    write_gate_report(g, p1)
    write_gate_report(g, p2)
    assert p1.read_bytes() == p2.read_bytes() and b"\r" not in p1.read_bytes()
