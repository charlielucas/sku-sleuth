"""Score a decoded batch against the gold set. Measurement only — no policy."""

from __future__ import annotations

from pathlib import Path

from sku_sleuth.models import dump_json, sha256_json, sha256_jsonl_rows

ATTR_KEYS = ("material", "pack_count", "position", "size")
EVALUATION_VERSION = "2"


class EvaluationValidationError(ValueError):
    """Field-level contract violation in an evaluation input artifact."""

    def __init__(self, *, artifact: str, row_id: str, field: str, value: object) -> None:
        self.details = {
            "artifact": artifact,
            "row_id": row_id,
            "field": field,
            "expected": "JSON boolean",
            "actual_type": type(value).__name__,
        }
        super().__init__(
            f"{artifact} row {row_id!r} field {field!r} must be a JSON boolean; "
            f"got {type(value).__name__}"
        )


def _winter_flag(row: dict, artifact: str) -> bool:
    value = row.get("is_winter_rated")
    if type(value) is not bool:
        raise EvaluationValidationError(
            artifact=artifact,
            row_id=row["row_id"],
            field="is_winter_rated",
            value=value,
        )
    return value


def _r4(x: float) -> float:
    return round(x, 4)


def _ratio(num: int, den: int) -> float:
    return _r4(num / den) if den else 1.0


def evaluate_batch(
    decoded: list[dict], rejects: list[dict], gold: list[dict], manifest: dict
) -> dict:
    if not gold:
        raise ValueError("gold set must not be empty")
    if manifest["counts"]["total"] <= 0:
        raise ValueError("manifest total must be positive")
    if not manifest.get("schema", {}).get("passed", False):
        raise ValueError("manifest schema contract did not pass")

    def index_unique(rows: list[dict], artifact: str) -> dict[str, dict]:
        indexed: dict[str, dict] = {}
        for row in rows:
            rid = row.get("row_id")
            if not isinstance(rid, str) or not rid:
                raise ValueError(f"{artifact} contains a missing or invalid row_id")
            if rid in indexed:
                raise ValueError(f"duplicate row_id {rid!r} in {artifact}")
            indexed[rid] = row
        return indexed

    by_id = index_unique(decoded, "decoded")
    rej_by_id = index_unique(rejects, "rejects")
    gold_by_id = index_unique(gold, "gold")
    decoded_winter = {rid: _winter_flag(row, "decoded") for rid, row in by_id.items()}
    gold_winter: dict[str, bool] = {}
    for rid, row in gold_by_id.items():
        if row.get("expect") not in {"decode", "abstain"}:
            raise ValueError(f"gold row {rid} has invalid expectation")
        if row["expect"] == "decode":
            gold_winter[rid] = _winter_flag(row, "gold")
    overlap = sorted(set(by_id) & set(rej_by_id))
    if overlap:
        raise ValueError(f"row_id present in decoded and rejects: {overlap[0]!r}")
    if len(decoded) != manifest["counts"]["decoded"]:
        raise ValueError("decoded row count does not match manifest")
    reject_count = sum(
        manifest["counts"][bucket] for bucket in ("abstained", "quarantined", "errored")
    )
    if len(rejects) != reject_count:
        raise ValueError("reject row count does not match manifest")
    actual_reject_counts = {bucket: 0 for bucket in ("abstained", "quarantined", "errored")}
    for row in rejects:
        bucket = row.get("bucket")
        if bucket not in actual_reject_counts:
            raise ValueError(f"unknown reject bucket: {bucket!r}")
        actual_reject_counts[bucket] += 1
    for bucket, actual_count in actual_reject_counts.items():
        if actual_count != manifest["counts"][bucket]:
            raise ValueError(f"{bucket} row count does not match manifest")
    if len(decoded) + len(rejects) != manifest["counts"]["total"]:
        raise ValueError("artifact row counts do not cover the manifest total")
    decoded_sha256 = sha256_jsonl_rows(decoded)
    rejects_sha256 = sha256_jsonl_rows(rejects)
    if decoded_sha256 != manifest["decoded_sha256"]:
        raise ValueError("decoded artifact hash does not match manifest")
    if rejects_sha256 != manifest["rejects_sha256"]:
        raise ValueError("rejects artifact hash does not match manifest")

    manifest_sha256 = sha256_json(manifest)
    gold_sha256 = sha256_jsonl_rows(gold)

    flag_tp = flag_fp = flag_fn = 0
    cat_total = cat_correct = 0
    # Per-category support includes every expected-decode row. Abstention and
    # quarantine are therefore recall misses rather than disappearing from metrics.
    cat_tp: dict[str, int] = {}
    cat_fp: dict[str, int] = {}
    cat_support: dict[str, int] = {}
    attr_stats = {k: {"correct": 0, "total": 0} for k in ATTR_KEYS}
    tier_stats: dict[str, dict[str, int]] = {}
    confusion: dict[tuple[str, str], int] = {}
    g_decoded = g_abstained = g_quarantined = unexpected = correct_abstains = 0

    for d in decoded:
        tier_stats.setdefault(d["tier"], {"rows": 0, "gold_rows": 0, "gold_correct": 0})
        tier_stats[d["tier"]]["rows"] += 1

    for g in gold_by_id.values():
        rid = g["row_id"]
        pred = by_id.get(rid)
        rej = rej_by_id.get(rid)
        if pred is None and rej is None:
            raise ValueError(f"gold row {rid} missing from decoded and rejects")

        if g["expect"] == "abstain":
            if pred is not None:
                unexpected += 1
                cat_fp[pred["category"]] = cat_fp.get(pred["category"], 0) + 1
                if decoded_winter[rid]:
                    flag_fp += 1
            else:
                correct_abstains += 1
            continue

        expected_winter = gold_winter[rid]
        cat_total += 1
        cat_support[g["category"]] = cat_support.get(g["category"], 0) + 1
        for k in ATTR_KEYS:
            attr_stats[k]["total"] += 1
        if pred is None:
            if rej["bucket"] == "quarantined":
                g_quarantined += 1
            else:
                g_abstained += 1
            if expected_winter:
                flag_fn += 1  # abstained/quarantined positives are recall misses
            key = (
                f"{g['category']}/{g['subcategory']}",
                f"<{rej['bucket']}>",
            )
            confusion[key] = confusion.get(key, 0) + 1
            continue

        g_decoded += 1
        ts = tier_stats.setdefault(pred["tier"], {"rows": 0, "gold_rows": 0, "gold_correct": 0})
        ts["gold_rows"] += 1

        cat_ok = pred["category"] == g["category"]
        sub_ok = pred["subcategory"] == g["subcategory"]
        if cat_ok:
            cat_correct += 1
        if not (cat_ok and sub_ok):
            key = (
                f"{g['category']}/{g['subcategory']}",
                f"{pred['category']}/{pred['subcategory']}",
            )
            confusion[key] = confusion.get(key, 0) + 1

        if cat_ok:
            cat_tp[g["category"]] = cat_tp.get(g["category"], 0) + 1
        else:
            cat_fp[pred["category"]] = cat_fp.get(pred["category"], 0) + 1

        pred_winter = decoded_winter[rid]
        if pred_winter and expected_winter:
            flag_tp += 1
        elif pred_winter and not expected_winter:
            flag_fp += 1
        elif expected_winter and not pred_winter:
            flag_fn += 1

        for k in ATTR_KEYS:
            gv = g["attributes"].get(k)
            pv = pred["attributes"].get(k)
            if gv == pv:
                attr_stats[k]["correct"] += 1

        if cat_ok and sub_ok and pred_winter == expected_winter:
            ts["gold_correct"] += 1

    categories = {}
    for cat in sorted(set(cat_support) | set(cat_tp) | set(cat_fp)):
        tp = cat_tp.get(cat, 0)
        fp = cat_fp.get(cat, 0)
        support = cat_support.get(cat, 0)
        precision = _ratio(tp, tp + fp)
        recall = _ratio(tp, support)
        f1 = _r4(2 * precision * recall / (precision + recall)) if precision + recall else 0.0
        categories[cat] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }

    pairs = sorted(confusion.items(), key=lambda kv: (-kv[1], kv[0]))
    category_accuracy = _ratio(cat_correct, cat_total)
    conditional_category_accuracy = _ratio(cat_correct, g_decoded)
    return {
        "evaluation_version": EVALUATION_VERSION,
        "batch_id": manifest["batch_id"],
        "decoded_sha256": decoded_sha256,
        "rejects_sha256": rejects_sha256,
        "manifest_sha256": manifest_sha256,
        "gold_sha256": gold_sha256,
        "evaluation_input_sha256": sha256_json(
            {
                "evaluation_version": EVALUATION_VERSION,
                "manifest_sha256": manifest_sha256,
                "decoded_sha256": decoded_sha256,
                "rejects_sha256": rejects_sha256,
                "gold_sha256": gold_sha256,
            }
        ),
        "coverage": _ratio(manifest["counts"]["decoded"], manifest["counts"]["total"]),
        "counts": manifest["counts"],
        "tier_invalid": manifest["tier_invalid"],
        "flag": {
            "tp": flag_tp,
            "fp": flag_fp,
            "fn": flag_fn,
            "precision": _ratio(flag_tp, flag_tp + flag_fp),
            "recall": _ratio(flag_tp, flag_tp + flag_fn),
        },
        "category_accuracy": category_accuracy,
        "conditional_category_accuracy": conditional_category_accuracy,
        "classification": {
            "expected_decode_rows": cat_total,
            "decoded_expected_rows": g_decoded,
            "category_correct": cat_correct,
            "end_to_end_category_accuracy": category_accuracy,
            "conditional_category_accuracy": conditional_category_accuracy,
        },
        "categories": categories,
        "attributes": {
            k: {
                "correct": v["correct"],
                "total": v["total"],
                "accuracy": _ratio(v["correct"], v["total"]),
            }
            for k, v in attr_stats.items()
        },
        "tiers": tier_stats,
        "confusion_pairs": [[a, b, n] for (a, b), n in pairs],
        "gold": {
            "total": len(gold),
            "decoded": g_decoded,
            "abstained": g_abstained,
            "quarantined": g_quarantined,
            "unexpected_decodes": unexpected,
            "correct_abstains": correct_abstains,
        },
    }


def write_eval_report(report: dict, path: Path) -> None:
    dump_json(report, path)


def render_markdown(report: dict) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"Batch `{report['batch_id'][:12]}…` — coverage {report['coverage']:.2%}",
        "",
        "## Winter flag",
        f"precision {report['flag']['precision']:.4f} / recall {report['flag']['recall']:.4f}"
        f" (tp={report['flag']['tp']} fp={report['flag']['fp']} fn={report['flag']['fn']})",
        "",
        "## Categories",
        f"end-to-end accuracy {report['category_accuracy']:.4f} / "
        f"conditional-on-decode {report['conditional_category_accuracy']:.4f}",
        "",
        "Expected-decode abstentions and quarantines count as end-to-end misses; "
        "coverage is reported separately.",
        "",
        "| category | precision | recall | f1 | support |",
        "|---|---|---|---|---|",
    ]
    for cat, s in report["categories"].items():
        lines.append(f"| {cat} | {s['precision']} | {s['recall']} | {s['f1']} | {s['support']} |")
    lines += ["", "## Attributes", "", "| attribute | accuracy | n |", "|---|---|---|"]
    for k, a in report["attributes"].items():
        lines.append(f"| {k} | {a['accuracy']} | {a['total']} |")
    if report["confusion_pairs"]:
        lines += ["", "## Confusion pairs", ""]
        for gold_key, pred_key, n in report["confusion_pairs"]:
            lines.append(f"- {gold_key} → {pred_key} ×{n}")
    return "\n".join(lines) + "\n"
