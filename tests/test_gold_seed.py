from pathlib import Path

from sku_sleuth.generate import generate
from sku_sleuth.models import load_jsonl

GOLD = Path("seeds/gold_set.jsonl")


def load_gold():
    return load_jsonl(GOLD)


def test_shape_and_uniqueness():
    gold = load_gold()
    assert len(gold) == 200
    ids = [g["row_id"] for g in gold]
    assert len(set(ids)) == 200
    for g in gold:
        assert g["expect"] in {"decode", "abstain"}
        if g["expect"] == "decode":
            assert {"category", "subcategory", "attributes", "is_winter_rated"} <= set(g)


def test_gold_rows_exist_in_seed42_batch():
    gold = load_gold()
    batch = {x.product.row_id: x for x in generate(seed=42, rows=1200)}
    for g in gold:
        assert g["row_id"] in batch, f"{g['row_id']} not in seed-42 batch"
        assert g["title"] == batch[g["row_id"]].product.title


def test_composition_invariants():
    gold = load_gold()
    winters = [g for g in gold if g.get("is_winter_rated") is True]
    assert 45 <= len(winters) <= 55
    abstains = [g for g in gold if g["expect"] == "abstain"]
    assert len(abstains) >= 15
    wnty = [g for g in gold if "WNTY" in g["title"].upper()]
    assert len(wnty) >= 6
    apparel = [g for g in gold if g.get("category") == "Apparel"]
    assert len(apparel) >= 25


def test_trap_brand_nonwinter_coverage():
    gold = load_gold()
    batch = {x.product.row_id: x for x in generate(seed=42, rows=1200)}
    trap_nonwinter = [
        g
        for g in gold
        if g["expect"] == "decode"
        and g.get("is_winter_rated") is False
        and batch[g["row_id"]].product.sku.startswith("NVK-")
    ]
    assert len(trap_nonwinter) >= 8


def test_gold_set_doc_exists_with_adjudication_notes():
    doc = Path("seeds/GOLD_SET.md").read_text(encoding="utf-8")
    assert "Inclusion criteria" in doc
    assert "Adjudication notes" in doc
