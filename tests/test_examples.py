import json
import sys
from pathlib import Path

from sku_sleuth.models import sha256_file, sha256_json

sys.path.insert(0, "scripts")
from run_pipeline import main as run_pipeline  # noqa: E402


def test_committed_raw_csv_is_seed42():
    import tempfile

    from sku_sleuth.generate import generate, write_raw_csv

    with tempfile.TemporaryDirectory() as td:
        fresh = Path(td) / "raw.csv"
        write_raw_csv(generate(seed=42, rows=1200), fresh)
        assert fresh.read_bytes() == Path("data/raw_products.csv").read_bytes()


def test_examples_tell_the_story():
    passing = json.loads(Path("examples/passing_run/gate_report.json").read_text())
    failing = json.loads(Path("examples/failing_run/gate_report.json").read_text())
    assert passing["passed"] is True and failing["passed"] is False
    flag_check = next(c for c in failing["checks"] if c["name"] == "flag_precision")
    assert flag_check["passed"] is False
    p_load = json.loads(Path("examples/passing_run/load_result.json").read_text())
    f_load = json.loads(Path("examples/failing_run/load_result.json").read_text())
    assert p_load["status"] == "loaded"
    assert p_load["reconciliation"]["passed"] is True
    assert (f_load["status"], f_load["reason"]) == ("refused", "gate_failed")
    schema = json.loads(Path("examples/passing_run/schema_report.json").read_text())
    assert schema["passed"] is True and schema["drift_status"] == "none"
    comparison = json.loads(Path("examples/failing_run/comparison_report.json").read_text())
    assert comparison["counts"]["changed"] > 0
    assert comparison["change_types"]["winter_flag"] > 0


def test_example_batch_ids_match_committed_csv():
    passing = json.loads(Path("examples/passing_run/manifest.json").read_text())
    failing = json.loads(Path("examples/failing_run/manifest.json").read_text())
    for manifest in (passing, failing):
        assert manifest["batch_id"] == sha256_json(manifest["identity"])
        assert manifest["raw_sha256"] == sha256_file(Path("data/raw_products.csv"))


def test_committed_examples_are_byte_fresh(tmp_path):
    passing = tmp_path / "passing_run"
    failing = tmp_path / "failing_run"
    assert run_pipeline(["--scenario", "passing", "--workdir", str(passing)]) == 0
    assert (
        run_pipeline(
            [
                "--scenario",
                "failing",
                "--workdir",
                str(failing),
                "--baseline-dir",
                str(passing),
            ]
        )
        == 1
    )

    expected_passing = {
        "raw_products.csv",
        "schema_report.json",
        "decoded.jsonl",
        "rejects.jsonl",
        "manifest.json",
        "eval_report.json",
        "eval_report.md",
        "gate_report.json",
        "comparison_report.json",
        "lineage_comparison_report.json",
        "load_result.json",
        "lineage_report.json",
        "reconciliation_report.json",
        "run_meta.json",
    }
    expected_failing = expected_passing - {
        "lineage_comparison_report.json",
        "lineage_report.json",
        "reconciliation_report.json",
    }
    for generated, committed, expected in (
        (passing, Path("examples/passing_run"), expected_passing),
        (failing, Path("examples/failing_run"), expected_failing),
    ):
        committed_names = {path.name for path in committed.iterdir() if path.suffix != ".db"}
        generated_names = {path.name for path in generated.iterdir() if path.suffix != ".db"}
        assert committed_names == generated_names == expected
        for name in sorted(expected):
            assert (generated / name).read_bytes() == (committed / name).read_bytes(), name
