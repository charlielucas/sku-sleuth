from pathlib import Path

from sku_sleuth.models import load_brands

SEED = Path("seeds/brands.json")


def test_twelve_vetted_brands():
    brands = load_brands(SEED)
    assert len(brands) == 12
    assert len({b.name for b in brands}) == 12
    for b in brands:
        assert b.kind in {"parts", "apparel", "both"}
        assert b.name == b.name.upper()
        assert b.aliases[0] == b.name
        assert all(a == a.upper() for a in b.aliases)
        assert len(b.aliases) == 2
        assert len(b.aliases[1]) == 3


def test_exactly_one_trap_brand():
    brands = load_brands(SEED)
    traps = [b for b in brands if b.winter_bias]
    assert [t.name for t in traps] == ["NORVIKKA"]
    assert traps[0].kind == "both"


def test_aliases_globally_unique():
    brands = load_brands(SEED)
    aliases = [a for b in brands for a in b.aliases]
    assert len(aliases) == len(set(aliases))
