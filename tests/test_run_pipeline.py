import json
import sys

sys.path.insert(0, "scripts")
import run_pipeline  # noqa: E402
from run_pipeline import main  # noqa: E402


def test_passing_scenario(tmp_path, capsys):
    code = main(["--scenario", "passing", "--workdir", str(tmp_path), "--rows", "1200"])
    assert code == 0
    out = capsys.readouterr().out
    assert "GATE: PASS" in out
    for name in (
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
        "products.db",
    ):
        assert (tmp_path / name).exists(), name
    load_result = json.loads((tmp_path / "load_result.json").read_text())
    assert load_result["status"] == "loaded"
    assert load_result["reconciliation"]["passed"] is True


def test_failing_scenario(tmp_path, capsys):
    code = main(["--scenario", "failing", "--workdir", str(tmp_path), "--rows", "1200"])
    assert code == 1
    out = capsys.readouterr().out
    assert "GATE: FAIL" in out
    load_result = json.loads((tmp_path / "load_result.json").read_text())
    assert (load_result["status"], load_result["reason"]) == ("refused", "gate_failed")


def test_failing_rerun_removes_prior_conditional_load_artifacts(tmp_path, capsys):
    passing_code = main(["--scenario", "passing", "--workdir", str(tmp_path), "--rows", "1200"])
    assert passing_code == 0
    for name in run_pipeline.CONDITIONAL_LOAD_ARTIFACTS:
        assert (tmp_path / name).exists(), name

    failing_code = main(["--scenario", "failing", "--workdir", str(tmp_path), "--rows", "1200"])

    assert failing_code == 1
    assert "GATE: FAIL" in capsys.readouterr().out
    load_result = json.loads((tmp_path / "load_result.json").read_text())
    assert (load_result["status"], load_result["reason"]) == ("refused", "gate_failed")
    for name in run_pipeline.CONDITIONAL_LOAD_ARTIFACTS:
        assert not (tmp_path / name).exists(), name


def test_passing_gate_is_not_success_when_load_or_reconciliation_refuses(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        run_pipeline,
        "load_batch",
        lambda *args, **kwargs: {
            "status": "refused",
            "reason": "simulated_conflict",
            "inserted": 0,
            "batch_id": None,
        },
    )
    code = run_pipeline.main(
        ["--scenario", "passing", "--workdir", str(tmp_path), "--rows", "1200"]
    )
    assert code == 2
    assert "gate passed but load/reconciliation did not complete" in capsys.readouterr().out
