import csv
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sku_sleuth.compare import compare_batches, write_comparison_report
from sku_sleuth.engine import decode_stage
from sku_sleuth.evaluate import evaluate_batch, write_eval_report
from sku_sleuth.gates import load_thresholds, run_gate, write_gate_report
from sku_sleuth.generate import generate, write_raw_csv
from sku_sleuth.load import load_batch, read_registry, reconcile_batch
from sku_sleuth.models import dump_json, dump_jsonl, load_jsonl, sha256_file, sha256_json
from sku_sleuth.tiers.model import StubModel

FIXTURE = Path("seeds/stub_model_fixture.json")
GOLD = Path("seeds/gold_set.jsonl")


def gated_batch(
    tmp_path,
    rows=1200,
    name="b1",
    catalog_path=Path("seeds/catalog.csv"),
    model=None,
    additive_source=False,
):
    raw = tmp_path / f"{name}.csv"
    products = generate(seed=42, rows=rows)
    if additive_source:
        with open(raw, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["row_id", "sku", "title", "source"])
            for generated in products:
                product = generated.product
                writer.writerow([product.row_id, product.sku, product.title, "synthetic-fixture"])
    else:
        write_raw_csv(products, raw)
    out = tmp_path / name
    out.mkdir()
    manifest = decode_stage(
        raw,
        "guarded",
        model or StubModel.from_json(FIXTURE),
        out,
        catalog_path=catalog_path,
    )
    report = evaluate_batch(
        load_jsonl(out / "decoded.jsonl"),
        load_jsonl(out / "rejects.jsonl"),
        load_jsonl(GOLD),
        manifest,
    )
    write_eval_report(report, out / "eval_report.json")
    thresholds = load_thresholds()
    gate = run_gate(report, manifest, thresholds)
    write_gate_report(gate, out / "gate_report.json")
    comparison = compare_batches(
        [],
        load_jsonl(out / "decoded.jsonl"),
        current_rejects=load_jsonl(out / "rejects.jsonl"),
        current_batch_id=manifest["batch_id"],
        current_manifest=manifest,
    )
    write_comparison_report(comparison, out / "comparison_report.json")
    return out, raw, manifest, gate


def load_bundle(
    out,
    raw,
    db,
    *,
    decoded=None,
    gate=None,
    comparison=True,
    thresholds=None,
    gold=GOLD,
):
    return load_batch(
        decoded or out / "decoded.jsonl",
        gate or out / "gate_report.json",
        db,
        raw_path=raw,
        rejects_path=out / "rejects.jsonl",
        schema_report_path=out / "schema_report.json",
        manifest_path=out / "manifest.json",
        eval_report_path=out / "eval_report.json",
        gold_path=gold,
        thresholds=load_thresholds() if thresholds is None else thresholds,
        comparison_report_path=(out / "comparison_report.json") if comparison else None,
    )


def rebuild_downstream_evidence(out: Path, *, gold: Path = GOLD) -> dict:
    decoded = load_jsonl(out / "decoded.jsonl")
    rejects = load_jsonl(out / "rejects.jsonl")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["decoded_sha256"] = sha256_file(out / "decoded.jsonl")
    manifest["rejects_sha256"] = sha256_file(out / "rejects.jsonl")
    manifest["run_id"] = sha256_json(
        {
            "batch_id": manifest["batch_id"],
            "decoded_sha256": manifest["decoded_sha256"],
            "rejects_sha256": manifest["rejects_sha256"],
        }
    )
    dump_json(manifest, out / "manifest.json")
    report = evaluate_batch(decoded, rejects, load_jsonl(gold), manifest)
    write_eval_report(report, out / "eval_report.json")
    gate = run_gate(report, manifest, load_thresholds())
    write_gate_report(gate, out / "gate_report.json")
    comparison = compare_batches(
        [],
        decoded,
        current_rejects=rejects,
        current_batch_id=manifest["batch_id"],
        current_manifest=manifest,
    )
    write_comparison_report(comparison, out / "comparison_report.json")
    return manifest


def write_policy_gate(out: Path, manifest: dict, thresholds: dict, name: str) -> Path:
    report = json.loads((out / "eval_report.json").read_text(encoding="utf-8"))
    gate = run_gate(report, manifest, thresholds)
    assert gate["passed"] is True
    path = out / f"{name}_gate_report.json"
    write_gate_report(gate, path)
    return path


def test_load_and_reload_noop(tmp_path):
    out, raw, manifest, gate = gated_batch(tmp_path)
    assert gate["passed"] is True
    db = tmp_path / "p.db"
    r1 = load_bundle(out, raw, db)
    assert r1["status"] == "loaded" and r1["inserted"] > 900
    before = db.read_bytes()
    r2 = load_bundle(out, raw, db)
    assert r2["status"] == "noop" and r2["inserted"] == 0
    assert db.read_bytes() == before


def test_refuses_without_gate(tmp_path):
    out, raw, *_ = gated_batch(tmp_path)
    r = load_bundle(out, raw, tmp_path / "p.db", gate=out / "missing.json")
    assert (r["status"], r["reason"]) == ("refused", "missing_gate_report")


def test_refuses_failed_gate(tmp_path):
    out, raw, manifest, gate = gated_batch(tmp_path)
    gate_failed = dict(gate, passed=False)
    p = out / "gate_failed.json"
    write_gate_report(gate_failed, p)
    r = load_bundle(out, raw, tmp_path / "p.db", gate=p)
    assert (r["status"], r["reason"]) == ("refused", "gate_failed")


def test_refuses_tampered_artifact(tmp_path):
    out, raw, *_ = gated_batch(tmp_path)
    decoded = out / "decoded.jsonl"
    data = bytearray(decoded.read_bytes())
    data[len(data) // 2] ^= 0x01
    decoded.write_bytes(bytes(data))
    r = load_bundle(out, raw, tmp_path / "p.db", decoded=decoded)
    assert r["status"] == "refused"
    assert r["reason"] in {"malformed_decoded", "artifact_binding_mismatch"}


def test_incremental_superset_batch(tmp_path):
    out1, raw1, m1, _ = gated_batch(tmp_path, rows=1200, name="b1")
    out2, raw2, m2, _ = gated_batch(tmp_path, rows=1400, name="b2")
    assert m1["batch_id"] != m2["batch_id"]
    db = tmp_path / "p.db"
    r1 = load_bundle(out1, raw1, db)
    r2 = load_bundle(out2, raw2, db, comparison=False)
    assert r2["status"] == "loaded"
    assert 0 < r2["inserted"] < 250  # only the ~200 new rows land
    registry = read_registry(db)
    assert [b["batch_id"] for b in registry] == [m1["batch_id"], m2["batch_id"]]
    assert registry[0]["inserted"] == r1["inserted"]


def test_removed_snapshot_is_visible_and_exact_reapply_is_still_noop(tmp_path):
    big, big_raw, _, _ = gated_batch(tmp_path, rows=1400, name="big")
    small, small_raw, _, _ = gated_batch(tmp_path, rows=1200, name="small")
    db = tmp_path / "p.db"
    assert load_bundle(big, big_raw, db)["status"] == "loaded"
    smaller = load_bundle(small, small_raw, db, comparison=False)
    assert smaller["status"] == "loaded"
    assert smaller["inserted"] == 0
    assert smaller["lineage"]["removed"] > 0
    repeated = load_bundle(small, small_raw, db, comparison=False)
    assert repeated["status"] == "noop"
    assert repeated["reconciliation"]["passed"] is True


def test_row_content_conflict_is_refused_not_ignored(tmp_path):
    first, first_raw, _, _ = gated_batch(tmp_path, name="first")
    catalog = tmp_path / "changed_catalog.csv"
    original = Path("seeds/catalog.csv").read_text(encoding="utf-8")
    catalog.write_text(
        original.replace(
            "BTX-5012,Electrical,Ignition Coils,,,,,0",
            "BTX-5012,Braking,Brake Pads,,,,,0",
        ),
        encoding="utf-8",
    )
    second, second_raw, _, gate = gated_batch(tmp_path, name="second", catalog_path=catalog)
    assert gate["passed"] is True
    db = tmp_path / "p.db"
    assert load_bundle(first, first_raw, db)["status"] == "loaded"
    conflict = load_bundle(second, second_raw, db)
    assert (conflict["status"], conflict["reason"]) == ("refused", "row_content_conflict")


def test_mixed_bundle_is_refused(tmp_path):
    first, first_raw, _, _ = gated_batch(tmp_path, rows=1200, name="first")
    second, _, _, _ = gated_batch(tmp_path, rows=1400, name="second")
    result = load_bundle(
        first,
        first_raw,
        tmp_path / "p.db",
        decoded=second / "decoded.jsonl",
    )
    assert result["status"] == "refused"
    assert result["reason"] == "source_outcome_row_set_mismatch"


class FixedIdentityStub(StubModel):
    def identity(self):
        return {"adapter": "same-external-model", "model_id": "v1", "deterministic": False}


def test_same_batch_id_with_different_run_artifact_is_hard_conflict(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed_fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    key = next(iter(changed_fixture))
    changed_fixture[key] = {
        "category": "Braking",
        "subcategory": "Brake Pads",
        "attributes": {},
        "is_winter_rated": False,
    }
    first, first_raw, first_manifest, first_gate = gated_batch(
        tmp_path, name="first-run", model=FixedIdentityStub(fixture)
    )
    second, second_raw, second_manifest, second_gate = gated_batch(
        tmp_path, name="second-run", model=FixedIdentityStub(changed_fixture)
    )
    assert first_gate["passed"] is True and second_gate["passed"] is True
    assert first_manifest["batch_id"] == second_manifest["batch_id"]
    assert first_manifest["run_id"] != second_manifest["run_id"]
    db = tmp_path / "p.db"
    assert load_bundle(first, first_raw, db)["status"] == "loaded"
    conflict = load_bundle(second, second_raw, db)
    assert (conflict["status"], conflict["reason"]) == ("refused", "batch_id_conflict")


def test_reconciliation_detects_delete_insert_payload_and_provenance_mutations(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    conn = sqlite3.connect(db)
    try:
        row_ids = [
            row[0] for row in conn.execute("SELECT row_id FROM products ORDER BY row_id LIMIT 3")
        ]
        conn.execute("UPDATE products SET sku = 'MUTATED' WHERE row_id = ?", (row_ids[0],))
        conn.execute(
            "UPDATE products SET batch_id = 'wrong-origin' WHERE row_id = ?", (row_ids[1],)
        )
        conn.execute("DELETE FROM products WHERE row_id = ?", (row_ids[2],))
        conn.execute(
            "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
            ("EXTRA", "S", "Braking", "Brake Pads", "{}", 0, "rules", "e", manifest["batch_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert row_ids[0] in report["payload_mutation_row_ids"]
    assert row_ids[1] in report["provenance_mutation_row_ids"]
    assert row_ids[2] in report["missing_row_ids"]
    assert "EXTRA" in report["unexpected_row_ids"]
    assert not any(
        reason.startswith("load_reconciliation_") for reason in report["metadata_mutation_reasons"]
    )


def test_comparison_is_replayed_from_the_registered_parent(tmp_path):
    first, first_raw, first_manifest, _ = gated_batch(tmp_path, rows=1200, name="first")
    second, second_raw, second_manifest, _ = gated_batch(tmp_path, rows=1400, name="second")
    db = tmp_path / "p.db"
    assert load_bundle(first, first_raw, db)["status"] == "loaded"
    comparison = compare_batches(
        load_jsonl(first / "decoded.jsonl"),
        load_jsonl(second / "decoded.jsonl"),
        baseline_rejects=load_jsonl(first / "rejects.jsonl"),
        current_rejects=load_jsonl(second / "rejects.jsonl"),
        baseline_batch_id=first_manifest["batch_id"],
        current_batch_id=second_manifest["batch_id"],
        baseline_manifest=first_manifest,
        current_manifest=second_manifest,
    )
    write_comparison_report(comparison, second / "comparison_report.json")

    result = load_bundle(second, second_raw, db)
    assert result["status"] == "loaded"
    assert result["lineage"]["parent_batch_id"] == first_manifest["batch_id"]
    assert result["comparison"] == comparison


def test_edited_self_rehashed_comparison_is_refused(tmp_path):
    first, first_raw, first_manifest, _ = gated_batch(tmp_path, rows=1200, name="first")
    second, second_raw, second_manifest, _ = gated_batch(tmp_path, rows=1400, name="second")
    db = tmp_path / "p.db"
    assert load_bundle(first, first_raw, db)["status"] == "loaded"
    forged = compare_batches(
        load_jsonl(first / "decoded.jsonl"),
        load_jsonl(second / "decoded.jsonl"),
        baseline_rejects=load_jsonl(first / "rejects.jsonl"),
        current_rejects=load_jsonl(second / "rejects.jsonl"),
        baseline_batch_id=first_manifest["batch_id"],
        current_batch_id=second_manifest["batch_id"],
        baseline_manifest=first_manifest,
        current_manifest=second_manifest,
    )
    forged["counts"]["added"] += 1
    forged["comparison_sha256"] = sha256_json(
        {name: value for name, value in forged.items() if name != "comparison_sha256"}
    )
    write_comparison_report(forged, second / "comparison_report.json")

    refused = load_bundle(second, second_raw, db)
    assert (refused["status"], refused["reason"]) == (
        "refused",
        "comparison_replay_mismatch",
    )
    assert [row["batch_id"] for row in read_registry(db)] == [first_manifest["batch_id"]]


@pytest.mark.parametrize(
    ("statement", "expected_reason"),
    [
        (
            "DELETE FROM batch_rows WHERE rowid = "
            "(SELECT rowid FROM batch_rows WHERE batch_id = ? LIMIT 1)",
            "snapshot_count_mismatch",
        ),
        (
            "UPDATE batch_rows SET payload_sha256 = printf('%064d', 0) "
            "WHERE rowid = (SELECT rowid FROM batch_rows WHERE batch_id = ? LIMIT 1)",
            "snapshot_hash_mismatch",
        ),
        (
            "DELETE FROM batch_outcomes WHERE rowid = "
            "(SELECT rowid FROM batch_outcomes WHERE batch_id = ? LIMIT 1)",
            "outcome_count_mismatch",
        ),
        (
            "UPDATE batch_outcomes SET payload_json = '{}' WHERE rowid = "
            "(SELECT rowid FROM batch_outcomes WHERE batch_id = ? LIMIT 1)",
            "outcome_hash_mismatch",
        ),
        (
            "DELETE FROM batch_evidence WHERE batch_id = ?",
            "missing_batch_evidence",
        ),
        (
            "DELETE FROM load_reconciliation WHERE batch_id = ?",
            "missing_reconciliation_anchor",
        ),
        (
            "UPDATE batch_evidence SET snapshot_rows = snapshot_rows + 1 WHERE batch_id = ?",
            "snapshot_count_mismatch",
        ),
        (
            "UPDATE load_reconciliation SET batch_rows_sha256 = printf('%064d', 0) "
            "WHERE batch_id = ?",
            "snapshot_hash_mismatch",
        ),
    ],
)
def test_reconciliation_detects_snapshot_metadata_delete_or_edit(
    tmp_path, statement, expected_reason
):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute(statement, (manifest["batch_id"],))
    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert any(expected_reason in reason for reason in report["metadata_mutation_reasons"])


def test_reconciliation_reports_malformed_database_values_without_coercion(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        row_ids = [
            row[0] for row in conn.execute("SELECT row_id FROM products ORDER BY row_id LIMIT 2")
        ]
        conn.execute("UPDATE products SET attributes_json = '{' WHERE row_id = ?", (row_ids[0],))
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE products SET is_winter_rated = 2 WHERE row_id = ?", (row_ids[1],))
    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert report["database_schema_mutations"][row_ids[0]] == ["attributes_json_malformed"]
    assert report["database_schema_mutations"][row_ids[1]] == ["is_winter_rated_out_of_domain"]
    assert row_ids[0] in report["payload_mutation_row_ids"]
    assert row_ids[1] in report["payload_mutation_row_ids"]


def test_database_rejects_out_of_domain_winter_integers(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        row_id = conn.execute("SELECT row_id FROM products LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute("UPDATE products SET is_winter_rated = 2 WHERE row_id = ?", (row_id,))


def test_concurrent_exact_loads_are_idempotent(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: load_bundle(out, raw, db), range(8)))
    assert [result["status"] for result in results].count("loaded") == 1
    assert [result["status"] for result in results].count("noop") == 7
    assert all(result["batch_id"] == manifest["batch_id"] for result in results)
    assert len(read_registry(db)) == 1


def test_same_measurement_can_record_a_new_passing_policy_decision(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    thresholds = {
        "flag_precision": 0.974,
        "category_accuracy": 0.98,
        "coverage": 0.95,
        "error_rows": 0,
        "quarantine_rate": 0.03,
    }
    report = json.loads((out / "eval_report.json").read_text(encoding="utf-8"))
    revised_gate = run_gate(report, manifest, thresholds)
    assert revised_gate["passed"] is True
    revised_gate_path = out / "revised_gate_report.json"
    write_gate_report(revised_gate, revised_gate_path)
    revised = load_bundle(out, raw, db, gate=revised_gate_path, thresholds=thresholds)
    assert revised["status"] == "noop"
    assert revised["decision_recorded"] is True
    assert read_registry(db)[0]["decisions"] == 2
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT decision_count FROM load_reconciliation WHERE batch_id = ?",
                (manifest["batch_id"],),
            ).fetchone()[0]
            == 2
        )
    assert reconcile_batch(db, manifest["batch_id"])["passed"] is True
    before = db.read_bytes()
    exact_replay = load_bundle(out, raw, db, gate=revised_gate_path, thresholds=thresholds)
    assert exact_replay["status"] == "noop"
    assert exact_replay["decision_recorded"] is False
    assert db.read_bytes() == before


@pytest.mark.parametrize("deleted_index", [0, 1])
def test_deleting_either_policy_decision_breaks_the_anchored_history(tmp_path, deleted_index):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    thresholds = {
        "flag_precision": 0.974,
        "category_accuracy": 0.98,
        "coverage": 0.95,
        "error_rows": 0,
        "quarantine_rate": 0.03,
    }
    gate_path = write_policy_gate(out, manifest, thresholds, "second_policy")
    assert (
        load_bundle(out, raw, db, gate=gate_path, thresholds=thresholds)["decision_recorded"]
        is True
    )
    with sqlite3.connect(db) as conn:
        decision_ids = [
            row[0]
            for row in conn.execute("SELECT decision_id FROM gate_decisions ORDER BY decision_id")
        ]
        assert len(decision_ids) == 2
        conn.execute(
            "DELETE FROM gate_decisions WHERE decision_id = ?", (decision_ids[deleted_index],)
        )
    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert "reconciliation_decision_count_mismatch" in report["metadata_mutation_reasons"]
    assert "reconciliation_decisions_hash_mismatch" in report["metadata_mutation_reasons"]


@pytest.mark.parametrize("corruption", ["delete", "inject"])
def test_corrupt_policy_history_cannot_be_absorbed_by_a_third_policy(tmp_path, corruption):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    second_thresholds = {
        "flag_precision": 0.974,
        "category_accuracy": 0.98,
        "coverage": 0.95,
        "error_rows": 0,
        "quarantine_rate": 0.03,
    }
    second_gate = write_policy_gate(out, manifest, second_thresholds, "second_policy")
    assert (
        load_bundle(out, raw, db, gate=second_gate, thresholds=second_thresholds)[
            "decision_recorded"
        ]
        is True
    )

    with sqlite3.connect(db) as conn:
        if corruption == "delete":
            conn.execute(
                "DELETE FROM gate_decisions WHERE decision_id = "
                "(SELECT decision_id FROM gate_decisions ORDER BY decision_id LIMIT 1)"
            )
        else:
            conn.execute(
                "INSERT INTO gate_decisions VALUES (?,?,?,?,?,?)",
                ("c" * 64, manifest["batch_id"], "now", "a" * 64, "b" * 64, 1),
            )
        before_rows = conn.execute(
            "SELECT decision_id, batch_id, gate_report_sha256, thresholds_sha256, passed "
            "FROM gate_decisions ORDER BY decision_id"
        ).fetchall()
        before_anchor = conn.execute(
            "SELECT decision_count, decisions_sha256 FROM load_reconciliation WHERE batch_id = ?",
            (manifest["batch_id"],),
        ).fetchone()

    third_thresholds = {
        "flag_precision": 0.973,
        "category_accuracy": 0.979,
        "coverage": 0.94,
        "error_rows": 0,
        "quarantine_rate": 0.04,
    }
    third_gate = write_policy_gate(out, manifest, third_thresholds, "third_policy")
    refused = load_bundle(out, raw, db, gate=third_gate, thresholds=third_thresholds)
    assert (refused["status"], refused["reason"]) == ("refused", "reconciliation_failed")
    with sqlite3.connect(db) as conn:
        after_rows = conn.execute(
            "SELECT decision_id, batch_id, gate_report_sha256, thresholds_sha256, passed "
            "FROM gate_decisions ORDER BY decision_id"
        ).fetchall()
        after_anchor = conn.execute(
            "SELECT decision_count, decisions_sha256 FROM load_reconciliation WHERE batch_id = ?",
            (manifest["batch_id"],),
        ).fetchone()
    assert after_rows == before_rows
    assert after_anchor == before_anchor


def test_concurrent_new_policy_rejudgments_update_one_decision_anchor(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    thresholds = {
        "flag_precision": 0.974,
        "category_accuracy": 0.98,
        "coverage": 0.95,
        "error_rows": 0,
        "quarantine_rate": 0.03,
    }
    gate_path = write_policy_gate(out, manifest, thresholds, "concurrent_policy")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: load_bundle(out, raw, db, gate=gate_path, thresholds=thresholds),
                range(8),
            )
        )
    assert all(result["status"] == "noop" for result in results)
    assert sum(result["decision_recorded"] for result in results) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM gate_decisions").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT decision_count FROM load_reconciliation WHERE batch_id = ?",
                (manifest["batch_id"],),
            ).fetchone()[0]
            == 2
        )
    assert reconcile_batch(db, manifest["batch_id"])["passed"] is True


@pytest.mark.parametrize(
    ("artifact", "mutation"),
    [
        ("schema_report.json", lambda value: []),
        ("manifest.json", lambda value: value | {"counts": "not-an-object"}),
        ("eval_report.json", lambda value: value | {"flag": []}),
        (
            "gate_report.json",
            lambda value: value
            | {"checks": [value["checks"][0] | {"passed": 1}, *value["checks"][1:]]},
        ),
        ("comparison_report.json", lambda value: value | {"changed_rows": {}}),
    ],
)
def test_malformed_nested_reports_are_structured_refusals(tmp_path, artifact, mutation):
    out, raw, _, _ = gated_batch(tmp_path)
    path = out / artifact
    value = json.loads(path.read_text(encoding="utf-8"))
    dump_json(mutation(value), path)
    result = load_bundle(out, raw, tmp_path / "p.db")
    assert result["status"] == "refused"
    assert isinstance(result["reason"], str) and result["reason"].startswith("malformed_")


def test_loader_applies_shared_taxonomy_validation_to_decoded_rows(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    rows = load_jsonl(out / "decoded.jsonl")
    rows[0]["category"] = "Unknown"
    rows[0]["subcategory"] = "Made Up"
    with open(out / "decoded.jsonl", "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    refused = load_bundle(out, raw, tmp_path / "p.db")
    assert (refused["status"], refused["reason"]) == (
        "refused",
        "schema_contract_failed",
    )


def test_reconciliation_is_read_only_even_for_missing_or_unknown_databases(tmp_path):
    missing = tmp_path / "missing.db"
    assert reconcile_batch(missing, "unknown")["reason"] == "missing_database"
    assert not missing.exists()

    unknown = tmp_path / "unknown.db"
    with sqlite3.connect(unknown) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('preserve me')")
    before = unknown.read_bytes()
    report = reconcile_batch(unknown, "unknown")
    assert report["reason"] == "database_schema_missing"
    assert unknown.read_bytes() == before


def test_registry_schema_declares_and_enforces_foreign_keys(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        targets = {row[2] for row in conn.execute("PRAGMA foreign_key_list(products)")}
        assert "batches" in targets
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            conn.execute(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
                ("FOREIGN", "S", "Braking", "Brake Pads", "{}", 0, "rules", "e", "none"),
            )


def test_incompatible_existing_registry_is_a_structured_refusal(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE batch_evidence (batch_id TEXT PRIMARY KEY)")
        before_schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    before_bytes = db.read_bytes()
    result = load_bundle(out, raw, db)
    assert (result["status"], result["reason"]) == (
        "refused",
        "database_schema_incompatible",
    )
    with sqlite3.connect(db) as conn:
        after_schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after_schema == before_schema
    assert db.read_bytes() == before_bytes


def test_self_consistent_additive_schema_projection_loads(tmp_path):
    out, raw, manifest, gate = gated_batch(tmp_path, additive_source=True)
    assert gate["passed"] is True
    assert manifest["schema"]["drift_status"] == "additive"
    assert manifest["schema"]["drift_events"] == [{"kind": "additive_field", "field": "source"}]
    assert load_bundle(out, raw, tmp_path / "p.db")["status"] == "loaded"


def test_manifest_schema_projection_must_match_verified_schema_report(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"]["drift_status"] = "additive"
    manifest["schema"]["extra_fields"] = ["source"]
    manifest["schema"]["drift_events"] = [{"kind": "additive_field", "field": "source"}]
    dump_json(manifest, manifest_path)
    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "schema_projection_mismatch",
    )
    assert result["details"] == ["drift_events", "drift_status", "extra_fields"]


def test_manifest_schema_rejects_nonsensical_drift_status(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"]["drift_status"] = "surprising"
    dump_json(manifest, manifest_path)
    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "malformed_manifest.schema.drift_status",
    )


@pytest.mark.parametrize(
    "event",
    [
        None,
        {"kind": "not-a-real-event"},
        {"kind": "additive_field", "field": None},
        {"kind": "invalid_utf8", "field": "unexpected"},
    ],
)
def test_manifest_schema_rejects_null_or_invalid_drift_events(tmp_path, event):
    out, raw, _, _ = gated_batch(tmp_path)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"]["drift_events"] = [event]
    dump_json(manifest, manifest_path)
    result = load_bundle(out, raw, tmp_path / "p.db")
    assert result["status"] == "refused"
    assert result["reason"].startswith("malformed_manifest.schema.drift_events[0]")


@pytest.mark.parametrize(
    ("column", "value", "reason_fragment"),
    [
        ("gate_report_sha256", "0" * 64, "gate_decision_id_mismatch"),
        ("thresholds_sha256", "1" * 64, "gate_decision_id_mismatch"),
        ("passed", 0, "gate_decision_passed_mismatch"),
        ("batch_id", "wrong-batch-link", "gate_decision_batch_link_mismatch"),
    ],
)
def test_decision_content_mutations_fail_reconcile_and_exact_load(
    tmp_path, column, value, reason_fragment
):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute(f"UPDATE gate_decisions SET {column} = ?", (value,))

    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert any(reason_fragment in reason for reason in report["metadata_mutation_reasons"])
    replay = load_bundle(out, raw, db)
    assert (replay["status"], replay["reason"]) == ("refused", "gate_decision_mismatch")
    assert column in replay["details"]


@pytest.mark.parametrize("column", ["added", "removed", "changed", "unchanged"])
def test_lineage_count_mutations_fail_reconcile_and_exact_load(tmp_path, column):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute(f"UPDATE batch_lineage SET {column} = {column} + 1")

    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert f"batch_lineage_{column}_mismatch" in report["metadata_mutation_reasons"]
    replay = load_bundle(out, raw, db)
    assert (replay["status"], replay["reason"]) == (
        "refused",
        "lineage_metadata_mismatch",
    )
    assert replay["details"] == [column]


def test_derivable_batch_and_evidence_mutations_are_detected(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    batch_id = manifest["batch_id"]
    mutations = [
        (
            "batches",
            "decoded_sha256",
            "0" * 64,
            "batch_registry_decoded_hash_mismatch",
        ),
        (
            "batches",
            "inserted",
            999999,
            "batch_registry_inserted_mismatch",
        ),
        (
            "batch_evidence",
            "run_id",
            "1" * 64,
            "batch_evidence_run_id_mismatch",
        ),
        (
            "batch_evidence",
            "incoming_rows",
            999999,
            "batch_evidence_incoming_count_mismatch",
        ),
    ]
    with sqlite3.connect(db) as conn:
        for table, column, value, reason in mutations:
            original = conn.execute(
                f"SELECT {column} FROM {table} WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE batch_id = ?", (value, batch_id))
            conn.commit()
            report = reconcile_batch(db, batch_id)
            assert report["passed"] is False
            assert reason in report["metadata_mutation_reasons"]
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE batch_id = ?", (original, batch_id)
            )
            conn.commit()


@pytest.mark.parametrize(
    "field",
    ("manifest_sha256", "raw_sha256", "schema_report_sha256", "gold_sha256"),
)
def test_decision_identity_binds_non_derivable_measurement_evidence(tmp_path, field):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"UPDATE batch_evidence SET {field} = ? WHERE batch_id = ?",
            ("f" * 64, manifest["batch_id"]),
        )
    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert any(
        reason.startswith("gate_decision_id_mismatch:")
        for reason in report["metadata_mutation_reasons"]
    )


def test_reconciliation_summary_mutations_are_detected(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    assert load_bundle(out, raw, db)["status"] == "loaded"
    batch_id = manifest["batch_id"]
    fields = (
        "passed",
        "expected_rows",
        "verified_rows",
        "missing_rows",
        "unexpected_rows",
        "payload_mutations",
        "provenance_mutations",
        "database_schema_mutations",
        "metadata_mutations",
        "decision_count",
    )
    with sqlite3.connect(db) as conn:
        for field in fields:
            original = conn.execute(
                f"SELECT {field} FROM load_reconciliation WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            if field == "passed":
                conn.execute(
                    "UPDATE load_reconciliation SET passed = 0 WHERE batch_id = ?", (batch_id,)
                )
            else:
                conn.execute(
                    f"UPDATE load_reconciliation SET {field} = {field} + 1 WHERE batch_id = ?",
                    (batch_id,),
                )
            conn.commit()
            report = reconcile_batch(db, batch_id)
            assert report["passed"] is False
            assert f"load_reconciliation_{field}_mismatch" in report["metadata_mutation_reasons"]
            conn.execute(
                f"UPDATE load_reconciliation SET {field} = ? WHERE batch_id = ?",
                (original, batch_id),
            )
            conn.commit()

        for field, reason_fragment in (
            ("source_rowset_sha256", "load_reconciliation_source_rowset_sha256_mismatch"),
            ("database_rowset_sha256", "load_reconciliation_database_rowset_sha256_mismatch"),
            ("outcomes_sha256", "reconciliation_outcome_hash_mismatch"),
            ("decisions_sha256", "reconciliation_decisions_hash_mismatch"),
        ):
            original = conn.execute(
                f"SELECT {field} FROM load_reconciliation WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            conn.execute(
                f"UPDATE load_reconciliation SET {field} = ? WHERE batch_id = ?",
                ("0" * 64, batch_id),
            )
            conn.commit()
            report = reconcile_batch(db, batch_id)
            assert report["passed"] is False
            assert any(reason_fragment in reason for reason in report["metadata_mutation_reasons"])
            conn.execute(
                f"UPDATE load_reconciliation SET {field} = ? WHERE batch_id = ?",
                (original, batch_id),
            )
            conn.commit()


def test_failed_parent_cannot_be_laundered_into_a_child_snapshot(tmp_path):
    parent, parent_raw, parent_manifest, _ = gated_batch(tmp_path, rows=1200, name="parent")
    child, child_raw, child_manifest, _ = gated_batch(tmp_path, rows=1400, name="child")
    db = tmp_path / "p.db"
    assert load_bundle(parent, parent_raw, db)["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "EXTRA",
                "S",
                "Braking",
                "Brake Pads",
                "{}",
                0,
                "rules",
                "external",
                parent_manifest["batch_id"],
            ),
        )
    parent_failure = reconcile_batch(db, parent_manifest["batch_id"])
    assert parent_failure["passed"] is False
    assert parent_failure["unexpected_row_ids"] == ["EXTRA"]
    before = db.read_bytes()

    refused = load_bundle(child, child_raw, db, comparison=False)
    assert (refused["status"], refused["reason"]) == (
        "refused",
        "parent_reconciliation_failed",
    )
    assert db.read_bytes() == before
    assert child_manifest["batch_id"] not in {row["batch_id"] for row in read_registry(db)}
    assert reconcile_batch(db, parent_manifest["batch_id"])["passed"] is False


def test_historical_reconciliation_allows_only_latest_anchored_rows(tmp_path):
    parent, parent_raw, parent_manifest, _ = gated_batch(tmp_path, rows=1200, name="parent")
    child, child_raw, child_manifest, _ = gated_batch(tmp_path, rows=1400, name="child")
    db = tmp_path / "p.db"
    assert load_bundle(parent, parent_raw, db)["status"] == "loaded"
    assert load_bundle(child, child_raw, db, comparison=False)["status"] == "loaded"
    assert reconcile_batch(db, parent_manifest["batch_id"])["passed"] is True
    assert reconcile_batch(db, child_manifest["batch_id"])["passed"] is True

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "UNREGISTERED",
                "S",
                "Braking",
                "Brake Pads",
                "{}",
                0,
                "rules",
                "external",
                child_manifest["batch_id"],
            ),
        )
    for batch_id in (parent_manifest["batch_id"], child_manifest["batch_id"]):
        report = reconcile_batch(db, batch_id)
        assert report["passed"] is False
        assert report["unexpected_row_ids"] == ["UNREGISTERED"]


def test_malformed_unexpected_row_does_not_reduce_verified_expected_rows(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    db = tmp_path / "p.db"
    loaded = load_bundle(out, raw, db)
    assert loaded["status"] == "loaded"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "MALFORMED_EXTRA",
                "S",
                "Braking",
                "Brake Pads",
                "{",
                0,
                "rules",
                "external",
                manifest["batch_id"],
            ),
        )
    report = reconcile_batch(db, manifest["batch_id"])
    assert report["passed"] is False
    assert report["unexpected_row_ids"] == ["MALFORMED_EXTRA"]
    assert "MALFORMED_EXTRA" in report["database_schema_mutations"]
    assert report["verified_rows"] == report["expected_rows"]


def test_manifest_top_level_projection_cannot_contradict_identity(tmp_path):
    out, raw, manifest, _ = gated_batch(tmp_path)
    manifest["total"] += 1
    manifest["decoder_version"] = "different-decoder"
    manifest["decode_schema_version"] = "different-schema"
    manifest["ruleset"] = "different-ruleset"
    manifest["identity"]["contract"]["raw_schema_version"] = "different-raw-schema"
    manifest["batch_id"] = sha256_json(manifest["identity"])
    manifest["run_id"] = sha256_json(
        {
            "batch_id": manifest["batch_id"],
            "decoded_sha256": manifest["decoded_sha256"],
            "rejects_sha256": manifest["rejects_sha256"],
        }
    )
    dump_json(manifest, out / "manifest.json")
    report = evaluate_batch(
        load_jsonl(out / "decoded.jsonl"),
        load_jsonl(out / "rejects.jsonl"),
        load_jsonl(GOLD),
        manifest,
    )
    write_eval_report(report, out / "eval_report.json")
    gate = run_gate(report, manifest, load_thresholds())
    write_gate_report(gate, out / "gate_report.json")
    comparison = compare_batches(
        [],
        load_jsonl(out / "decoded.jsonl"),
        current_rejects=load_jsonl(out / "rejects.jsonl"),
        current_batch_id=manifest["batch_id"],
        current_manifest=manifest,
    )
    write_comparison_report(comparison, out / "comparison_report.json")

    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "manifest_projection_mismatch",
    )
    assert result["details"] == [
        "decode_schema_version",
        "decoder_version",
        "raw_schema_version",
        "ruleset",
        "total",
    ]


def test_outcome_row_set_must_exactly_partition_raw_source(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    decoded = load_jsonl(out / "decoded.jsonl")
    gold_ids = {row["row_id"] for row in load_jsonl(GOLD)}
    row = next(row for row in decoded if row["row_id"] not in gold_ids)
    missing_row_id = row["row_id"]
    row["row_id"] = "INVENTED_OUTCOME"
    dump_jsonl(decoded, out / "decoded.jsonl")
    rebuild_downstream_evidence(out)

    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "source_outcome_row_set_mismatch",
    )
    assert result["details"] == {
        "missing_outcome_row_ids": [missing_row_id],
        "unexpected_outcome_row_ids": ["INVENTED_OUTCOME"],
    }


def test_decoded_sku_must_match_raw_source_after_full_rebind(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    decoded = load_jsonl(out / "decoded.jsonl")
    gold_ids = {row["row_id"] for row in load_jsonl(GOLD)}
    row = next(row for row in decoded if row["row_id"] not in gold_ids)
    row["sku"] = "ALTERED-SKU"
    row_id = row["row_id"]
    dump_jsonl(decoded, out / "decoded.jsonl")
    rebuild_downstream_evidence(out)

    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "source_outcome_sku_mismatch",
    )
    assert result["details"] == [row_id]


def test_reject_title_must_match_raw_source_after_full_rebind(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    rejects = load_jsonl(out / "rejects.jsonl")
    gold_ids = {row["row_id"] for row in load_jsonl(GOLD)}
    row = next(row for row in rejects if row["row_id"] not in gold_ids)
    row["title"] = "ALTERED SOURCE TITLE"
    row_id = row["row_id"]
    dump_jsonl(rejects, out / "rejects.jsonl")
    rebuild_downstream_evidence(out)

    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "source_reject_title_mismatch",
    )
    assert result["details"] == [row_id]


def test_reject_sku_must_match_raw_source_after_full_rebind(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    rejects = load_jsonl(out / "rejects.jsonl")
    gold_ids = {row["row_id"] for row in load_jsonl(GOLD)}
    row = next(row for row in rejects if row["row_id"] not in gold_ids)
    row["sku"] = "ALTERED-REJECT-SKU"
    row_id = row["row_id"]
    dump_jsonl(rejects, out / "rejects.jsonl")
    rebuild_downstream_evidence(out)

    result = load_bundle(out, raw, tmp_path / "p.db")
    assert (result["status"], result["reason"]) == (
        "refused",
        "source_outcome_sku_mismatch",
    )
    assert result["details"] == [row_id]


def test_gold_title_must_match_raw_source_after_full_rebind(tmp_path):
    out, raw, _, _ = gated_batch(tmp_path)
    gold = load_jsonl(GOLD)
    gold[0]["title"] = "STALE GOLD TITLE"
    row_id = gold[0]["row_id"]
    gold_path = tmp_path / "gold.jsonl"
    dump_jsonl(gold, gold_path)
    rebuild_downstream_evidence(out, gold=gold_path)

    result = load_bundle(out, raw, tmp_path / "p.db", gold=gold_path)
    assert (result["status"], result["reason"]) == (
        "refused",
        "source_gold_title_mismatch",
    )
    assert result["details"] == [row_id]
