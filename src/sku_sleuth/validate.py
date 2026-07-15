"""Structural validation applied to every tier's output by the engine."""

from __future__ import annotations

from sku_sleuth.models import APPAREL, MATERIALS, POSITIONS, SIZES, TAXONOMY, Decode

ALLOWED_ATTRS = {"position", "pack_count", "size", "material"}


def validate_decode(d: Decode) -> list[str]:
    errors: list[str] = []
    if d.category not in TAXONOMY:
        errors.append(f"unknown category: {d.category!r}")
    elif d.subcategory not in TAXONOMY[d.category]:
        errors.append(f"subcategory {d.subcategory!r} not in category {d.category!r}")
    if not isinstance(d.is_winter_rated, bool):
        errors.append("is_winter_rated must be a bool")

    for key in sorted(d.attributes):
        value = d.attributes[key]
        if key not in ALLOWED_ATTRS:
            errors.append(f"unknown attribute: {key!r}")
        elif key == "position":
            if value not in POSITIONS:
                errors.append(f"bad position: {value!r}")
            elif d.category == APPAREL:
                errors.append("position not allowed for Apparel")
        elif key == "size":
            if value not in SIZES:
                errors.append(f"bad size: {value!r}")
            elif d.category != APPAREL:
                errors.append("size only allowed for Apparel")
        elif key == "pack_count":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 24:
                errors.append(f"bad pack_count: {value!r}")
        elif key == "material" and value not in MATERIALS:
            errors.append(f"bad material: {value!r}")
    return errors
