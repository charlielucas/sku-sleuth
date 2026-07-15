"""Tier loop, row buckets, batch identity, and the decode stage artifact writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from sku_sleuth.models import (
    Decode,
    Product,
    decode_to_dict,
    dump_json,
    dump_jsonl,
    load_brands,
    sha256_file,
    sha256_json,
)
from sku_sleuth.schema import RAW_SCHEMA_VERSION, validate_raw_csv
from sku_sleuth.tiers.catalog import CatalogTier
from sku_sleuth.tiers.rules import RulesTier
from sku_sleuth.validate import validate_decode

DECODER_VERSION = "2"
DECODE_SCHEMA_VERSION = "1"


def mint_batch_id(identity: dict) -> str:
    """Mint a semantic identity from the complete effective decode contract."""
    return sha256_json(identity)


def _implementation_fingerprints() -> dict[str, str]:
    """Bind the batch to decode code, not just manually bumped version strings."""
    here = Path(__file__).resolve().parent
    paths = {
        "engine": here / "engine.py",
        "models": here / "models.py",
        "raw_schema": here / "schema.py",
        "validation": here / "validate.py",
        "catalog_tier": here / "tiers" / "catalog.py",
        "rules_tier": here / "tiers" / "rules.py",
        "model_tier": here / "tiers" / "model.py",
    }
    return {name: sha256_file(path) for name, path in sorted(paths.items())}


def build_batch_identity(
    products: list[Product],
    ruleset: str,
    model_tier,
    brands: tuple,
    catalog_tier: CatalogTier,
) -> dict:
    if not hasattr(model_tier, "identity"):
        raise TypeError("model tier must expose a deterministic identity() contract")
    model_identity = model_tier.identity()
    return {
        "contract": {
            "decoder_version": DECODER_VERSION,
            "decode_schema_version": DECODE_SCHEMA_VERSION,
            "raw_schema_version": RAW_SCHEMA_VERSION,
        },
        "inputs": {
            "raw_content_sha256": sha256_json(
                [asdict(product) for product in sorted(products, key=lambda item: item.row_id)]
            ),
            "catalog_content_sha256": sha256_json(catalog_tier.entries),
            "brands_content_sha256": sha256_json(
                [
                    {
                        "name": brand.name,
                        "aliases": sorted(brand.aliases),
                        "kind": brand.kind,
                        "winter_bias": brand.winter_bias,
                    }
                    for brand in brands
                ]
            ),
        },
        "configuration": {
            "ruleset": ruleset,
            "model": model_identity,
        },
        "implementation": _implementation_fingerprints(),
    }


def is_malformed(product: Product) -> str | None:
    if not product.title.strip():
        return "empty_title"
    if len(product.title.strip()) < 4:
        return "short_title"
    if not product.sku.strip():
        return "missing_sku"
    return None


@dataclass
class BatchOutcome:
    decodes: list[Decode] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    tier_invalid: dict = field(default_factory=dict)


def run_batch(products: list[Product], tiers: list) -> BatchOutcome:
    out = BatchOutcome()
    counts = {"total": len(products), "decoded": 0, "abstained": 0, "quarantined": 0, "errored": 0}
    for product in products:
        reason = is_malformed(product)
        if reason is not None:
            counts["quarantined"] += 1
            out.rejects.append(
                {
                    "row_id": product.row_id,
                    "sku": product.sku,
                    "title": product.title,
                    "bucket": "quarantined",
                    "reason": reason,
                }
            )
            continue
        decoded = errored = False
        for tier in tiers:
            try:
                d = tier.decode(product)
            except Exception as e:  # noqa: BLE001 — bucket, never crash the batch
                counts["errored"] += 1
                out.rejects.append(
                    {
                        "row_id": product.row_id,
                        "sku": product.sku,
                        "title": product.title,
                        "bucket": "errored",
                        "reason": f"{tier.name}:{type(e).__name__}",
                    }
                )
                errored = True
                break
            if d is None:
                continue
            if validate_decode(d):
                out.tier_invalid[tier.name] = out.tier_invalid.get(tier.name, 0) + 1
                continue
            counts["decoded"] += 1
            out.decodes.append(d)
            decoded = True
            break
        if not decoded and not errored:
            counts["abstained"] += 1
            out.rejects.append(
                {
                    "row_id": product.row_id,
                    "sku": product.sku,
                    "title": product.title,
                    "bucket": "abstained",
                    "reason": "no_confident_decode",
                }
            )
    out.counts = counts
    return out


def decode_stage(
    raw_csv: Path,
    ruleset: str,
    model_tier,
    out_dir: Path,
    brands_path: Path = Path("seeds/brands.json"),
    catalog_path: Path = Path("seeds/catalog.csv"),
) -> dict:
    from sku_sleuth.generate import read_raw_csv

    out_dir.mkdir(parents=True, exist_ok=True)
    schema_report = validate_raw_csv(raw_csv, out_dir / "schema_report.json")
    products = read_raw_csv(raw_csv)
    brands = load_brands(brands_path)
    catalog_tier = CatalogTier.from_csv(catalog_path)
    tiers = [
        catalog_tier,
        RulesTier(brands, ruleset=ruleset),
        model_tier,
    ]
    identity = build_batch_identity(
        products,
        ruleset,
        model_tier,
        brands,
        catalog_tier,
    )
    if identity["inputs"]["raw_content_sha256"] != schema_report["effective_content_sha256"]:
        raise RuntimeError("schema/decode raw-content fingerprint mismatch")
    outcome = run_batch(products, tiers)
    decoded_rows = sorted((decode_to_dict(d) for d in outcome.decodes), key=lambda r: r["row_id"])
    rejects_rows = sorted(outcome.rejects, key=lambda r: r["row_id"])
    dump_jsonl(decoded_rows, out_dir / "decoded.jsonl")
    dump_jsonl(rejects_rows, out_dir / "rejects.jsonl")
    batch_id = mint_batch_id(identity)
    decoded_sha256 = sha256_file(out_dir / "decoded.jsonl")
    rejects_sha256 = sha256_file(out_dir / "rejects.jsonl")
    manifest = {
        "batch_id": batch_id,
        "run_id": sha256_json(
            {
                "batch_id": batch_id,
                "decoded_sha256": decoded_sha256,
                "rejects_sha256": rejects_sha256,
            }
        ),
        "decoder_version": DECODER_VERSION,
        "decode_schema_version": DECODE_SCHEMA_VERSION,
        "ruleset": ruleset,
        "identity": identity,
        "raw_sha256": sha256_file(raw_csv),
        "total": outcome.counts["total"],
        "counts": outcome.counts,
        "tier_invalid": outcome.tier_invalid,
        "decoded_sha256": decoded_sha256,
        "rejects_sha256": rejects_sha256,
        "schema_report_sha256": sha256_json(schema_report),
        "schema": {
            "passed": schema_report["passed"],
            "missing_required_fields": schema_report["missing_required_fields"],
            "extra_fields": schema_report["extra_fields"],
            "drift_status": schema_report["drift_status"],
            "drift_events": schema_report["drift_events"],
            "missing_value_counts": schema_report["missing_value_counts"],
            "null_value_counts": schema_report["null_value_counts"],
            "effective_content_sha256": schema_report["effective_content_sha256"],
            "row_count": schema_report["row_count"],
        },
    }
    dump_json(manifest, out_dir / "manifest.json")
    return manifest
