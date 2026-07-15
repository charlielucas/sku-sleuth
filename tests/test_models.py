import hashlib
import json

from sku_sleuth.models import (
    APPAREL,
    TAXONOMY,
    Brand,
    Decode,
    decode_from_dict,
    decode_to_dict,
    dump_json,
    dump_jsonl,
    load_brands,
    load_jsonl,
    sha256_file,
    sha256_jsonl_rows,
)


def test_taxonomy_shape():
    assert set(TAXONOMY) == {"Braking", "Filtration", "Electrical", "Visibility", "Apparel"}
    assert sum(len(v) for v in TAXONOMY.values()) == 13
    assert TAXONOMY[APPAREL] == ("Gloves", "Coveralls", "Hi-Vis Jackets")


def test_decode_round_trip():
    d = Decode(
        row_id="R0001",
        sku="VRN-0001",
        category="Braking",
        subcategory="Brake Pads",
        attributes={"position": "front", "pack_count": 4},
        is_winter_rated=False,
        tier="rules",
        evidence="rules:brand=VORNELLE;kw=BRK PADS",
    )
    assert decode_from_dict(decode_to_dict(d)) == d


def test_dump_json_is_lf_and_sorted(tmp_path):
    p = tmp_path / "x.json"
    dump_json({"b": 1, "a": 2}, p)
    raw = p.read_bytes()
    assert b"\r" not in raw
    assert raw.decode().index('"a"') < raw.decode().index('"b"')


def test_jsonl_round_trip(tmp_path):
    p = tmp_path / "x.jsonl"
    rows = [{"row_id": "R0002"}, {"row_id": "R0001"}]
    dump_jsonl(rows, p)
    assert load_jsonl(p) == rows
    assert b"\r" not in p.read_bytes()


def test_jsonl_row_hash_matches_dumped_unicode_bytes(tmp_path):
    p = tmp_path / "unicode.jsonl"
    rows = [{"title": "café", "sku": "CAF-0001"}]
    dump_jsonl(rows, p)

    assert sha256_jsonl_rows(rows) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_load_brands_sorted(tmp_path):
    p = tmp_path / "brands.json"
    p.write_text(
        json.dumps(
            [
                {"name": "VORNELLE", "aliases": ["VORNELLE", "VRN"], "kind": "parts"},
                {
                    "name": "NORVIKKA",
                    "aliases": ["NORVIKKA", "NVK"],
                    "kind": "both",
                    "winter_bias": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    brands = load_brands(p)
    assert [b.name for b in brands] == ["NORVIKKA", "VORNELLE"]
    assert brands[0].winter_bias is True and brands[1].winter_bias is False
    assert isinstance(brands[0], Brand)


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
