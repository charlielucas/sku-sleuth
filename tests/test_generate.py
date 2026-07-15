from pathlib import Path

from sku_sleuth.generate import GeneratedProduct, generate, read_raw_csv, write_raw_csv

SEED_BRANDS = Path("seeds/brands.json")


def test_deterministic_same_seed():
    a = generate(seed=42, rows=50, brands_path=SEED_BRANDS)
    b = generate(seed=42, rows=50, brands_path=SEED_BRANDS)
    assert a == b


def test_different_seed_differs():
    a = generate(seed=42, rows=50, brands_path=SEED_BRANDS)
    b = generate(seed=43, rows=50, brands_path=SEED_BRANDS)
    assert [g.product.title for g in a] != [g.product.title for g in b]


def test_prefix_property():
    small = generate(seed=42, rows=100, brands_path=SEED_BRANDS)
    big = generate(seed=42, rows=140, brands_path=SEED_BRANDS)
    assert big[:100] == small


def test_row_ids_stable_and_unique():
    items = generate(seed=42, rows=25, brands_path=SEED_BRANDS)
    assert [g.product.row_id for g in items] == [f"R{i:04d}" for i in range(1, 26)]


def test_truth_is_well_formed():
    for g in generate(seed=42, rows=200, brands_path=SEED_BRANDS):
        assert isinstance(g, GeneratedProduct)
        assert g.quality in {"clean", "out_of_catalog", "malformed"}
        assert set(g.truth) == {"category", "subcategory", "attributes", "is_winter_rated"}


def test_csv_round_trip_and_lf(tmp_path):
    items = generate(seed=42, rows=30, brands_path=SEED_BRANDS)
    p = tmp_path / "raw.csv"
    write_raw_csv(items, p)
    assert b"\r" not in p.read_bytes()
    products = read_raw_csv(p)
    assert products == [g.product for g in items]


def test_csv_double_write_byte_identical(tmp_path):
    items = generate(seed=42, rows=30, brands_path=SEED_BRANDS)
    p1, p2 = tmp_path / "a.csv", tmp_path / "b.csv"
    write_raw_csv(items, p1)
    write_raw_csv(items, p2)
    assert p1.read_bytes() == p2.read_bytes()


def test_challenge_mix_seed42():
    items = generate(seed=42, rows=1200, brands_path=SEED_BRANDS)
    quality = [g.quality for g in items]
    assert 15 <= quality.count("malformed") <= 60
    assert 35 <= quality.count("out_of_catalog") <= 90
    titles = [g.product.title.upper() for g in items]
    assert sum("WNTY" in t for t in titles) >= 40  # false friend present
    assert any(t.endswith(" L") for t in titles)  # stray-L collision present


def test_trap_brand_has_tokenless_winter_rows():
    items = generate(seed=42, rows=1200, brands_path=SEED_BRANDS)
    trap_winter = [
        g
        for g in items
        if g.quality == "clean"
        and g.truth["is_winter_rated"]
        and ("NORVIKKA" in g.product.title.upper() or "NVK-" in g.product.sku)
    ]
    tokens = ("WINTER", "WNTR", "INSULATED", "THERMAL")
    tokenless = [
        g for g in trap_winter if not any(tok in g.product.title.upper() for tok in tokens)
    ]
    assert len(trap_winter) >= 20
    assert len(tokenless) >= 5


def test_trap_brand_has_nonwinter_rows():
    items = generate(seed=42, rows=1200, brands_path=SEED_BRANDS)
    trap_nonwinter = [
        g
        for g in items
        if g.quality == "clean" and not g.truth["is_winter_rated"] and "NVK-" in g.product.sku
    ]
    assert len(trap_nonwinter) >= 10


def test_malformed_rows_have_junk_titles():
    items = generate(seed=42, rows=1200, brands_path=SEED_BRANDS)
    for g in items:
        if g.quality == "malformed":
            assert len(g.product.title.strip()) < 4
