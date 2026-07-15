"""Headless scenario runner: generate -> decode -> evaluate -> gate -> load.

Exists for CI and for regenerating committed examples; not a user-facing CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sku_sleuth.compare import compare_batches, write_comparison_report
from sku_sleuth.engine import decode_stage
from sku_sleuth.evaluate import evaluate_batch, render_markdown, write_eval_report
from sku_sleuth.gates import load_thresholds, run_gate, write_gate_report
from sku_sleuth.generate import generate, write_raw_csv
from sku_sleuth.load import load_batch
from sku_sleuth.models import dump_json, load_jsonl
from sku_sleuth.tiers.model import StubModel

RULESETS = {"passing": "guarded", "failing": "naive"}

CONDITIONAL_LOAD_ARTIFACTS = (
    "lineage_comparison_report.json",
    "lineage_report.json",
    "reconciliation_report.json",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("passing", "failing"), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--baseline-dir",
        type=Path,
        help="Optional prior run directory for deterministic batch comparison evidence.",
    )
    args = parser.parse_args(argv)
    if args.rows <= 0:
        parser.error("--rows must be positive")

    work = args.workdir
    work.mkdir(parents=True, exist_ok=True)
    # These files are emitted only after a successful load. Remove prior
    # transcripts before reusing a work directory so a refused load cannot
    # appear to have lineage or reconciliation evidence from an earlier run.
    for name in CONDITIONAL_LOAD_ARTIFACTS:
        (work / name).unlink(missing_ok=True)
    database = work / "products.db"
    # This script materializes one deterministic scenario, not a durable
    # registry. Rebuild its local database so reruns cannot inherit stale
    # schemas or turn the committed `loaded` transcript into a `noop`.
    if database.exists():
        database.unlink()
    ruleset = RULESETS[args.scenario]

    raw = work / "raw_products.csv"
    write_raw_csv(generate(seed=args.seed, rows=args.rows), raw)
    model = StubModel.from_json(Path("seeds/stub_model_fixture.json"))
    manifest = decode_stage(raw, ruleset, model, work)

    report = evaluate_batch(
        load_jsonl(work / "decoded.jsonl"),
        load_jsonl(work / "rejects.jsonl"),
        load_jsonl(Path("seeds/gold_set.jsonl")),
        manifest,
    )
    write_eval_report(report, work / "eval_report.json")
    with open(work / "eval_report.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(render_markdown(report))

    thresholds = load_thresholds()
    gate = run_gate(report, manifest, thresholds)
    write_gate_report(gate, work / "gate_report.json")

    baseline_decoded: list[dict] = []
    baseline_rejects: list[dict] = []
    baseline_batch_id = None
    baseline_manifest = None
    if args.baseline_dir is not None:
        baseline_decoded = load_jsonl(args.baseline_dir / "decoded.jsonl")
        baseline_rejects = load_jsonl(args.baseline_dir / "rejects.jsonl")
        baseline_manifest = json.loads(
            (args.baseline_dir / "manifest.json").read_text(encoding="utf-8")
        )
        baseline_batch_id = baseline_manifest["batch_id"]
    comparison = compare_batches(
        baseline_decoded,
        load_jsonl(work / "decoded.jsonl"),
        baseline_rejects=baseline_rejects,
        current_rejects=load_jsonl(work / "rejects.jsonl"),
        baseline_batch_id=baseline_batch_id,
        current_batch_id=manifest["batch_id"],
        baseline_manifest=baseline_manifest,
        current_manifest=manifest,
    )
    write_comparison_report(comparison, work / "comparison_report.json")

    load_result = load_batch(
        work / "decoded.jsonl",
        work / "gate_report.json",
        database,
        raw_path=raw,
        rejects_path=work / "rejects.jsonl",
        schema_report_path=work / "schema_report.json",
        manifest_path=work / "manifest.json",
        eval_report_path=work / "eval_report.json",
        gold_path=Path("seeds/gold_set.jsonl"),
        thresholds=thresholds,
        # Contractual lineage is derived from the registry's verified parent.
        # The report above remains useful as optional exploratory evidence.
        comparison_report_path=None,
    )
    # The full canonical comparison has its own artifact; keep the load
    # transcript compact instead of duplicating hundreds of row IDs.
    dump_json(
        {name: value for name, value in load_result.items() if name != "comparison"},
        work / "load_result.json",
    )
    if "comparison" in load_result:
        dump_json(load_result["comparison"], work / "lineage_comparison_report.json")
    if "lineage" in load_result:
        dump_json(load_result["lineage"], work / "lineage_report.json")
    if "reconciliation" in load_result:
        dump_json(load_result["reconciliation"], work / "reconciliation_report.json")
    dump_json(
        {
            "scenario": args.scenario,
            "seed": args.seed,
            "rows": args.rows,
            "batch_id": manifest["batch_id"],
            "run_id": manifest["run_id"],
            "baseline_batch_id": baseline_batch_id,
        },
        work / "run_meta.json",
    )  # non-contractual run metadata (design §8)

    print(f"scenario={args.scenario} ruleset={ruleset} batch={manifest['batch_id'][:12]}...")
    for c in gate["checks"]:
        mark = "PASS" if c["passed"] else "FAIL"
        print(f"  {c['name']:<18} {c['actual']!s:>8} {c['op']} {c['threshold']!s:<6} {mark}")
    print(f"GATE: {'PASS' if gate['passed'] else 'FAIL'}")
    schema = manifest["schema"]
    print(
        "schema: PASS"
        + (f" additive_fields={schema['extra_fields']}" if schema["extra_fields"] else "")
    )
    print("compare: " + " ".join(f"{name}={value}" for name, value in comparison["counts"].items()))
    print(
        f"load: {load_result['status']}"
        + (
            f" ({load_result['reason']})"
            if load_result["reason"]
            else f" inserted={load_result['inserted']}"
        )
    )
    if load_result.get("reconciliation"):
        state = "PASS" if load_result["reconciliation"]["passed"] else "FAIL"
        print(f"reconciliation: {state}")
    if not gate["passed"]:
        return 1
    load_ok = load_result["status"] in {"loaded", "noop"}
    reconciliation_ok = bool(load_result.get("reconciliation", {}).get("passed"))
    if not load_ok or not reconciliation_ok:
        print("PIPELINE: FAIL — gate passed but load/reconciliation did not complete")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
