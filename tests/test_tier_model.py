import json as _json
import types
from pathlib import Path

from sku_sleuth.generate import generate
from sku_sleuth.models import Product
from sku_sleuth.tiers.model import (
    AnthropicModel,
    StubModel,
    build_prompt,
    extract_text,
    parse_response,
)
from sku_sleuth.tiers.rules import normalize_title
from sku_sleuth.validate import validate_decode

FIXTURE = Path("seeds/stub_model_fixture.json")


def test_fixture_hit_decodes():
    stub = StubModel.from_json(FIXTURE)
    assert stub.name == "model"
    title = sorted(stub.fixture)[0]
    d = stub.decode(Product("RX", "XX-1", title.lower()))
    assert d is not None and d.tier == "model" and d.evidence == "model:stub-fixture"


def test_fixture_miss_abstains():
    stub = StubModel.from_json(FIXTURE)
    assert stub.decode(Product("RX", "XX-1", "COMPLETELY UNKNOWN THING")) is None


def test_fixture_targets_seed42_out_of_catalog_rows():
    stub = StubModel.from_json(FIXTURE)
    ooc = {
        normalize_title(g.product.title): g
        for g in generate(seed=42, rows=1200)
        if g.quality == "out_of_catalog"
    }
    assert set(stub.fixture) <= set(ooc)
    assert len(stub.fixture) == 20


def test_fixture_has_designed_imperfections():
    stub = StubModel.from_json(FIXTURE)
    ooc = {
        normalize_title(g.product.title): g
        for g in generate(seed=42, rows=1200)
        if g.quality == "out_of_catalog"
    }
    invalid = wrong = correct = 0
    for title in sorted(stub.fixture):
        d = stub.decode(Product("RX", "XX-1", title))
        truth = ooc[title].truth
        if validate_decode(d):
            invalid += 1
        elif (d.category, d.subcategory) != (truth["category"], truth["subcategory"]):
            wrong += 1
        else:
            correct += 1
    assert (correct, wrong, invalid) == (15, 3, 2)


def test_stub_is_deterministic():
    stub = StubModel.from_json(FIXTURE)
    title = sorted(stub.fixture)[0]
    assert stub.decode(Product("RX", "X", title)) == stub.decode(Product("RX", "X", title))


def test_fixture_has_flag_flips():
    stub = StubModel.from_json(FIXTURE)
    ooc = {
        normalize_title(g.product.title): g
        for g in generate(seed=42, rows=1200)
        if g.quality == "out_of_catalog"
    }
    flips = [
        (bool(stub.fixture[t]["is_winter_rated"]), ooc[t].truth["is_winter_rated"])
        for t in sorted(stub.fixture)
        if bool(stub.fixture[t]["is_winter_rated"]) != ooc[t].truth["is_winter_rated"]
    ]
    assert len(flips) == 2
    assert (True, False) in flips and (False, True) in flips  # one FP-maker, one FN-maker


PROD = Product("R0001", "ZV-1", "AXKORT FRT BRAKE PADS")


def test_build_prompt_contains_title_and_taxonomy_and_abstain():
    p = build_prompt(PROD)
    assert "AXKORT FRT BRAKE PADS" in p
    assert "Brake Pads" in p and "Hi-Vis Jackets" in p
    assert "abstain" in p.lower()


def test_parse_response_valid():
    text = _json.dumps(
        {
            "category": "Braking",
            "subcategory": "Brake Pads",
            "attributes": {"position": "front"},
            "is_winter_rated": False,
        }
    )
    d = parse_response(text, PROD)
    assert d is not None and d.tier == "model" and d.evidence == "model:anthropic"
    assert d.category == "Braking" and d.attributes == {"position": "front"}


def test_parse_response_abstain_and_garbage():
    assert parse_response('{"abstain": true}', PROD) is None
    assert parse_response("not json at all", PROD) is None
    assert parse_response('{"category": "Braking"}', PROD) is None
    bad_type = '{"category": 3, "subcategory": "x", "attributes": {}, "is_winter_rated": false}'
    assert parse_response(bad_type, PROD) is None


def test_anthropic_model_abstains_on_error(monkeypatch):
    model = AnthropicModel()

    def boom(product):
        raise RuntimeError("no api key, no network")

    monkeypatch.setattr(model, "_call_api", boom)
    assert model.decode(PROD) is None


def test_extract_text_skips_leading_thinking_block():
    # claude-sonnet-5 with adaptive thinking on: content[0] is a thinking block
    # with no .text — extract_text must find the text block that follows it.
    content = [
        types.SimpleNamespace(type="thinking", thinking="reasoning..."),
        types.SimpleNamespace(type="text", text="hello"),
    ]
    assert extract_text(content) == "hello"


def test_extract_text_empty_or_no_text_returns_empty_string():
    assert extract_text([]) == ""
    assert extract_text([types.SimpleNamespace(type="thinking", thinking="only reasoning")]) == ""
    # and parse_response("", ...) abstains rather than raising
    assert parse_response("", PROD) is None
