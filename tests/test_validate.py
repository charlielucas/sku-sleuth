from sku_sleuth.models import Decode
from sku_sleuth.validate import validate_decode


def make(**over):
    base = dict(
        row_id="R0001",
        sku="ZV-0001",
        category="Braking",
        subcategory="Brake Pads",
        attributes={"position": "front", "pack_count": 4, "material": "ceramic"},
        is_winter_rated=False,
        tier="rules",
        evidence="e",
    )
    base.update(over)
    return Decode(**base)


def test_valid_decode_passes():
    assert validate_decode(make()) == []


def test_unknown_category_fails():
    errs = validate_decode(make(category="Exhaust", subcategory="Mufflers"))
    assert any("category" in e for e in errs)


def test_subcategory_must_match_category():
    assert validate_decode(make(subcategory="Gloves")) != []


def test_size_only_for_apparel():
    errs = validate_decode(make(attributes={"size": "L"}))
    assert any("size" in e for e in errs)
    ok = validate_decode(
        make(
            category="Apparel",
            subcategory="Gloves",
            attributes={"size": "L", "material": "leather"},
        )
    )
    assert ok == []


def test_position_not_for_apparel():
    errs = validate_decode(
        make(category="Apparel", subcategory="Gloves", attributes={"position": "front"})
    )
    assert any("position" in e for e in errs)


def test_bad_domains_fail():
    assert validate_decode(make(attributes={"position": "middle"})) != []
    assert validate_decode(make(attributes={"pack_count": 0})) != []
    assert validate_decode(make(attributes={"pack_count": "4"})) != []
    assert validate_decode(make(attributes={"material": "titanium"})) != []
    assert validate_decode(make(attributes={"color": "red"})) != []


def test_flag_must_be_bool():
    assert validate_decode(make(is_winter_rated="yes")) != []
