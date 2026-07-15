from pathlib import Path

import pytest

from sku_sleuth.engine import decode_stage
from sku_sleuth.evaluate import (
    EvaluationValidationError,
    evaluate_batch,
    render_markdown,
    write_eval_report,
)
from sku_sleuth.generate import generate, write_raw_csv
from sku_sleuth.models import load_jsonl, sha256_json, sha256_jsonl_rows
from sku_sleuth.tiers.model import StubModel

GOLD = Path("seeds/gold_set.jsonl")
FIXTURE = Path("seeds/stub_model_fixture.json")


def run_stage(tmp_path, ruleset="guarded"):
    raw = tmp_path / "raw.csv"
    write_raw_csv(generate(seed=42, rows=1200), raw)
    out = tmp_path / ("out_" + ruleset)
    out.mkdir()
    manifest = decode_stage(raw, ruleset, StubModel.from_json(FIXTURE), out)
    return (
        load_jsonl(out / "decoded.jsonl"),
        load_jsonl(out / "rejects.jsonl"),
        load_jsonl(GOLD),
        manifest,
    )


def single_row_inputs():
    decoded = [
        {
            "row_id": "R1",
            "sku": "S1",
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": {},
            "is_winter_rated": False,
            "tier": "rules",
            "evidence": "e",
        }
    ]
    gold = [
        {
            "row_id": "R1",
            "title": "T",
            "expect": "decode",
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": {},
            "is_winter_rated": False,
        }
    ]
    manifest = {
        "batch_id": "b",
        "decoded_sha256": sha256_jsonl_rows(decoded),
        "rejects_sha256": sha256_jsonl_rows([]),
        "tier_invalid": {},
        "schema": {"passed": True},
        "counts": {"total": 1, "decoded": 1, "abstained": 0, "quarantined": 0, "errored": 0},
    }
    return decoded, gold, manifest


@pytest.mark.parametrize("bad_value", ["false", 1])
def test_gold_winter_flag_requires_exact_json_boolean(bad_value):
    decoded, gold, manifest = single_row_inputs()
    gold[0]["is_winter_rated"] = bad_value

    with pytest.raises(EvaluationValidationError, match="must be a JSON boolean") as exc_info:
        evaluate_batch(decoded, [], gold, manifest)

    assert exc_info.value.details == {
        "artifact": "gold",
        "row_id": "R1",
        "field": "is_winter_rated",
        "expected": "JSON boolean",
        "actual_type": type(bad_value).__name__,
    }


@pytest.mark.parametrize("bad_value", ["false", 1])
def test_decoded_winter_flag_requires_exact_json_boolean(bad_value):
    decoded, gold, manifest = single_row_inputs()
    decoded[0]["is_winter_rated"] = bad_value
    manifest["decoded_sha256"] = sha256_jsonl_rows(decoded)

    with pytest.raises(EvaluationValidationError, match="must be a JSON boolean") as exc_info:
        evaluate_batch(decoded, [], gold, manifest)

    assert exc_info.value.details["artifact"] == "decoded"
    assert exc_info.value.details["actual_type"] == type(bad_value).__name__


def test_report_shape_and_ranges(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    r = evaluate_batch(decoded, rejects, gold, manifest)
    assert r["batch_id"] == manifest["batch_id"]
    assert 0.80 <= r["coverage"] <= 0.98
    assert r["flag"]["precision"] >= 0.97  # guarded ruleset must clear the gate
    assert r["flag"]["fp"] == 1  # exactly the one designed FP (see seeds/GOLD_SET.md)
    assert r["category_accuracy"] >= 0.93
    assert r["gold"]["total"] == 200
    assert set(r["attributes"]) == {"material", "pack_count", "position", "size"}
    assert r["tiers"]["rules"]["rows"] > r["tiers"]["catalog"]["rows"]
    assert r["tiers"]["model"]["rows"] >= 10  # stub fixture contributes


def test_honest_imperfection(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    r = evaluate_batch(decoded, rejects, gold, manifest)
    imperfect = (
        r["flag"]["precision"] < 1.0
        or r["category_accuracy"] < 1.0
        or any(a["accuracy"] < 1.0 for a in r["attributes"].values())
        or len(r["confusion_pairs"]) > 0
    )
    assert imperfect, "metrics are suspiciously perfect — the seeded challenges are not biting"


def test_naive_ruleset_tanks_flag_precision(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path, ruleset="naive")
    r = evaluate_batch(decoded, rejects, gold, manifest)
    assert r["flag"]["precision"] < 0.97
    assert r["flag"]["fp"] >= 8


def test_missing_gold_row_raises(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    orphan = dict(gold[0])
    orphan["row_id"] = "R9999"
    with pytest.raises(ValueError, match="R9999"):
        evaluate_batch(decoded, rejects, gold + [orphan], manifest)


def test_quarantined_gold_positive_is_recall_miss(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    # pick a positive that was actually predicted True, so removing it must lower recall
    target = next(
        g
        for g in gold
        if g["expect"] == "decode"
        and g["is_winter_rated"]
        and any(d["row_id"] == g["row_id"] and d["is_winter_rated"] for d in decoded)
    )
    decoded2 = [d for d in decoded if d["row_id"] != target["row_id"]]
    rejects2 = rejects + [
        {
            "row_id": target["row_id"],
            "sku": "X",
            "title": target["title"],
            "bucket": "quarantined",
            "reason": "empty_title",
        }
    ]
    base = evaluate_batch(decoded, rejects, gold, manifest)
    moved_manifest = dict(manifest)
    moved_manifest["counts"] = dict(
        manifest["counts"],
        decoded=len(decoded2),
        quarantined=manifest["counts"]["quarantined"] + 1,
    )
    moved_manifest["decoded_sha256"] = sha256_jsonl_rows(decoded2)
    moved_manifest["rejects_sha256"] = sha256_jsonl_rows(rejects2)
    moved_manifest["run_id"] = sha256_json(
        {
            "batch_id": manifest["batch_id"],
            "decoded_sha256": moved_manifest["decoded_sha256"],
            "rejects_sha256": moved_manifest["rejects_sha256"],
        }
    )
    moved = evaluate_batch(decoded2, rejects2, gold, moved_manifest)
    assert moved["flag"]["fn"] >= base["flag"]["fn"]
    assert moved["flag"]["recall"] < base["flag"]["recall"] or base["flag"]["recall"] == 0


def test_determinism_byte_identical(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    r = evaluate_batch(decoded, rejects, gold, manifest)
    p1, p2 = tmp_path / "r1.json", tmp_path / "r2.json"
    write_eval_report(r, p1)
    write_eval_report(evaluate_batch(decoded, rejects, gold, manifest), p2)
    assert p1.read_bytes() == p2.read_bytes()
    assert b"\r" not in p1.read_bytes()


def test_markdown_render_mentions_flag_first(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    md = render_markdown(evaluate_batch(decoded, rejects, gold, manifest))
    assert md.index("Winter flag") < md.index("Categories")


def test_attribute_null_handling_synthetic():
    # both-absent = correct; wrongly-present and wrongly-absent both count against
    def dec(rid, attrs):
        return {
            "row_id": rid,
            "sku": "S",
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": attrs,
            "is_winter_rated": False,
            "tier": "rules",
            "evidence": "e",
        }

    def gld(rid, attrs):
        return {
            "row_id": rid,
            "title": "T",
            "expect": "decode",
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": attrs,
            "is_winter_rated": False,
        }

    decoded = [dec("R1", {}), dec("R2", {"position": "front"}), dec("R3", {})]
    gold = [
        gld("R1", {}),  # both absent -> correct
        gld("R2", {}),  # wrongly present -> wrong
        gld("R3", {"position": "rear"}),
    ]  # wrongly absent -> wrong
    manifest = {
        "batch_id": "b",
        "decoded_sha256": sha256_jsonl_rows(decoded),
        "rejects_sha256": sha256_jsonl_rows([]),
        "tier_invalid": {},
        "schema": {"passed": True},
        "counts": {"total": 3, "decoded": 3, "abstained": 0, "quarantined": 0, "errored": 0},
    }
    r = evaluate_batch(decoded, [], gold, manifest)
    assert r["attributes"]["position"] == {"correct": 1, "total": 3, "accuracy": 0.3333}


def test_expected_decode_abstention_is_end_to_end_category_and_attribute_miss():
    decoded = [
        {
            "row_id": "R1",
            "sku": "S1",
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": {"material": "ceramic"},
            "is_winter_rated": False,
            "tier": "rules",
            "evidence": "e",
        }
    ]
    rejects = [
        {
            "row_id": "R2",
            "sku": "S2",
            "title": "A VALID TITLE",
            "bucket": "abstained",
            "reason": "no_confident_decode",
        }
    ]
    gold = [
        {
            "row_id": row_id,
            "title": "A VALID TITLE",
            "expect": "decode",
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": {"material": "ceramic"},
            "is_winter_rated": False,
        }
        for row_id in ("R1", "R2")
    ]
    manifest = {
        "batch_id": "b",
        "decoded_sha256": sha256_jsonl_rows(decoded),
        "rejects_sha256": sha256_jsonl_rows(rejects),
        "tier_invalid": {},
        "schema": {"passed": True},
        "counts": {"total": 2, "decoded": 1, "abstained": 1, "quarantined": 0, "errored": 0},
    }
    report = evaluate_batch(decoded, rejects, gold, manifest)
    assert report["coverage"] == 0.5
    assert report["category_accuracy"] == 0.5
    assert report["conditional_category_accuracy"] == 1.0
    assert report["categories"]["Braking"]["support"] == 2
    assert report["categories"]["Braking"]["recall"] == 0.5
    assert report["attributes"]["material"] == {"correct": 1, "total": 2, "accuracy": 0.5}


def test_duplicate_overlap_and_mixed_hashes_fail_loudly(tmp_path):
    decoded, rejects, gold, manifest = run_stage(tmp_path)
    with pytest.raises(ValueError, match="duplicate row_id"):
        evaluate_batch(decoded + [decoded[0]], rejects, gold, manifest)
    with pytest.raises(ValueError, match="decoded and rejects"):
        evaluate_batch(decoded, rejects + [decoded[0]], gold, manifest)
    mixed = list(decoded)
    mixed[0] = dict(mixed[0], evidence="changed after manifest")
    with pytest.raises(ValueError, match="decoded artifact hash"):
        evaluate_batch(mixed, rejects, gold, manifest)
