"""Tier 1: authoritative decode by exact (normalized) SKU match."""

from __future__ import annotations

import csv
from pathlib import Path

from sku_sleuth.models import Decode, Product


def normalize_sku(sku: str) -> str:
    return sku.strip().upper().replace(" ", "")


class CatalogTier:
    name = "catalog"

    def __init__(self, entries: dict[str, dict]):
        self.entries = entries

    @classmethod
    def from_csv(cls, path: Path) -> CatalogTier:
        entries: dict[str, dict] = {}
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                attrs: dict = {}
                if row["position"]:
                    attrs["position"] = row["position"]
                if row["pack_count"]:
                    attrs["pack_count"] = int(row["pack_count"])
                if row["size"]:
                    attrs["size"] = row["size"]
                if row["material"]:
                    attrs["material"] = row["material"]
                entries[normalize_sku(row["sku"])] = {
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "attributes": attrs,
                    "is_winter_rated": row["is_winter_rated"] == "1",
                }
        return cls(entries)

    def decode(self, product: Product) -> Decode | None:
        key = normalize_sku(product.sku)
        entry = self.entries.get(key)
        if entry is None:
            return None
        return Decode(
            row_id=product.row_id,
            sku=product.sku,
            category=entry["category"],
            subcategory=entry["subcategory"],
            attributes=dict(entry["attributes"]),
            is_winter_rated=entry["is_winter_rated"],
            tier=self.name,
            evidence=f"catalog:{key}",
        )
