"""Core datatypes, taxonomy constants, and file IO helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

TAXONOMY: dict[str, tuple[str, ...]] = {
    "Braking": ("Brake Pads", "Rotors", "Calipers"),
    "Filtration": ("Oil Filters", "Air Filters", "Cabin Filters"),
    "Electrical": ("Spark Plugs", "Ignition Coils"),
    "Visibility": ("Wiper Blades", "Headlight Bulbs"),
    "Apparel": ("Gloves", "Coveralls", "Hi-Vis Jackets"),
}
APPAREL = "Apparel"
POSITIONS = ("front", "rear", "left", "right")
SIZES = ("S", "M", "L", "XL", "2XL")
MATERIALS = ("ceramic", "semi-metallic", "leather", "nitrile", "rubber", "cotton")


@dataclass(frozen=True)
class Product:
    row_id: str
    sku: str
    title: str


@dataclass(frozen=True)
class Decode:
    row_id: str
    sku: str
    category: str
    subcategory: str
    attributes: dict
    is_winter_rated: bool
    tier: str
    evidence: str


@dataclass(frozen=True)
class Brand:
    name: str
    aliases: tuple[str, ...]
    kind: str  # "parts" | "apparel" | "both"
    winter_bias: bool = False


def decode_to_dict(d: Decode) -> dict:
    return asdict(d)


def decode_from_dict(data: dict) -> Decode:
    return Decode(**data)


def dump_json(data, path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, sort_keys=True, indent=2) + "\n")


def dump_jsonl(rows: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def canonical_json_bytes(data) -> bytes:
    """Serialize JSON-compatible data for semantic content fingerprints."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_json(data) -> str:
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


def sha256_jsonl_rows(rows: list[dict]) -> str:
    """Hash rows exactly as :func:`dump_jsonl` writes them."""
    payload = b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows)
    return hashlib.sha256(payload).hexdigest()


def load_brands(path: Path) -> tuple[Brand, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    brands = [
        Brand(
            name=b["name"],
            aliases=tuple(b["aliases"]),
            kind=b["kind"],
            winter_bias=bool(b.get("winter_bias", False)),
        )
        for b in raw
    ]
    return tuple(sorted(brands, key=lambda b: b.name))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
