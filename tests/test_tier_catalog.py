from pathlib import Path

from sku_sleuth.generate import generate
from sku_sleuth.models import Product
from sku_sleuth.tiers.catalog import CatalogTier, normalize_sku
from sku_sleuth.validate import validate_decode

SEED_CATALOG = Path("seeds/catalog.csv")


def test_normalize_sku():
    assert normalize_sku(" zv-0001 ") == "ZV-0001"
    assert normalize_sku("ZV - 0001") == "ZV-0001"


def test_catalog_hit_returns_valid_decode():
    tier = CatalogTier.from_csv(SEED_CATALOG)
    assert tier.name == "catalog"
    sku = next(iter(sorted(tier.entries)))
    d = tier.decode(Product(row_id="RX", sku=sku.lower(), title="anything"))
    assert d is not None
    assert d.tier == "catalog"
    assert d.evidence == f"catalog:{sku}"
    assert validate_decode(d) == []


def test_catalog_miss_abstains():
    tier = CatalogTier.from_csv(SEED_CATALOG)
    assert tier.decode(Product("RX", "ZZ-0000", "anything")) is None


def test_seed_has_sixty_rows_matching_generated_skus():
    tier = CatalogTier.from_csv(SEED_CATALOG)
    assert len(tier.entries) == 60
    generated = {normalize_sku(g.product.sku) for g in generate(seed=42, rows=1200)}
    assert set(tier.entries) <= generated


def test_all_catalog_decodes_valid():
    tier = CatalogTier.from_csv(SEED_CATALOG)
    for sku in sorted(tier.entries):
        d = tier.decode(Product("RX", sku, ""))
        assert validate_decode(d) == [], f"invalid catalog entry for {sku}"


def test_catalog_skus_are_unique_in_batch():
    # duplicate SKUs exist in the batch by construction (~8 collisions per 1,200 rows);
    # a catalog entry keyed on a duplicated SKU would authoritatively mis-decode the twin
    from collections import Counter

    counts = Counter(normalize_sku(g.product.sku) for g in generate(seed=42, rows=1200))
    tier = CatalogTier.from_csv(SEED_CATALOG)
    for sku in sorted(tier.entries):
        assert counts[sku] == 1, f"catalog SKU {sku} appears {counts[sku]}x in the seed-42 batch"


def test_catalog_excludes_trap_brand():
    # the winter-bias challenge brand must never be in the authoritative catalog:
    # catalog hits would shield the naive ruleset's brand-default from measurement
    tier = CatalogTier.from_csv(SEED_CATALOG)
    assert not any(sku.startswith("NVK-") for sku in tier.entries)
