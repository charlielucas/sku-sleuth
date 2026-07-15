# SKU Sleuth — Design

**Date:** 2026-07-10; portfolio-depth revision 2026-07-14
**Status:** Implemented and verified
**Scope:** Current design and evidence contract

## 1. Purpose

SKU Sleuth is an interactive demonstration of a production-style batch
classification workflow: decode messy product titles into a structured
taxonomy, evaluate the results against a curated gold set, apply declarative
quality gates, and load only gated batches into a local warehouse.

The interface is a **Streamlit "gate control room"**: the visitor picks a
batch, toggles between a guarded and a deliberately naive ruleset, drags gate
thresholds, and watches the evaluation metrics, gate verdict, and load
accept/refuse respond — with artifact hashes visibly binding each gate
approval to the exact bytes it evaluated.

Two tradeoffs are the thesis, and both are manipulable in the UI:

- **Precision vs coverage.** Every tier abstains rather than guesses, so
  coverage is honestly below 100% and the flag gate protects the expensive
  error class. Shipping less beats shipping wrong.
- **Measurement vs policy.** Evaluation measures; gates apply thresholds.
  In the app this is literal: measurement is computed once and cached, and
  moving a threshold slider re-judges the same report instantly without
  re-measuring anything.

The implementation stays intentionally local and inspectable: plain Python
stage functions under a thin app, SQLite evidence tables, and no service
infrastructure required to understand the workflow.

## 2. Goals, non-goals, and constraints

**Goals**

- A tested, deterministic Python engine (stdlib-only) under a thin
  interactive app layer (Streamlit is the only required app dependency).
- Hermetic tests and CI: no network, no API keys, deterministic.
- Honest metrics: the synthetic data contains seeded challenges the rules
  deliberately do not all handle, so coverage and precision are imperfect
  and the confusion pairs are real.
- Committed example artifacts from a scripted headless run, including a
  failing run — so a repo browser who never launches the app still sees the
  whole story.
- A concise README explaining the two tradeoffs in plain language.

**Non-goals**

- Real product data, real brand names, or a real downstream dashboard.
- Scale, streaming, scheduling, multi-user state, or deployment — this is a
  single-machine demo app.
- A required LLM. The model tier is pluggable; the real adapter is optional
  and never exercised by tests or CI.

**Portfolio distinctness (hard constraint).** The owner's existing public
repositories are predominantly small Python CLIs with CSV/Markdown outputs,
Makefiles, and near-identical README shapes; two of them already cover
classifier-with-review-routing and predicted-vs-expected label comparison.
This project therefore: (a) ships an interactive app, not a user-facing CLI —
the only command-line entry point is a headless script used by CI and for
regenerating committed examples; (b) uses no Makefile; (c) centers the parts
those repos lack: declarative batch gates, artifact-integrity hash chaining,
idempotent gated loads, abstention-first tiers with provenance, and a
curated gold set with adjudication notes; (d) writes its README to this
project's shape, not the house template.

**Data statement (appears once, in the README — not repeated as per-file
disclaimers):** every product, brand, SKU, and label in this repository is
synthetic and was generated for this project. Any resemblance to real
products, brands, or companies is coincidental. No third-party or
proprietary material is included.

**Authenticity constraints**

- The README describes what the project *is*, not a fictional operating
  history: no invented users, deployments, incidents, or accomplishments,
  and no metrics beyond what the committed runs actually produce. The
  seasonal-readiness consumer is explicitly labeled a fictional framing
  wherever it is mentioned.
- No badges, stock phrases, or boilerplate sections — README sections exist
  only where they serve this specific project.
- Tests exist only where they protect actual behavior (§13 is an enumerated
  behavior contract, not a coverage target).
- Commit history is the real build history: no fabricated commits, issues,
  releases, or activity.
- Repository visibility remains private until the owner explicitly chooses
  to publish it; this design does not imply a visibility change.

## 3. Domain

A synthetic catalog of automotive parts and workshop apparel.

**Taxonomy** (invented; 5 categories, 13 subcategories):

| Category   | Subcategories                              |
|------------|--------------------------------------------|
| Braking    | Brake Pads, Rotors, Calipers               |
| Filtration | Oil Filters, Air Filters, Cabin Filters    |
| Electrical | Spark Plugs, Ignition Coils                |
| Visibility | Wiper Blades, Headlight Bulbs              |
| Apparel    | Gloves, Coveralls, Hi-Vis Jackets          |

**Attributes** (all evaluated — see §8):

- `position`: front / rear / left / right (parts only)
- `pack_count`: integer from tokens like "(4-Pk)", "Set of 2"
- `size`: apparel sizes S–2XL (apparel only)
- `material`: e.g. ceramic, semi-metallic, leather, nitrile

**Flag:** `is_winter_rated` — a precision-critical boolean (winter wiper
blades, insulated gloves, cold-weather coveralls). The fictional framing:
downstream seasonal-readiness reporting combines this flag with category
coverage, and a false positive is the expensive error class. The pipeline is
therefore precision-first: every tier abstains rather than guesses, and the
flag gets its own gate.

**Brands:** ~12 invented brands in `seeds/brands.json`, each with 1–2 title
aliases (e.g. an all-caps form and an abbreviation). **Vetting is a required
implementation task:** web-search each candidate plus a trademark screen, and
reject any name that is within edit distance ~2 of, or an anagram of, a real
automotive, tool, outdoor-equipment, or apparel brand. Prefer clearly synthetic
constructions. The project name itself was screened the same way.

**SKU scheme:** invented — brand-specific three-letter prefix + 4 digits
(e.g. `VSK-4417`).

## 4. Synthetic data and seeded challenges

The generator (engine function; exposed via the headless script) writes
`data/raw_products.csv` (~1,200 rows for the default seed-42 batch):
`row_id, sku, title`. Titles come from per-subcategory templates plus noise:
inconsistent casing, abbreviations ("Frt", "Wntr", "HD"), stray tokens, and
occasional missing fields.

**Generator determinism contract:** seeded `random.Random` only; no set
iteration (sorted collections everywhere); no builtin `hash()`; stable
`row_id`s (zero-padded sequence, e.g. `R0001`); LF newlines. Same seed →
byte-identical CSV (double-run tested).

**Seeded challenges** — designed failure modes for rule-based classifiers,
listed in the README. Some are handled by the rules, some deliberately are
not, so the committed metrics are honest:

1. **Brand-default trap.** One brand's catalog is mostly winter-rated but
   includes non-winter items. A naive "this brand ⇒ winter" rule produces
   false positives. The shipped ruleset flags only on per-product evidence;
   the naive rule is selectable in the app (§11) and in the headless script.
2. **Abbreviation false friend.** "WNTR" (winter) vs "WNTY" (warranty) —
   token-level lookalikes the rules must distinguish.
3. **Token collision.** Pack-count tokens vs apparel sizes: in
   "4-Pk L Gloves", "L" is a size; in "Brk Pads Set of 4 L", "L" is a stray
   token. Expected to produce real confusion pairs.
4. **Out-of-catalog brands.** A slice of rows uses brands absent from
   `seeds/brands.json`; tiers abstain, keeping coverage honestly below 1.0.
5. **Corrupted titles.** A small share of rows carry junk or near-empty
   titles, unhandled by design → quarantine.

## 5. Gold set

`seeds/gold_set.jsonl` — **200 rows, including ~50 winter-rated positives.**
The positive count is chosen so the flag-precision gate is statistically
meaningful: with ~50 positives, of which ~40 decode as true positives
(token-suppressed challenge rows are recall misses), one false positive puts
precision ≈ 0.975 and two ≈ 0.952, so a 0.97 gate tolerates exactly one FP
and fails on two. This arithmetic is documented in `gates.toml` comments and
surfaced in the app's gate panel.

Each gold row carries: `row_id`, the raw title inline (so the gold set is
self-describing), and the full expected outcome — either a complete decode
(category, subcategory, attributes, flag) or an expected
abstention/quarantine.

`seeds/GOLD_SET.md` documents curation: inclusion criteria, the deliberately
ambiguous rows, and per-row adjudication notes for the tricky calls (e.g.
which reading of a colliding token was ruled correct, and why).

**Join contract:**

- Evaluation joins gold to the batch on `row_id`.
- A test asserts every gold `row_id` exists in the seed-42 generator output.
- A gold row found in neither the decoded set nor the quarantine set is an
  **evaluation error** (report fails loudly), never a silent drop.
- A gold row that was quarantined counts as a **recall miss**, not a skip.

## 6. Engine architecture

The engine is a plain Python package with no Streamlit imports — the app is
a consumer. Stages are functions over explicit inputs/outputs; each stage
also serializes its artifact so every run is inspectable on disk:

```
generate(seed, rows)         → raw_products.csv
schema(raw)                  → schema_report.json
decode(raw, config)          → decoded.jsonl + rejects.jsonl + manifest.json
evaluate(decoded, gold)      → eval_report.json (+ eval_report.md render)
gate(eval, manifest, thresholds) → gate_report.json
compare(A, B)                → exploratory comparison_report.json
load(bound bundle, db)       → verified-parent comparison + SQLite evidence
```

Interactive runs write under `runs/<temporary-workdir>/` (gitignored); the headless
script writes the committed `examples/`.

**Recipe identity, run identity, and integrity chain.** `decode` mints a
path- and ordering-independent recipe `batch_id` from canonical effective
inputs: raw records; decoder, raw-schema, and decoded-schema versions; the
ruleset and implementation; catalog and brand content; and model adapter,
model ID, fixture, and configuration. Implementation-source fingerprints
ensure a code change cannot silently reuse the recipe. Tests vary each input
and reorder/copy equivalent inputs.

The separate `run_id = sha256(batch_id + decoded hash + rejects hash)` names
one realized output. Deterministic runs share both IDs; a nondeterministic
model can share a recipe while producing a distinct run. The manifest binds
raw/schema/configuration to decoded and rejected bytes. Evaluation binds that
manifest and both artifacts to the gold bytes. The gate binds the evaluation
to threshold content. Load reads each file once, checks the complete chain,
and recomputes evaluation and policy from those same in-memory bytes before
inserting anything.

Display-only source names and absolute paths are absent from contractual
reports and hashes. Copying identical effective inputs to a differently named
path therefore preserves the schema report, manifest, recipe ID, and run ID.

These hashes are **unsigned consistency evidence**, not identity, approval,
or authorization. They detect stale, mixed, edited, and internally
inconsistent local bundles under a trusted-code assumption. They do not prove
who created a report. Production approval would require signed attestations,
protected signing keys, authenticated actors, and immutable artifact storage.

**Schema contract.** `row_id`, `sku`, and `title` are versioned required raw
fields. The schema report classifies drift as `none`, `additive`, or
`breaking`, lists deterministic per-field events, and distinguishes a missing
CSV value from an explicitly empty value. Additive columns remain visible
evidence but are ignored by this decoder. Missing columns, empty input,
duplicate/missing row IDs, and invalid UTF-8 fail before decoding.

**Row buckets** (disjoint; defined in the manifest):

- `decoded` — a tier produced a valid decode.
- `abstained` — input was well-formed but no tier produced a valid decode;
  written to `rejects.jsonl` with reason `no_confident_decode`.
- `quarantined` — input malformed (empty/garbled title, missing SKU);
  written to `rejects.jsonl` with a reason code.
- `errored` — an unhandled per-row exception was caught by the engine;
  counted, never crashes the batch.

`coverage = decoded ÷ total_raw` — abstained and quarantined rows stay in
the denominator, so neither abstention nor aggressive quarantining can game
coverage. Abstention and quarantine are separate buckets so the quarantine
ceiling measures input hygiene, not classifier confidence.

## 7. Tier engine

**Decode record:** `row_id, sku, category, subcategory, attributes{},
is_winter_rated, tier, evidence`. `evidence` is a short string naming the
rule/token/fixture that fired. There is **no numeric confidence** — the tier
plus evidence *is* the provenance; invented floats would be noise.

**Contract:** tiers implement `decode(product) -> Decode | None` and
self-censor — return `None` unless confident. The engine is a plain
first-non-`None` loop over `[catalog, rules, model]`. A decode is
all-or-nothing per row: no cross-tier field merging.

**Shared post-tier validation:** every tier's output passes one validator
(category/subcategory membership in the taxonomy, attribute domain checks).
An invalid decode is treated as an abstention and counted in the eval's
per-tier stats — model output is never trusted structurally.

- **Tier 1 — catalog:** normalized exact SKU match against
  `seeds/catalog.csv` (a small known-SKU list with authoritative decodes).
- **Tier 2 — rules:** the core. Deterministic token/regex extraction, brand
  alias resolution, keyword → subcategory maps with disambiguation, and
  evidence-based winter-flag logic (per-product tokens, never brand
  defaults). The naive brand-default variant is a selectable ruleset, not
  dead code.
- **Tier 3 — model:** a `ModelTier` interface with two implementations.
  - `StubModel` (default; the only one tests/CI use): a committed fixture
    mapping normalized titles → canned responses, simulating model answers
    for rows that lack rule-decodable tokens. The fixture deliberately
    includes a few wrong answers and one or two structurally invalid
    responses (exercising the validator and keeping tier-3 metrics honest).
    Abstains on anything not in the fixture. Fully deterministic.
  - `AnthropicModel` (optional): activates only by explicit selection with
    `ANTHROPIC_API_KEY` set; requires the `anthropic` extra. Prompt
    construction and response parsing are pure functions unit-tested against
    canned response fixtures; only the ~10-line HTTP call is untested. A
    malformed, out-of-taxonomy, or uncertain response → abstain, never
    guess. Excluded from every determinism claim.

## 8. Evaluation

`evaluate` first rejects duplicate IDs, decoded/reject overlap, artifact hash
or count mismatches, and incomplete joins. It then scores the bound batch and
reports:

- per-category precision / recall / F1;
- `is_winter_rated` precision and recall, reported separately and first;
- per-attribute exact-match accuracy with explicit null handling (correctly
  absent counts as a match; wrongly present or absent counts against);
- coverage (per §6) and abstention/quarantine/error counts;
- per-tier contribution: rows decoded, accuracy, and invalid-output count
  per tier;
- confusion pairs (predicted vs gold category/subcategory);
- the join-completeness check (§5).

Gold rows whose expected outcome is abstention count against precision if
they are decoded anyway, and as correct abstentions otherwise. Every gold row
with an expected decode enters category support and attribute denominators;
an abstention or quarantine is therefore an end-to-end category/attribute
miss and a flag recall miss where applicable. The report names both
end-to-end category accuracy and conditional-on-decoded category accuracy;
coverage remains a separate operational metric.

**Determinism contract (scoped):** running `evaluate` twice on the same
inputs in the same environment produces byte-identical
`eval_report.json` (tested). Mechanics: `json.dumps(sort_keys=True)`,
`newline="\n"` on all writers, no timestamps or absolute paths in report
bodies (run metadata lives in a separate, non-contractual `run_meta.json`),
and `.gitattributes` forces LF on `*.json`, `*.jsonl`, `*.md`. CI regenerates
both committed scenarios and byte-compares every deterministic example
artifact, including `run_meta.json`, so examples cannot silently go stale.

## 9. Gates

`gates.toml` — the **default gate profile**; the app's sliders initialize
from it, and `gate()` takes thresholds as a parameter so policy is data,
not code:

```toml
# Illustrative demo defaults — not derived from any production system.
# Sizing note: the gold set has ~50 flag positives, of which ~40 decode as
# true positives (token-suppressed challenge rows are recall misses); one
# false positive ≈ 0.975 precision, two ≈ 0.952 — 0.97 tolerates exactly
# one FP and fails on two.
flag_precision    = 0.97
category_accuracy = 0.93
coverage          = 0.80
error_rows        = 0      # hard zero: unhandled exceptions block the batch
quarantine_rate   = 0.10   # ceiling; quarantine is visible, not free
```

`gate` refuses a mixed evaluation/manifest pair, then produces
`gate_report.json`: each check with actual vs threshold, overall PASS/FAIL,
the recipe/run IDs, and a binding over raw, schema, manifest, decoded,
rejects, evaluation, gold, and threshold hashes.

**Why `gate` is not part of `evaluate`:** evaluation measures; gates apply
policy. Thresholds change without touching measurement code, and the same
eval report can be judged by different gate profiles — which is exactly what
the app's sliders do, live. The separation is stated in the README.

At load time each passing policy verdict is stored in `gate_decisions`, keyed
by the measured batch/run, evaluation, gate report, and threshold hashes.
`batch_evidence` contains measurement evidence only. Re-judging an existing
measurement under a different passing policy adds a decision record and
returns a verified no-op instead of raising a batch conflict.

## 10. Load

`load` writes SQLite (stdlib `sqlite3`):

- `products` — decoded rows, natural key `row_id`.
- `batches` / `batch_evidence` — recipe/run registry and bound artifact hashes.
- `gate_decisions` — independently keyed passing policy verdicts.
- `batch_lineage` / `batch_rows` — parent and complete expected product snapshot.
- `batch_outcomes` — complete decoded/rejected evidence used to replay
  comparisons from a registered parent.
- `load_reconciliation` — duplicate count/full-snapshot hash anchors and
  expected/verified state hashes.

Behavior:

- Reads raw, schema, manifest, decoded, rejects, gold, evaluation, gate, and
  optional comparison bytes once. It validates every nested report shape,
  counts, unique IDs, decoded taxonomy/attribute domains, replays evaluation
  and gate policy, and refuses malformed or mixed bundles with a structured
  result.
- Validates `manifest.schema` as the exact canonical projection of the
  independently recomputed `schema_report.json`; drift status, events, field
  evidence, counts, and effective-content hash cannot disagree.
- Binds every outcome back to the verified raw row map: decoded plus rejected
  IDs must exactly partition raw IDs, SKUs must match, reject titles must
  match, and every gold ID/title must identify the same raw row. Rebuilding
  downstream hashes cannot legitimize a stale or invented source join.
- Cross-checks top-level manifest counts/versions/ruleset against recipe
  identity and the recomputed raw-schema version.
- Starts `BEGIN IMMEDIATE` before registry state checks. Concurrent exact loads
  serialize to one transaction that inserts and subsequent verified no-ops.
  The same recipe ID with different run/artifact evidence remains a hard
  `batch_id_conflict`.
- Reconstructs the baseline from the registered parent's hash-verified
  `batch_outcomes`, recomputes the canonical comparison, and refuses an
  optional supplied report unless it matches exactly. Editing a comparison
  and recomputing its self-hash cannot change lineage.
- Recomputes full lineage counts, decision IDs/content, batch insert/hash
  summaries, run/incoming-row evidence, and reconciliation summaries on exact
  no-ops and read-only audits. Non-derivable timestamps remain informational.
- Requires the latest parent to reconcile before deriving a child snapshot,
  preventing an externally inserted product from being anchored by a later
  batch. Historical audits allow later append-only rows only when the latest
  batch's independently anchored snapshot contains the same payload and
  origin; unregistered rows remain unexpected for every batch.
- Incremental by natural key: a second batch inserts only new `row_id`s. An
  existing ID must have an identical payload; changed content is a hard
  conflict, never a silent `INSERT OR IGNORE`.
  Mechanism: batch 2 is a superset snapshot — the seed-42 sequence extended
  to 1,400 rows, so its first 1,200 rows are byte-identical to batch 1 (a
  test asserts the prefix property). Batch 2 therefore still contains every
  gold row (evaluation and gating work unchanged), gets its own `batch_id`,
  and the loader inserts only the new decoded `row_id`s. Removals in a later
  snapshot are recorded as lineage evidence but do not delete append-only
  history.

After insertion, reconciliation compares the complete expected union with
the database, including payload and origin-batch provenance, before commit.
The persisted `reconcile_batch` check later detects missing/deleted rows,
unexpected inserts, malformed JSON/boolean storage, payload mutations,
provenance mutations, and deleted or edited snapshot/outcome metadata. Its
expected row count and full snapshot hash are duplicated in measurement and
reconciliation anchors rather than trusted from mutable `batch_rows` alone.
The check opens SQLite read-only and never creates or migrates tables. An
exact reapply of a snapshot that reported removals still verifies against its
full persisted union snapshot. Foreign keys are enabled on every loader
connection, and `is_winter_rated` is constrained to integer 0 or 1. Fresh
registry DDL is atomic; a nonempty incompatible registry is refused before
its schema can be modified.

No watermark bookkeeping: batch registry + natural-key dedupe already gives
idempotency, and a mechanism with nothing real to bookmark would be
decoration.

## 11. The control room (Streamlit app)

Single-page app (`app.py`, launched with `streamlit run app.py`). The app
layer contains no classification, evaluation, or gating logic — it calls the
engine and renders its artifacts. Layout:

**Sidebar — the controls:**

- Batch selector: seed-42 (1,200 rows) / superset batch 2 (1,400 rows).
- Ruleset toggle: **guarded** (evidence-based flag) vs **naive**
  (brand-default flag) — the seeded trap made interactive.
- Model tier: StubModel (default) / AnthropicModel (only offered when a key
  is present; labeled as non-deterministic).
- Gate threshold sliders, initialized from `gates.toml`, with the one-FP
  granularity note beside the flag-precision slider.
- Reset-to-defaults.

**Main panels:**

1. **Batch overview** — bucket counts, coverage, schema-drift status, the
   canonical recipe, `batch_id`, `run_id`, and artifact hashes.
2. **Evaluation** — flag precision/recall headline, per-category table,
   per-attribute accuracy, per-tier contribution, and a confusion-pair
   table. Rendered from the cached eval report.
3. **Batch comparison** — partition counts, field/classification/tier/flag
   changes, and decoded↔reject transitions for the selected snapshot.
4. **Gate verdict** — one row per check: actual vs threshold, pass/fail
   chip, overall PASS/FAIL banner. Recomputed instantly on slider moves
   from the cached eval report — the measurement-vs-policy tradeoff made
   tactile.
5. **Load** — a load button, lineage/reconciliation evidence, the registry,
   and the refusal path:
   loading a gated-FAIL batch shows the structured refusal; a **"tamper
   with artifact" toggle** mutates one byte of `decoded.jsonl` before the
   load attempt, demonstrating the hash-mismatch refusal live. Re-loading
   an already-loaded batch shows the idempotent no-op; loading batch 2
   after batch 1 shows the incremental insert.
6. **Row inspector** — pick any row: raw title, decode result, tier,
   evidence string; for gold rows, the expected outcome and match/miss
   side by side.

**State and caching:** decode and evaluate results are cached per batch,
ruleset, and model plus hashes of catalog, brand, fixture, gold, engine, and
evaluation inputs. Changing an effective measurement input invalidates the
cache; moving thresholds does not and re-judges instantly. That asymmetry
*is* the measurement-vs-policy lesson. All app state is derivable from the
sidebar; there is no hidden session state to corrupt.

## 12. Headless run and committed examples

`scripts/run_pipeline.py` runs schema → generate/decode → evaluate → gate →
compare → load → reconcile for a named scenario. Success requires gate PASS,
load `loaded`/`noop`, and reconciliation PASS. `--baseline-dir` binds a prior
bundle into an exploratory deterministic comparison; successful loads also
write `lineage_comparison_report.json`, replayed from the registry's verified
parent. It exists for CI and committed artifact regeneration:

The runner recreates its scenario-local `products.db` on every invocation;
durable registry/idempotency behavior belongs to `load_batch` and its tests,
while this script must reproduce the same committed `loaded` transcript.

- `examples/passing_run/` — guarded ruleset: schema, manifest, comparison,
  eval report (json + md), gate report (PASS), lineage, load, and
  reconciliation summaries.
- `examples/failing_run/` — naive ruleset: the same chain ending in gate
  FAIL and a load refusal transcript. This is the project's thesis in one
  committed artifact: the gate stops a plausible-but-wrong change from
  reaching the warehouse.
- A short capture of the control room (screenshot or GIF) for the README,
  clearly labeled as the app rendering the committed example data.

## 13. Testing

Enumerated test contract (no count target — these must exist and be
discoverable by name):

- unit tests per tier (catalog normalization, each rule family, stub fixture
  behavior including the invalid-response path);
- engine: precedence order, all-or-nothing decode, validator applied to
  every tier, per-row exception capture → `errored` bucket;
- recipe identity: path/order independence, sensitivity to each effective
  input, and distinct run IDs for different nondeterministic output;
- schema: none/additive/breaking drift, missing-vs-empty evidence, duplicate
  IDs, empty input, and invalid UTF-8;
- generator: same seed → byte-identical output; all gold `row_id`s present
  in seed-42 output; batch-2 prefix property;
- evaluate: double-run byte-identity; join completeness (missing gold row →
  error; quarantined gold row → recall miss); attribute null handling;
- gate: threshold boundaries, including exactly-one-FP passes and two-FPs
  fail at `flag_precision = 0.97`; thresholds-as-parameter behavior;
- compare: union partition identities, strict type changes (`true` ≠ `1`),
  missing ≠ null, field deltas, and decoded↔reject transitions;
- load: exact no-op, full mixed-bundle refusal, same-recipe/different-run
  conflict, natural-key payload conflict, removed-snapshot reapply, concurrent
  exact-load serialization, independent passing-policy decisions, canonical
  parent-comparison replay, self-rehashed comparison forgery refusal, and
  delete/edit/payload/provenance/schema/metadata reconciliation mutations;
- trust-boundary probes: malformed top-level and nested reports, invalid
  decoded taxonomy, invalid SQLite boolean integers, malformed attribute JSON,
  foreign-key enforcement, and non-mutating read-only reconciliation;
- headless script end-to-end in a tmpdir (both scenarios; exit codes);
- **golden regression:** committed expected decodes for the gold set;
  any rule change that shifts outcomes fails with a readable per-row diff;
  `scripts/update_golden.py` refreshes it in one command;
- app smoke test via `streamlit.testing.v1.AppTest`: the app boots on the
  default batch, renders a PASS verdict, and flips to FAIL when the naive
  ruleset is selected (hermetic; no browser);
- `AnthropicModel`: prompt-construction and response-parsing tests against
  canned fixtures (no network).

All tests hermetic; `StubModel` only.

## 14. CI and tooling

- **Tooling:** Python 3.12, `uv`, `pyproject.toml` (src layout), `ruff`
  (lint + format), `pytest`. Engine: stdlib-only. App dependency:
  `streamlit`. Extras: `anthropic`. No Makefile.
- **GitHub Actions** (`.github/workflows/ci.yml`): ruff check + format check,
  pytest (engine + AppTest), then
  `scripts/run_pipeline.py` for the passing scenario (job fails if the gate
  fails) and the failing scenario (job asserts the expected nonzero exit),
  with reports uploaded as artifacts. Dependencies and every invocation use
  the frozen `uv.lock`; workflow permissions are read-only.
- **Local mirror:** `scripts/check.ps1` and `scripts/check.sh` run the same
  sequence.

## 15. Repository layout

```
sku-sleuth/
  app.py                     # Streamlit control room (thin; no engine logic)
  src/sku_sleuth/
    __init__.py  models.py  generate.py
    engine.py  schema.py  validate.py  evaluate.py  gates.py  compare.py  load.py
    tiers/  __init__.py  catalog.py  rules.py  model.py
  seeds/       brands.json  catalog.csv  gold_set.jsonl  GOLD_SET.md
               stub_model_fixture.json
  data/        raw_products.csv          (generated, committed)
  examples/    passing_run/  failing_run/  control_room.png
  runs/                                  (gitignored workspace)
  tests/
  scripts/     run_pipeline.py  update_golden.py  check.ps1  check.sh
  docs/        design.md
  README.md  LICENSE (MIT)  pyproject.toml  gates.toml
  .gitattributes  .gitignore  .github/workflows/ci.yml
```

## 16. README plan

Order matters — the first screen must hook a 10-minute reviewer:

1. **The control room capture** (screenshot/GIF of the gate verdict flipping
   when the naive ruleset is selected), immediately followed by one messy
   title next to its decoded JSON with tier + evidence.
2. Quickstart: `uv sync` + `streamlit run app.py` (plus the headless script
   for a no-UI run).
3. The two tradeoffs, in plain language (§1) — including the concrete app
   behavior that embodies each (abstention/coverage numbers; cached
   measurement vs instant policy).
4. What the gate chain actually prevents, told via the committed failing
   run and the tamper demo.
5. Limits, stated before a reviewer finds them: synthetic self-generated
   data (the rules' author also wrote the generator — mitigated by the
   seeded unhandled challenges and honest metrics), illustrative gate
   thresholds, stub model by default, known-failures list.
6. The data statement (§2) and license.

No badges; no sections that don't serve this project.

## 17. Decision log

- **Interactive control room** over the house CLI-pipeline shape: the
  owner's existing public repos are predominantly Python CLIs with CSV
  outputs, and two already cover classifier/eval territory. The reshape
  keeps the novel content (gates, hash chain, gated loads, abstention,
  gold-set curation) and changes the interaction model. Streamlit chosen
  over FastAPI (weaker visual story) and an event-driven queue (infra
  theater at this size).
- **Engine/app split:** all logic in a stdlib-only package; the app renders
  artifacts and never computes them. Keeps tests hermetic and the app thin.
- **Optional LLM adapter** over none/required: shows the seam without
  breaking hermetic CI.
- **Fixture-driven StubModel** over an abstain-only stub: exercises the
  tier-3 code path and output validation, with deliberate imperfection so
  CI metrics stay honest. Builtin `hash()` banned (per-process salting).
- **No numeric confidence** on decodes: tier + evidence is the provenance;
  invented floats invite the question "where does 0.87 come from?".
- **Canonical recipe ID + realized run ID:** configuration equivalence is
  path/order independent, while nondeterministic output remains distinct.
- **Unsigned hash chain + replay at load:** catches stale/mixed/edited local
  evidence without overclaiming authentication; surfaced via tamper demo.
- **Append-only conflict detection + reconciliation:** natural-key changes are
  explicit failures, lineage is replayed from verified parent outcomes, and
  stored state is re-verifiable against independent snapshot anchors.
- **Thresholds as data** (`gates.toml` default profile + slider overrides):
  the same eval report judged by different policies, live.
- **Batch registry, no watermark:** nothing real to bookmark in a
  full-snapshot demo; idempotency comes from keys and the registry.
- **Gold set 200 rows / ~50 positives:** sized so the flag gate can
  actually bind (one-FP arithmetic in §5).
- **Name and brands vetted** against real-company collisions; the original
  working name was dropped after a collision screen, and four brand
  candidates were replaced from the spares list.
