from pathlib import Path

import pytest

from sku_sleuth.models import Product, load_brands
from sku_sleuth.tiers.rules import RulesTier, normalize_title
from sku_sleuth.validate import validate_decode

BRANDS = load_brands(Path("seeds/brands.json"))


@pytest.fixture()
def tier():
    return RulesTier(BRANDS)


def P(title, sku="VRN-1234", row_id="RX"):
    return Product(row_id=row_id, sku=sku, title=title)


def test_normalize_title():
    assert normalize_title("vornelle Frt  (4-Pk) pads") == "VORNELLE FRT 4-PK PADS"


def test_basic_decode(tier):
    d = tier.decode(P("VORNELLE FRT CERAMIC BRK PADS (4-PK)"))
    assert d is not None and validate_decode(d) == []
    assert (d.category, d.subcategory) == ("Braking", "Brake Pads")
    assert d.attributes == {"position": "front", "pack_count": 4, "material": "ceramic"}
    assert d.tier == "rules"
    assert "brand=VORNELLE" in d.evidence and "kw=" in d.evidence


def test_case_insensitive(tier):
    d = tier.decode(P("krelvonne oil fltr"))
    assert d is not None and d.subcategory == "Oil Filters"


def test_unknown_brand_abstains(tier):
    assert tier.decode(P("AXKORT FRT BRK PADS")) is None


def test_no_keyword_abstains(tier):
    assert tier.decode(P("VORNELLE MYSTERY ITEM")) is None


def test_ambiguous_keywords_abstain(tier):
    assert tier.decode(P("VORNELLE WIPER BLADES AND HEADLIGHT BULB KIT")) is None


def test_pack_set_of(tier):
    d = tier.decode(P("QUORVYN SPARK PLUGS SET OF 6"))
    assert d.attributes.get("pack_count") == 6


def test_apparel_size(tier):
    d = tier.decode(P("BRUMHALT LEATHER GLOVES SZ XL", sku="BRH-2000"))
    assert (d.category, d.subcategory) == ("Apparel", "Gloves")
    assert d.attributes.get("size") == "XL"
    assert d.attributes.get("material") == "leather"


def test_stray_l_collision_is_a_known_failure(tier):
    d = tier.decode(P("WRENFOLD COVERALLS L", sku="WRF-3000"))
    assert d.attributes.get("size") == "L"  # designed misread: L here is a crate code


def test_rr_position_unhandled(tier):
    d = tier.decode(P("VORNELLE RR BRAKE PADS"))
    assert "position" not in d.attributes  # documented known failure


def test_size_not_extracted_for_parts(tier):
    d = tier.decode(P("VORNELLE BRAKE PADS L"))
    assert "size" not in d.attributes


def test_guarded_flag_requires_evidence_token(tier):
    d = tier.decode(P("TELVOSSA WNTR GLOVES", sku="TLV-1000"))
    assert d.is_winter_rated is True and ";flag=WNTR" in d.evidence
    d2 = tier.decode(P("TELVOSSA GLOVES", sku="TLV-1000"))
    assert d2.is_winter_rated is False


def test_cold_weather_phrase_flags(tier):
    d = tier.decode(P("VORNELLE COLD-WEATHER WIPER BLADES"))
    assert d.is_winter_rated is True and ";flag=COLD-WEATHER" in d.evidence


def test_wnty_is_not_winter(tier):
    d = tier.decode(P("VORNELLE BRAKE PADS 2YR WNTY"))
    assert d.is_winter_rated is False


def test_insulated_only_for_apparel(tier):
    d = tier.decode(P("BRUMHALT INSULATED COVERALLS", sku="BRH-1"))
    assert d.is_winter_rated is True
    d2 = tier.decode(P("VORNELLE INSULATED BRAKE PADS"))
    assert d2.is_winter_rated is False


def test_guarded_does_not_brand_default(tier):
    d = tier.decode(P("NORVIKKA BRAKE PADS", sku="NVK-1"))
    assert d.is_winter_rated is False


def test_naive_brand_defaults_the_trap_brand():
    naive = RulesTier(BRANDS, ruleset="naive")
    d = naive.decode(P("NORVIKKA BRAKE PADS", sku="NVK-1"))
    assert d.is_winter_rated is True and ";flag=brand-default" in d.evidence
    other = naive.decode(P("VORNELLE BRAKE PADS"))
    assert other.is_winter_rated is False


def test_naive_still_reads_tokens():
    naive = RulesTier(BRANDS, ruleset="naive")
    d = naive.decode(P("TELVOSSA WNTR GLOVES", sku="TLV-1"))
    assert d.is_winter_rated is True and ";flag=WNTR" in d.evidence


def test_bad_ruleset_rejected():
    with pytest.raises(ValueError):
        RulesTier(BRANDS, ruleset="bogus")
