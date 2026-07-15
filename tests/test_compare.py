from sku_sleuth.compare import compare_batches
from sku_sleuth.models import sha256_jsonl_rows


def decoded(row_id, *, winter=False, attributes=None):
    return {
        "row_id": row_id,
        "sku": f"S-{row_id}",
        "category": "Braking",
        "subcategory": "Brake Pads",
        "attributes": {} if attributes is None else attributes,
        "is_winter_rated": winter,
        "tier": "rules",
        "evidence": "rules:test",
    }


def rejected(row_id, bucket="abstained"):
    return {
        "row_id": row_id,
        "sku": f"S-{row_id}",
        "title": "A VALID TITLE",
        "bucket": bucket,
        "reason": "test",
    }


def test_comparison_partitions_union_and_tracks_outcome_transitions():
    baseline_decoded = [decoded("R1"), decoded("R2"), decoded("R5", winter=True)]
    baseline_rejects = [rejected("R3")]
    current_decoded = [
        decoded("R1"),
        decoded("R4"),
        decoded("R5", winter=1, attributes={"material": None}),
    ]
    current_rejects = [rejected("R2", "quarantined")]

    report = compare_batches(
        baseline_decoded,
        current_decoded,
        baseline_rejects=baseline_rejects,
        current_rejects=current_rejects,
        baseline_batch_id="before",
        current_batch_id="after",
    )
    assert report["counts"] == {"added": 1, "removed": 1, "changed": 2, "unchanged": 1}
    assert report["partition"] == {
        "baseline_total": 4,
        "current_total": 4,
        "baseline_identity": 4,
        "current_identity": 4,
        "union_total": 5,
        "union_identity": 5,
    }
    assert report["transitions"]["decoded_to_quarantined"] == 1
    r5 = next(row for row in report["changed_rows"] if row["row_id"] == "R5")
    assert r5["changes"]["is_winter_rated"] == {"before": True, "after": 1}
    assert r5["changes"]["attributes.material"] == {
        "before": {"presence": "missing"},
        "after": None,
    }


def test_comparison_refuses_rows_that_do_not_match_supplied_manifest():
    rows = [decoded("R1")]
    manifest = {
        "batch_id": "current",
        "decoded_sha256": "wrong",
        "rejects_sha256": sha256_jsonl_rows([]),
    }
    try:
        compare_batches(
            [],
            rows,
            current_batch_id="current",
            current_manifest=manifest,
        )
    except ValueError as exc:
        assert "does not match its manifest" in str(exc)
    else:
        raise AssertionError("comparison accepted mixed artifact/manifest evidence")


def test_duplicate_or_overlapping_outcomes_fail_loudly():
    row = decoded("R1")
    try:
        compare_batches([row, row], [])
    except ValueError as exc:
        assert "duplicate row_id" in str(exc)
    else:
        raise AssertionError("duplicate row IDs were silently collapsed")

    try:
        compare_batches([row], [], baseline_rejects=[rejected("R1")])
    except ValueError as exc:
        assert "decoded and rejected" in str(exc)
    else:
        raise AssertionError("decoded/reject overlap was silently collapsed")
