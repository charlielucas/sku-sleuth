"""Regenerate the golden-regression snapshot for gold-row pipeline outcomes."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sku_sleuth.engine import decode_stage
from sku_sleuth.generate import generate, write_raw_csv
from sku_sleuth.models import dump_json, load_jsonl
from sku_sleuth.tiers.model import StubModel

GOLDEN_PATH = Path("tests/golden/gold_outcomes.json")


def compute_gold_outcomes() -> dict:
    gold_ids = {g["row_id"] for g in load_jsonl(Path("seeds/gold_set.jsonl"))}
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        raw = work / "raw.csv"
        write_raw_csv(generate(seed=42, rows=1200), raw)
        decode_stage(
            raw, "guarded", StubModel.from_json(Path("seeds/stub_model_fixture.json")), work
        )
        decoded = {d["row_id"]: d for d in load_jsonl(work / "decoded.jsonl")}
        rejects = {r["row_id"]: r for r in load_jsonl(work / "rejects.jsonl")}
    outcomes = {}
    for rid in sorted(gold_ids):
        if rid in decoded:
            outcomes[rid] = decoded[rid]
        else:
            outcomes[rid] = {"bucket": rejects[rid]["bucket"]}
    return outcomes


if __name__ == "__main__":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    dump_json(compute_gold_outcomes(), GOLDEN_PATH)
    print(f"wrote {GOLDEN_PATH}")
