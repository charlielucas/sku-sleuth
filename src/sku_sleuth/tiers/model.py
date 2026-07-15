"""Tier 3: pluggable model tier — deterministic stub + optional API adapter."""

from __future__ import annotations

import json
from pathlib import Path

from sku_sleuth.models import TAXONOMY, Decode, Product, sha256_json
from sku_sleuth.tiers.rules import normalize_title


class StubModel:
    name = "model"

    def __init__(self, fixture: dict[str, dict]):
        self.fixture = fixture

    @classmethod
    def from_json(cls, path: Path) -> StubModel:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def decode(self, product: Product) -> Decode | None:
        entry = self.fixture.get(normalize_title(product.title))
        if entry is None:
            return None
        return Decode(
            row_id=product.row_id,
            sku=product.sku,
            category=entry["category"],
            subcategory=entry["subcategory"],
            attributes=dict(entry.get("attributes", {})),
            is_winter_rated=bool(entry["is_winter_rated"]),
            tier=self.name,
            evidence="model:stub-fixture",
        )

    def identity(self) -> dict:
        return {
            "adapter": type(self).__name__,
            "fixture_sha256": sha256_json(self.fixture),
            "deterministic": True,
        }


def build_prompt(product: Product) -> str:
    taxonomy_lines = "\n".join(f"- {cat}: {', '.join(subs)}" for cat, subs in TAXONOMY.items())
    return (
        "You classify one product title from a synthetic parts/apparel catalog.\n"
        f"Title: {product.title}\n\n"
        "Valid categories and subcategories:\n"
        f"{taxonomy_lines}\n\n"
        "Reply with ONLY a JSON object: "
        '{"category": ..., "subcategory": ..., '
        '"attributes": {"position"?, "pack_count"?, "size"?, "material"?}, '
        '"is_winter_rated": true|false}. '
        'If you are not confident, reply {"abstain": true}.'
    )


def parse_response(text: str, product: Product) -> Decode | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("abstain") is True:
        return None
    required = ("category", "subcategory", "attributes", "is_winter_rated")
    if not all(k in data for k in required):
        return None
    if not isinstance(data["category"], str) or not isinstance(data["subcategory"], str):
        return None
    if not isinstance(data["attributes"], dict) or not isinstance(data["is_winter_rated"], bool):
        return None
    return Decode(
        row_id=product.row_id,
        sku=product.sku,
        category=data["category"],
        subcategory=data["subcategory"],
        attributes=data["attributes"],
        is_winter_rated=data["is_winter_rated"],
        tier="model",
        evidence="model:anthropic",
    )


def extract_text(content: list) -> str:
    """First text block from an API response's content list; '' if none."""
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


class AnthropicModel:
    name = "model"

    def __init__(self, model_id: str = "claude-sonnet-5"):
        self.model_id = model_id

    def identity(self) -> dict:
        return {
            "adapter": type(self).__name__,
            "model_id": self.model_id,
            "prompt_contract": "1",
            "max_tokens": 1024,
            "deterministic": False,
        }

    def _call_api(self, product: Product) -> str:
        import anthropic  # requires the [anthropic] extra + ANTHROPIC_API_KEY

        client = anthropic.Anthropic()
        message = client.messages.create(
            model=self.model_id,
            max_tokens=1024,  # caps thinking + text combined; claude-sonnet-5 has adaptive
            # thinking on by default, so content[0] is often a thinking block, not text
            messages=[{"role": "user", "content": build_prompt(product)}],
        )
        return extract_text(message.content)

    def decode(self, product: Product) -> Decode | None:
        try:
            return parse_response(self._call_api(product), product)
        except Exception:
            return None  # abstain, never guess (design §7)
