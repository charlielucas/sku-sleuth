"""SKU Sleuth gate control room. Rendering only — all logic lives in sku_sleuth."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import streamlit as st

from sku_sleuth.compare import compare_batches, write_comparison_report
from sku_sleuth.engine import decode_stage
from sku_sleuth.evaluate import evaluate_batch, write_eval_report
from sku_sleuth.gates import load_thresholds, run_gate
from sku_sleuth.generate import generate, read_raw_csv, write_raw_csv
from sku_sleuth.load import load_batch, read_registry
from sku_sleuth.models import load_jsonl, sha256_file, sha256_json
from sku_sleuth.tiers.model import StubModel

RUNS = Path("runs")
REGISTRY_DB = RUNS / "control_room_v3.db"
BATCHES = {"Batch 1 — 1,200 rows": 1200, "Batch 2 — 1,400 rows (superset)": 1400}
DEFAULTS = load_thresholds(Path("gates.toml"))
SLIDER_SPEC = [
    ("flag_precision", 0.90, 1.00, 0.005),
    ("category_accuracy", 0.80, 1.00, 0.005),
    ("coverage", 0.50, 1.00, 0.01),
    ("error_rows", 0, 20, 1),
    ("quarantine_rate", 0.0, 0.30, 0.01),
]

st.set_page_config(page_title="SKU Sleuth — gate control room", layout="wide")


def render_table(rows: list[dict]) -> None:
    """Render a small Markdown table without importing Pandas/PyArrow.

    This keeps AppTest stable on Python 3.12 with the current Streamlit lock,
    whose Arrow string conversion can segfault inside ``st.dataframe``.
    """
    if not rows:
        st.caption("No rows")
        return
    columns = list(rows[0])

    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(row.get(col, "")) for col in columns) + " |" for row in rows
    )
    st.markdown("\n".join(lines))


@st.cache_data(show_spinner="Decoding batch (measurement — the slow part)…")
def measured(
    rows: int, ruleset: str, model_name: str, input_fingerprint: str
) -> tuple[dict, dict, list[dict], list[dict], dict, str]:
    """Generate + decode + evaluate, cached by every effective file input."""
    RUNS.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{ruleset}-{rows}-", dir=RUNS))
    raw = work / "raw_products.csv"
    write_raw_csv(generate(seed=42, rows=rows), raw)
    if model_name.startswith("Anthropic"):
        from sku_sleuth.tiers.model import AnthropicModel

        model_tier = AnthropicModel()
    else:
        model_tier = StubModel.from_json(Path("seeds/stub_model_fixture.json"))
    manifest = decode_stage(raw, ruleset, model_tier, work)
    decoded = load_jsonl(work / "decoded.jsonl")
    rejects = load_jsonl(work / "rejects.jsonl")
    gold = load_jsonl(Path("seeds/gold_set.jsonl"))
    report = evaluate_batch(decoded, rejects, gold, manifest)
    write_eval_report(report, work / "eval_report.json")
    baseline_manifest = None
    if ruleset == "naive" and model_name.startswith("StubModel"):
        baseline_work = Path(tempfile.mkdtemp(prefix=f"guarded-{rows}-", dir=RUNS))
        baseline_manifest = decode_stage(
            raw,
            "guarded",
            StubModel.from_json(Path("seeds/stub_model_fixture.json")),
            baseline_work,
        )
        baseline_decoded = load_jsonl(baseline_work / "decoded.jsonl")
        baseline_rejects = load_jsonl(baseline_work / "rejects.jsonl")
    elif rows > 1200:
        baseline_decoded = [row for row in decoded if int(row["row_id"][1:]) <= 1200]
        baseline_rejects = [row for row in rejects if int(row["row_id"][1:]) <= 1200]
    else:
        baseline_decoded, baseline_rejects = [], []
    comparison = compare_batches(
        baseline_decoded,
        decoded,
        baseline_rejects=baseline_rejects,
        current_rejects=rejects,
        baseline_batch_id=baseline_manifest["batch_id"] if baseline_manifest else None,
        current_batch_id=manifest["batch_id"],
        baseline_manifest=baseline_manifest,
        current_manifest=manifest,
    )
    write_comparison_report(comparison, work / "comparison_report.json")
    return manifest, report, decoded, rejects, comparison, str(work)


with st.sidebar:
    st.title("SKU Sleuth")
    batch_label = st.radio("Batch", list(BATCHES), key="batch")
    ruleset = st.radio(
        "Ruleset",
        ("guarded", "naive"),
        key="ruleset",
        help="naive = brand-default winter flag (designed challenge #1)",
    )
    model_options = ["StubModel (deterministic)"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        model_options.append("AnthropicModel (non-deterministic, calls the API)")
    model_choice = st.selectbox(
        "Model tier",
        model_options,
        key="model_tier",
        help="The stub answers a committed fixture and abstains otherwise.",
    )
    st.divider()
    st.caption("Gate thresholds (policy — instant, measurement stays cached)")

    def _reset_thresholds() -> None:
        # callbacks run before the script, where writing widget state is legal;
        # assigning st.session_state[key] after the slider renders would raise
        for name, *_ in SLIDER_SPEC:
            st.session_state[name] = DEFAULTS[name]

    thresholds = {}
    for name, lo, hi, step in SLIDER_SPEC:
        st.session_state.setdefault(name, DEFAULTS[name])
        help_text = (
            "~50 gold flag positives, ~40 decoded TPs: one FP ≈ 0.975, "
            "two ≈ 0.952 — 0.97 tolerates exactly one"
            if name == "flag_precision"
            else None
        )
        thresholds[name] = st.slider(name, lo, hi, step=step, key=name, help=help_text)
    st.button("Reset thresholds", on_click=_reset_thresholds)

measurement_input_fingerprint = sha256_json(
    {
        "brands": sha256_file(Path("seeds/brands.json")),
        "catalog": sha256_file(Path("seeds/catalog.csv")),
        "model_fixture": sha256_file(Path("seeds/stub_model_fixture.json")),
        "gold": sha256_file(Path("seeds/gold_set.jsonl")),
        "implementation": {
            path: sha256_file(Path(path))
            for path in (
                "src/sku_sleuth/generate.py",
                "src/sku_sleuth/models.py",
                "src/sku_sleuth/schema.py",
                "src/sku_sleuth/engine.py",
                "src/sku_sleuth/validate.py",
                "src/sku_sleuth/evaluate.py",
                "src/sku_sleuth/compare.py",
                "src/sku_sleuth/tiers/catalog.py",
                "src/sku_sleuth/tiers/rules.py",
                "src/sku_sleuth/tiers/model.py",
            )
        },
    }
)
manifest, report, decoded, rejects, comparison, workdir = measured(
    BATCHES[batch_label], ruleset, model_choice, measurement_input_fingerprint
)
gate = run_gate(report, manifest, thresholds)

st.header("Batch overview")
c1, c2, c3, c4, c5, c6 = st.columns(6)
counts = manifest["counts"]
c1.metric("rows", counts["total"])
c2.metric("decoded", counts["decoded"])
c3.metric("abstained", counts["abstained"])
c4.metric("quarantined", counts["quarantined"])
c5.metric("errored", counts["errored"])
c6.metric("coverage", f"{report['coverage']:.2%}")
st.caption(
    f"recipe batch_id `{manifest['batch_id']}` · artifact run_id `{manifest['run_id']}` · "
    f"decoded sha256 `{manifest['decoded_sha256']}`"
)
schema = manifest["schema"]
schema_message = "raw schema contract PASS"
if schema["extra_fields"]:
    schema_message += f" · additive fields observed: {', '.join(schema['extra_fields'])}"
st.info(schema_message)
with st.expander("Decode recipe identity"):
    st.json(manifest["identity"])

st.header("Evaluation (measurement)")
f = report["flag"]
st.subheader(f"Winter flag: precision {f['precision']:.4f} · recall {f['recall']:.4f}")
st.caption(
    f"tp={f['tp']} fp={f['fp']} fn={f['fn']} — the flag feeds a fictional "
    "seasonal-readiness view, so false positives are the expensive error."
)
e1, e2 = st.columns(2)
with e1:
    st.write("Per category")
    render_table(
        [{"category": category, **stats} for category, stats in report["categories"].items()]
    )
    st.write("Per tier (gold rows)")
    render_table([{"tier": tier, **stats} for tier, stats in report["tiers"].items()])
with e2:
    st.write("Attributes")
    render_table(
        [{"attribute": attribute, **stats} for attribute, stats in report["attributes"].items()]
    )
    if report["confusion_pairs"]:
        st.write("Confusion pairs (gold → predicted)")
        render_table(
            [{"gold": a, "predicted": b, "count": n} for a, b, n in report["confusion_pairs"]]
        )

st.header("Batch comparison")
if ruleset == "naive" and model_choice.startswith("StubModel"):
    baseline_label = "the same rows under the guarded ruleset"
else:
    baseline_label = "empty baseline" if BATCHES[batch_label] == 1200 else "1,200-row prefix"
st.caption(f"Current batch compared with {baseline_label}; every row partitions deterministically.")
render_table([comparison["counts"] | comparison["change_types"]])
if comparison["transitions"]:
    render_table(
        [
            {"transition": transition, "rows": count}
            for transition, count in comparison["transitions"].items()
        ]
    )
if comparison["field_change_counts"]:
    render_table(
        [
            {"field": field, "changed_rows": count}
            for field, count in comparison["field_change_counts"].items()
        ]
    )
if comparison["changed_rows"]:
    with st.expander("Changed-row evidence (first 20)"):
        st.json(comparison["changed_rows"][:20])

st.header("Gate verdict (policy)")
render_table(
    [
        {
            "check": c["name"],
            "actual": c["actual"],
            "op": c["op"],
            "threshold": c["threshold"],
            "result": "PASS" if c["passed"] else "FAIL",
        }
        for c in gate["checks"]
    ]
)
if gate["passed"]:
    st.success("GATE: PASS — this batch may be loaded.")
else:
    st.error("GATE: FAIL — the loader will refuse this batch.")

st.header("Load")
tamper = st.toggle(
    "Tamper with decoded.jsonl before loading (flip one byte)",
    key="tamper",
    help="Demonstrates the hash chain: the gate approved specific bytes.",
)
if st.button(f"Attempt load into {REGISTRY_DB}"):
    work = Path(workdir)
    from sku_sleuth.gates import write_gate_report

    write_gate_report(gate, work / "gate_report.json")
    decoded_path = work / "decoded.jsonl"
    if tamper:
        blob = bytearray(decoded_path.read_bytes())
        blob[len(blob) // 2] ^= 0x01
        tampered = work / "decoded.tampered.jsonl"
        tampered.write_bytes(bytes(blob))
        decoded_path = tampered
    result = load_batch(
        decoded_path,
        work / "gate_report.json",
        REGISTRY_DB,
        raw_path=work / "raw_products.csv",
        rejects_path=work / "rejects.jsonl",
        schema_report_path=work / "schema_report.json",
        manifest_path=work / "manifest.json",
        eval_report_path=work / "eval_report.json",
        gold_path=Path("seeds/gold_set.jsonl"),
        thresholds=thresholds,
        # The displayed comparison is exploratory. Contractual lineage is
        # replayed from the verified registry parent inside the loader.
        comparison_report_path=None,
    )
    if result["status"] == "loaded":
        st.success(f"loaded — {result['inserted']} new rows")
    elif result["status"] == "noop":
        st.info("noop — this batch_id is already registered; nothing written")
    else:
        st.error(f"refused — {result['reason']}")
    if result.get("lineage"):
        lineage = result["lineage"]
        st.write("Persisted lineage")
        render_table(
            [
                {
                    "parent": (lineage["parent_batch_id"] or "first batch")[:12],
                    "added": lineage["added"],
                    "removed": lineage["removed"],
                    "changed": lineage["changed"],
                    "unchanged": lineage["unchanged"],
                    "evidence": lineage["comparison_sha256"][:12],
                }
            ]
        )
        with st.expander("Full persisted lineage JSON"):
            st.json(lineage)
    if result.get("comparison"):
        verified_comparison = result["comparison"]
        st.write("Verified parent comparison")
        render_table(
            [
                {
                    "baseline": (verified_comparison["baseline_batch_id"] or "none")[:12],
                    "current": verified_comparison["current_batch_id"][:12],
                    **verified_comparison["counts"],
                }
            ]
        )
        with st.expander("Full verified comparison JSON"):
            st.json(verified_comparison)
    if result.get("reconciliation"):
        reconciliation = result["reconciliation"]
        st.write("Post-load reconciliation")
        if reconciliation["passed"]:
            st.success("RECONCILIATION: PASS — source evidence and registry agree.")
        else:
            st.error(f"RECONCILIATION: FAIL — {reconciliation['reason']}")
        render_table(
            [
                {
                    "expected": reconciliation["expected_rows"],
                    "verified": reconciliation["verified_rows"],
                    "missing": len(reconciliation["missing_row_ids"]),
                    "unexpected": len(reconciliation["unexpected_row_ids"]),
                    "payload changes": len(reconciliation["payload_mutation_row_ids"]),
                    "provenance changes": len(reconciliation["provenance_mutation_row_ids"]),
                }
            ]
        )
        with st.expander("Full reconciliation JSON"):
            st.json(reconciliation)
st.write("Batch registry")
render_table(read_registry(REGISTRY_DB) or [{"batch_id": "(empty)"}])

st.header("Row inspector")
gold = load_jsonl(Path("seeds/gold_set.jsonl"))
gold_by_id = {g["row_id"]: g for g in gold}
titles = {p.row_id: p.title for p in read_raw_csv(Path(workdir) / "raw_products.csv")}
all_ids = sorted(titles)
rid = st.selectbox(
    "Row (gold rows marked ●)",
    all_ids,
    key="inspect_row",
    format_func=lambda i: f"● {i}" if i in gold_by_id else i,
)
st.caption(f"raw title: {titles[rid]!r}")
pred = next((d for d in decoded if d["row_id"] == rid), None)
rej = next((r for r in rejects if r["row_id"] == rid), None)
g = gold_by_id.get(rid)
if g is not None:
    if g["expect"] == "decode" and pred is not None:
        match = (
            pred["category"] == g["category"]
            and pred["subcategory"] == g["subcategory"]
            and pred["is_winter_rated"] == g["is_winter_rated"]
        )
        (st.success if match else st.error)("gold: MATCH" if match else "gold: MISS")
    elif g["expect"] == "abstain":
        ok = pred is None
        (st.success if ok else st.error)(
            "gold: correct abstention" if ok else "gold: decoded but expected abstention"
        )
    else:
        st.error("gold: expected a decode but the row was abstained/quarantined (recall miss)")
i1, i2 = st.columns(2)
with i1:
    st.write("Pipeline outcome")
    st.json(pred or rej or {"missing": True})
with i2:
    st.write("Gold expectation")
    st.json(g or {"not_a_gold_row": True})
