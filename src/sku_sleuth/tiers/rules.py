"""Tier 2: deterministic keyword/token extraction with brand gating."""

from __future__ import annotations

import re

from sku_sleuth.models import APPAREL, SIZES, Brand, Decode, Product

SUBCATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [
            ("BRAKE PADS", "Brake Pads"),
            ("BRK PADS", "Brake Pads"),
            ("BRAKE PAD", "Brake Pads"),
            ("ROTORS", "Rotors"),
            ("ROTOR", "Rotors"),
            ("CALIPERS", "Calipers"),
            ("CALIPER", "Calipers"),
            ("OIL FILTER", "Oil Filters"),
            ("OIL FLTR", "Oil Filters"),
            ("AIR FILTER", "Air Filters"),
            ("AIR FLTR", "Air Filters"),
            ("CABIN FILTER", "Cabin Filters"),
            ("CABIN FLTR", "Cabin Filters"),
            ("SPARK PLUGS", "Spark Plugs"),
            ("SPARK PLUG", "Spark Plugs"),
            ("SPK PLG", "Spark Plugs"),
            ("IGNITION COIL", "Ignition Coils"),
            ("IGN COIL", "Ignition Coils"),
            ("WIPER BLADES", "Wiper Blades"),
            ("WIPER BLADE", "Wiper Blades"),
            ("WIPER", "Wiper Blades"),
            ("HEADLIGHT BULBS", "Headlight Bulbs"),
            ("HEADLIGHT BULB", "Headlight Bulbs"),
            ("HDLGHT BULB", "Headlight Bulbs"),
            ("BULB", "Headlight Bulbs"),
            ("GLOVES", "Gloves"),
            ("GLOVE", "Gloves"),
            ("COVERALLS", "Coveralls"),
            ("COVERALL", "Coveralls"),
            ("HI-VIS JACKET", "Hi-Vis Jackets"),
            ("HI VIS JACKET", "Hi-Vis Jackets"),
            ("HIVIS JACKET", "Hi-Vis Jackets"),
            ("JACKET", "Hi-Vis Jackets"),
        ],
        key=lambda kv: -len(kv[0]),
    )
)

SUBCAT_CATEGORY = {
    "Brake Pads": "Braking",
    "Rotors": "Braking",
    "Calipers": "Braking",
    "Oil Filters": "Filtration",
    "Air Filters": "Filtration",
    "Cabin Filters": "Filtration",
    "Spark Plugs": "Electrical",
    "Ignition Coils": "Electrical",
    "Wiper Blades": "Visibility",
    "Headlight Bulbs": "Visibility",
    "Gloves": APPAREL,
    "Coveralls": APPAREL,
    "Hi-Vis Jackets": APPAREL,
}

POSITION_MAP = {
    "FRONT": "front",
    "FRT": "front",
    "REAR": "rear",
    "LEFT": "left",
    "LH": "left",
    "RIGHT": "right",
    "RH": "right",
}
MATERIAL_MAP = {
    "CERAMIC": "ceramic",
    "SEMI-METALLIC": "semi-metallic",
    "SEMI-MET": "semi-metallic",
    "LEATHER": "leather",
    "NITRILE": "nitrile",
    "RUBBER": "rubber",
    "COTTON": "cotton",
}

_PACK_RE = re.compile(r"\b(\d{1,2})\s*[- ]?(?:PK|PACK)\b")
_SET_RE = re.compile(r"\bSET OF (\d{1,2})\b")


def normalize_title(title: str) -> str:
    return " ".join(title.upper().replace("(", " ").replace(")", " ").split())


class RulesTier:
    name = "rules"
    WINTER_TOKENS_ANY = ("WINTER", "WNTR")
    WINTER_TOKENS_APPAREL = ("INSULATED", "THERMAL")

    def __init__(self, brands: tuple[Brand, ...], ruleset: str = "guarded"):
        if ruleset not in ("guarded", "naive"):
            raise ValueError(f"unknown ruleset: {ruleset!r}")
        self.ruleset = ruleset
        self._alias_to_brand = {a: b for b in brands for a in b.aliases}

    def _find_brand(self, tokens: list[str]) -> Brand | None:
        for tok in tokens:
            if tok in self._alias_to_brand:
                return self._alias_to_brand[tok]
        return None

    def _find_subcategory(self, norm: str) -> tuple[str, str] | None:
        hits: dict[str, str] = {}
        for phrase, subcat in SUBCATEGORY_KEYWORDS:
            if phrase in norm and subcat not in hits:
                hits[subcat] = phrase
        if len(hits) != 1:
            return None
        subcat = next(iter(hits))
        return subcat, hits[subcat]

    def _extract_attributes(self, norm: str, tokens: list[str], category: str) -> dict:
        attrs: dict = {}
        for tok in tokens:
            if tok in POSITION_MAP and category != APPAREL:
                attrs["position"] = POSITION_MAP[tok]
                break
        m = _PACK_RE.search(norm) or _SET_RE.search(norm)
        if m:
            attrs["pack_count"] = int(m.group(1))
        if category == APPAREL:
            for tok in reversed(tokens):
                if tok in SIZES:
                    attrs["size"] = tok
                    break
        for tok in tokens:
            if tok in MATERIAL_MAP:
                attrs["material"] = MATERIAL_MAP[tok]
                break
        return attrs

    def _winter_flag(
        self, norm: str, tokens: list[str], brand: Brand, category: str
    ) -> tuple[bool, str]:
        pool = self.WINTER_TOKENS_ANY + (self.WINTER_TOKENS_APPAREL if category == APPAREL else ())
        for tok in tokens:
            if tok in pool:
                return True, f";flag={tok}"
        if "COLD-WEATHER" in norm or "COLD WEATHER" in norm:
            return True, ";flag=COLD-WEATHER"
        if self.ruleset == "naive" and brand.winter_bias:
            return True, ";flag=brand-default"
        return False, ""

    def decode(self, product: Product) -> Decode | None:
        norm = normalize_title(product.title)
        tokens = norm.split()
        brand = self._find_brand(tokens)
        if brand is None:
            return None
        found = self._find_subcategory(norm)
        if found is None:
            return None
        subcat, phrase = found
        category = SUBCAT_CATEGORY[subcat]
        attrs = self._extract_attributes(norm, tokens, category)
        winter, flag_evidence = self._winter_flag(norm, tokens, brand, category)
        evidence = f"rules:brand={brand.name};kw={phrase}{flag_evidence}"
        return Decode(
            row_id=product.row_id,
            sku=product.sku,
            category=category,
            subcategory=subcat,
            attributes=attrs,
            is_winter_rated=winter,
            tier=self.name,
            evidence=evidence,
        )
