from pathlib import Path

from sku_sleuth.engine import (
    DECODER_VERSION,
    decode_stage,
    is_malformed,
    mint_batch_id,
    run_batch,
)
from sku_sleuth.generate import generate, write_raw_csv
from sku_sleuth.models import Decode, Product, load_jsonl, sha256_file
from sku_sleuth.tiers.model import StubModel

FIXTURE = Path("seeds/stub_model_fixture.json")


class FakeTier:
    def __init__(self, name, result=None, error=False):
        self.name = name
        self.result = result
        self.error = error

    def decode(self, product):
        if self.error:
            raise RuntimeError("boom")
        return self.result


def make_decode(row_id="R0001", **over):
    base = dict(
        row_id=row_id,
        sku="ZV-1",
        category="Braking",
        subcategory="Brake Pads",
        attributes={},
        is_winter_rated=False,
        tier="fake",
        evidence="e",
    )
    base.update(over)
    return Decode(**base)


def test_is_malformed():
    assert is_malformed(Product("R", "S", "")) == "empty_title"
    assert is_malformed(Product("R", "S", "###")) == "short_title"
    assert is_malformed(Product("R", "", "A REAL TITLE")) == "missing_sku"
    assert is_malformed(Product("R", "S", "A REAL TITLE")) is None


def test_precedence_first_tier_wins():
    d1, d2 = make_decode(tier="one"), make_decode(tier="two")
    out = run_batch(
        [Product("R0001", "ZV-1", "A REAL TITLE")], [FakeTier("one", d1), FakeTier("two", d2)]
    )
    assert out.decodes == [d1]


def test_error_bucket_and_batch_survival():
    out = run_batch(
        [Product("R0001", "ZV-1", "A REAL TITLE")],
        [FakeTier("bad", error=True), FakeTier("two", make_decode())],
    )
    assert out.counts["errored"] == 1 and out.counts["decoded"] == 0
    assert out.rejects[0]["bucket"] == "errored"
    assert out.rejects[0]["reason"] == "bad:RuntimeError"


def test_invalid_decode_counts_and_falls_through():
    bad = make_decode(category="Exhaust", subcategory="Mufflers", tier="one")
    good = make_decode(tier="two")
    out = run_batch(
        [Product("R0001", "ZV-1", "A REAL TITLE")], [FakeTier("one", bad), FakeTier("two", good)]
    )
    assert out.decodes == [good]
    assert out.tier_invalid == {"one": 1}


def test_all_abstain_goes_to_abstained():
    out = run_batch([Product("R0001", "ZV-1", "A REAL TITLE")], [FakeTier("one")])
    assert out.counts["abstained"] == 1
    assert out.rejects[0]["reason"] == "no_confident_decode"


def test_mint_batch_id_sensitive_to_inputs():
    identity = {"inputs": {"raw": "x"}, "configuration": {"ruleset": "guarded"}}
    a = mint_batch_id(identity)
    assert a != mint_batch_id(identity | {"inputs": {"raw": "y"}})
    assert a != mint_batch_id(identity | {"configuration": {"ruleset": "naive"}})
    assert len(a) == 64 and DECODER_VERSION == "2"


def test_decode_stage_artifacts(tmp_path):
    raw = tmp_path / "raw.csv"
    write_raw_csv(generate(seed=42, rows=200), raw)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    manifest = decode_stage(raw, "guarded", StubModel.from_json(FIXTURE), out_dir)
    decoded = load_jsonl(out_dir / "decoded.jsonl")
    rejects = load_jsonl(out_dir / "rejects.jsonl")
    assert manifest["total"] == 200
    counts = manifest["counts"]
    assert counts["decoded"] == len(decoded)
    bucket_sum = counts["decoded"] + counts["abstained"] + counts["quarantined"] + counts["errored"]
    assert bucket_sum == 200
    assert [d["row_id"] for d in decoded] == sorted(d["row_id"] for d in decoded)
    assert manifest["decoded_sha256"] == sha256_file(out_dir / "decoded.jsonl")
    assert "source" not in manifest
    assert all(r["bucket"] in {"abstained", "quarantined", "errored"} for r in rejects)


def test_decode_stage_deterministic(tmp_path):
    raw = tmp_path / "raw.csv"
    write_raw_csv(generate(seed=42, rows=200), raw)
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    m1 = decode_stage(raw, "guarded", StubModel.from_json(FIXTURE), d1)
    m2 = decode_stage(raw, "guarded", StubModel.from_json(FIXTURE), d2)
    assert m1 == m2
    assert (d1 / "decoded.jsonl").read_bytes() == (d2 / "decoded.jsonl").read_bytes()
