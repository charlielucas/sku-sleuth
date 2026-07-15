"""Deterministic synthetic-catalog generator with seeded evaluation challenges."""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path

from sku_sleuth.models import APPAREL, SIZES, Brand, Product, load_brands

UNKNOWN_BRANDS = ("AXKORT", "BELVAND", "CRUMANE")

SUBCAT_PHRASES: dict[str, tuple[str, ...]] = {
    "Brake Pads": ("BRAKE PADS", "BRK PADS"),
    "Rotors": ("ROTORS", "ROTOR"),
    "Calipers": ("CALIPERS", "CALIPER"),
    "Oil Filters": ("OIL FILTER", "OIL FLTR"),
    "Air Filters": ("AIR FILTER", "AIR FLTR"),
    "Cabin Filters": ("CABIN FILTER", "CABIN FLTR"),
    "Spark Plugs": ("SPARK PLUGS", "SPARK PLUG", "SPK PLG"),
    "Ignition Coils": ("IGNITION COIL", "IGN COIL"),
    "Wiper Blades": ("WIPER BLADES", "WIPER BLADE", "WIPER"),
    "Headlight Bulbs": ("HEADLIGHT BULBS", "HEADLIGHT BULB", "HDLGHT BULB"),
    "Gloves": ("GLOVES", "GLOVE"),
    "Coveralls": ("COVERALLS", "COVERALL"),
    "Hi-Vis Jackets": ("HI-VIS JACKET", "HI VIS JACKET", "HIVIS JACKET"),
}

SUBCAT_WEIGHTS: dict[str, int] = {
    "Brake Pads": 12,
    "Rotors": 8,
    "Calipers": 5,
    "Oil Filters": 10,
    "Air Filters": 8,
    "Cabin Filters": 5,
    "Spark Plugs": 10,
    "Ignition Coils": 6,
    "Wiper Blades": 10,
    "Headlight Bulbs": 6,
    "Gloves": 8,
    "Coveralls": 6,
    "Hi-Vis Jackets": 6,
}

SUBCAT_CATEGORY: dict[str, str] = {
    sub: cat
    for cat, subs in {
        "Braking": ("Brake Pads", "Rotors", "Calipers"),
        "Filtration": ("Oil Filters", "Air Filters", "Cabin Filters"),
        "Electrical": ("Spark Plugs", "Ignition Coils"),
        "Visibility": ("Wiper Blades", "Headlight Bulbs"),
        "Apparel": ("Gloves", "Coveralls", "Hi-Vis Jackets"),
    }.items()
    for sub in subs
}

WINTER_BASE = {
    "Wiper Blades": 0.30,
    "Gloves": 0.40,
    "Coveralls": 0.30,
    "Hi-Vis Jackets": 0.20,
}
TRAP_WINTER = {
    "Wiper Blades": 0.80,
    "Gloves": 0.80,
    "Coveralls": 0.80,
    "Hi-Vis Jackets": 0.80,
    "Brake Pads": 0.15,
    "Rotors": 0.15,
}

POSITION_SUBCATS = ("Brake Pads", "Rotors", "Calipers", "Wiper Blades")
PACK_SUBCATS = ("Brake Pads", "Spark Plugs", "Wiper Blades", "Headlight Bulbs", "Gloves")
MATERIAL_CHOICES = {
    "Brake Pads": (0.7, ("ceramic", "semi-metallic")),
    "Gloves": (0.6, ("leather", "nitrile")),
    "Coveralls": (0.4, ("cotton",)),
    "Wiper Blades": (0.3, ("rubber",)),
}

POSITION_TOKENS = {
    "front": ("FRONT", "FRT"),
    "rear": ("REAR",),
    "left": ("LEFT", "LH"),
    "right": ("RIGHT", "RH"),
}
PACK_TOKENS = ("({n}-PK)", "{n}-PK", "SET OF {n}", "{n} PACK")
SIZE_TOKENS = ("SZ {s}", "SIZE {s}", "{s}")
WINTER_TOKENS_PARTS = ("WINTER", "WNTR")
WINTER_TOKENS_APPAREL = ("WINTER", "WNTR", "INSULATED", "THERMAL")


@dataclass(frozen=True)
class GeneratedProduct:
    product: Product
    truth: dict
    quality: str  # "clean" | "out_of_catalog" | "malformed"


def _pick_subcategory(rng: random.Random) -> str:
    subs = list(SUBCAT_WEIGHTS)
    weights = [SUBCAT_WEIGHTS[s] for s in subs]
    return rng.choices(subs, weights=weights, k=1)[0]


def _pick_brand(rng: random.Random, brands: tuple[Brand, ...], category: str) -> Brand:
    kind = "apparel" if category == APPAREL else "parts"
    eligible = [b for b in brands if b.kind in (kind, "both")]
    return rng.choice(eligible)


def _winter_truth(rng: random.Random, brand: Brand, subcategory: str) -> bool:
    rates = TRAP_WINTER if brand.winter_bias else WINTER_BASE
    return rng.random() < rates.get(subcategory, 0.0)


def _attribute_truth(rng: random.Random, category: str, subcategory: str) -> dict:
    attrs: dict = {}
    if subcategory in POSITION_SUBCATS and rng.random() < 0.6:
        attrs["position"] = rng.choice(("front", "rear", "left", "right"))
    if subcategory in PACK_SUBCATS and rng.random() < 0.4:
        attrs["pack_count"] = rng.choice((2, 4, 6, 8, 12))
    if category == APPAREL and rng.random() < 0.85:
        attrs["size"] = rng.choice(SIZES)
    if subcategory in MATERIAL_CHOICES:
        prob, choices = MATERIAL_CHOICES[subcategory]
        if rng.random() < prob:
            attrs["material"] = rng.choice(choices)
    return attrs


def _render_parts(
    rng: random.Random,
    brand_token: str,
    subcategory: str,
    attrs: dict,
    winter: bool,
    winter_token: str | None,
) -> list[str]:
    parts = [brand_token]
    if "material" in attrs:
        parts.append(attrs["material"].upper())
    if "position" in attrs:
        parts.append(rng.choice(POSITION_TOKENS[attrs["position"]]))
    if winter and winter_token:
        parts.append(winter_token)
    parts.append(rng.choice(SUBCAT_PHRASES[subcategory]))
    if "pack_count" in attrs:
        parts.append(rng.choice(PACK_TOKENS).format(n=attrs["pack_count"]))
    if "size" in attrs:
        parts.append(rng.choice(SIZE_TOKENS).format(s=attrs["size"]))
    return parts


def _apply_casing(rng: random.Random, title: str) -> str:
    style = rng.random()
    if style < 0.3:
        return title.lower()
    if style < 0.5:
        return title.title()
    return title


def generate(
    seed: int, rows: int, brands_path: Path = Path("seeds/brands.json")
) -> list[GeneratedProduct]:
    brands = load_brands(brands_path)
    rng = random.Random(seed)
    out: list[GeneratedProduct] = []
    for n in range(1, rows + 1):
        row_id = f"R{n:04d}"
        subcategory = _pick_subcategory(rng)
        category = SUBCAT_CATEGORY[subcategory]
        brand = _pick_brand(rng, brands, category)
        winter = _winter_truth(rng, brand, subcategory)
        attrs = _attribute_truth(rng, category, subcategory)
        sku = f"{brand.aliases[1]}-{rng.randint(1000, 9999)}"
        brand_token = brand.name if rng.random() >= 0.25 else brand.aliases[1]
        token_pool = WINTER_TOKENS_APPAREL if category == APPAREL else WINTER_TOKENS_PARTS
        winter_token = rng.choice(token_pool) if winter else None

        suppress = brand.winter_bias and winter and rng.random() < 0.5
        parts = _render_parts(
            rng, brand_token, subcategory, attrs, winter, None if suppress else winter_token
        )
        if not winter and rng.random() < 0.10:
            parts.append(rng.choice(("2YR WNTY", "WNTY 90D")))
        if category == APPAREL and "size" not in attrs and rng.random() < 0.15:
            parts.append("L")
        if rng.random() < 0.20:
            parts.append(rng.choice(("HD", "PRO", "NEW", "BULK")))
        if rng.random() < 0.30:
            parts.append(sku)
        quality = "clean"
        fate = rng.random()
        if fate < 0.03:
            quality = "malformed"
            title = rng.choice(("", "   ", "###", "?"))
        elif fate < 0.08:
            quality = "out_of_catalog"
            unknown = rng.choice(UNKNOWN_BRANDS)
            parts[0] = unknown
            sku = f"{unknown[:3]}-{sku.split('-', 1)[1]}"
            title = _apply_casing(rng, " ".join(parts))
        else:
            title = _apply_casing(rng, " ".join(parts))

        truth = {
            "category": category,
            "subcategory": subcategory,
            "attributes": attrs,
            "is_winter_rated": winter,
        }
        out.append(GeneratedProduct(Product(row_id, sku, title), truth, quality))
    return out


def write_raw_csv(items: list[GeneratedProduct], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["row_id", "sku", "title"])
        for g in items:
            writer.writerow([g.product.row_id, g.product.sku, g.product.title])


def read_raw_csv(path: Path) -> list[Product]:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [Product(r["row_id"], r["sku"], r["title"]) for r in reader]
