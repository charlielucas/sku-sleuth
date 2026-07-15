# Gold set curation

`seeds/gold_set.jsonl` is the measurement standard the whole pipeline is scored
against. It is **200 rows** sampled from the frozen seed-42 batch (1,200 rows),
hand-reviewed row by row, and verified against the real guarded pipeline
(`CatalogTier` -> `RulesTier("guarded")` -> `StubModel`) so the numbers below are
observed, not assumed.

## Inclusion criteria

- 200 rows sampled from the seed-42 batch (1,200 rows), stratified to include:
  every-day rows, 50 winter-rated positives (sized so one flag false positive
  approx 0.975 precision -- see the sizing note in `gates.toml`), trap-brand
  non-winter rows, WNTY warranty-token rows, stray-`L` apparel rows, and rows
  expected to abstain (malformed input, out-of-catalog brands not covered by the
  model fixture).
- Every row was reviewed by hand against its title **as written**; labels are not
  blind copies of generator truth. Where the generator truth reads wrong for the
  title (there were none: the two stray-`L` rows already omit `size` in truth),
  the label would be corrected here; where the *shipped decoder* reads the title
  wrong (the stray-`L` rows), the gold keeps the correct label so the eval
  surfaces the designed miss.
- The two Task-10 fixture flag-flips are both included as `expect: "decode"` with
  **truth** labels, so the guarded-flag precision/recall story is grounded in real
  traceable rows (R0466, R0498) rather than a suspicious perfect score.

### Composition (verified against the guarded pipeline)

| Stratum | Count | Notes |
|---|---:|---|
| Total rows | 200 | unique `row_id`, all present in seed-42 x 1200 with matching title |
| `expect: "decode"` | 182 | full expected decode (category/subcategory/attributes/flag) |
| `expect: "abstain"` | 18 | 12 malformed (quarantined) + 4 out-of-catalog-not-in-fixture (abstained) + 2 structurally-invalid Exhaust fixture rows (abstained) |
| `is_winter_rated: true` | 50 | of which 39 decode as true positives, 11 are recall misses |
| token-suppressed NORVIKKA winter | 10 | capped at 10 of the 50 positives; all recall misses under guarded rules |
| trap-brand (`NVK-`) non-winter, `expect: decode` | 14 | guarded withholds the flag; naive would brand-default to a FP |
| titles containing `WNTY` | 17 | warranty token, never winter evidence |
| Apparel rows | 36 | includes both stray-`L` rows |
| rows carrying a `"note"` adjudication | 12 | listed below |

### Flag metrics on the guarded reference pipeline

- true positives = 39, false positives = **1** (only R0498), recall misses = 11.
- **flag precision = 0.9750** (>= 0.97 gate; a second FP would drop it to approx 0.952 and fail).
- flag recall = 0.7800 (not gated; the token-suppression trap is designed to cost recall).
- category accuracy = 0.9835 (179/182; the 3 misses are the deliberately
  mislabeled fixture rows R0134 / R0335 / R0240) -- comfortably above the 0.93 gate.

## Expected outcomes

- `expect: "decode"` rows carry the full expected decode (`category`,
  `subcategory`, `attributes`, `is_winter_rated`).
- `expect: "abstain"` rows must end in the abstained or quarantined bucket;
  decoding them counts against precision. All 18 were confirmed to abstain or
  quarantine in the guarded pipeline (zero were decoded).
- A gold row found in neither the decoded set nor the quarantine set is an
  evaluation error, never a silent drop (see design section 5). A gold row that
  was quarantined counts as a recall miss.

## Adjudication notes

One bullet per non-obvious ruling; the `row_id` values are real and each carries
the same ruling inline in its `"note"` field.

- **R0466** (`AXKORT WNTR COVERALL`): the Task-10 fixture flipped this to
  `is_winter_rated=false`, but the title's `WNTR` token and the generator truth
  are winter=true. Gold keeps the **truth (true)**; the model tier decodes it
  winter=false, making this the deliberate **recall miss** (false negative) in the
  flag story.
- **R0498** (`belvand rear brake pads 2-pk vsk-6878`): the fixture flipped this to
  `is_winter_rated=true`; truth is winter=false. Gold keeps the **truth (false)**.
  The model decodes it winter=true, so this is the **single tolerated flag false
  positive** that puts precision at approx 0.975. No other gold row may produce a
  winter FP under the guarded ruleset, or the passing scenario fails its own gate.
- **R0481** (`BRUMHALT COTTON WNTR COVERALLS L`): the trailing bare `L` is a crate
  code, not a size. Generator truth carries no `size`, so gold omits
  `attributes.size`. The shipped rules read the `L` as `size="L"` -- a designed,
  documented **attribute miss** the eval must surface. `WNTR` is genuine winter
  evidence, so `is_winter_rated` stays true.
- **R0690** (`brh hi-vis jacket l`): the trailing bare `l` is likewise a crate
  code, not a size; gold omits `attributes.size` (rules will wrongly read
  `size="L"`). No winter token, so `is_winter_rated` stays false.
- **R0003** (`... 2YR WNTY` / `WNTY 90D`): `WNTY` is a warranty token, not winter
  evidence. It does not match the winter token set (`WINTER`/`WNTR`/`INSULATED`/
  `THERMAL`), so `is_winter_rated` stays false and no winter FP is produced -- the
  reason 17 `WNTY` rows can sit safely in the set.
- **R0021** (trap-brand `NVK-` non-winter): the guarded ruleset correctly
  withholds the winter flag; the **naive** ruleset would brand-default NORVIKKA to
  winter=true (a false positive). Task 7 excludes all trap-brand SKUs from the
  catalog, so no catalog hit can shield the row -- the guarded-vs-naive precision
  gap is real, not masked.
- **R0013** (NORVIKKA winter-bias, token suppressed): winter truth with no winter
  token in the title. The evidence-based guarded ruleset cannot see the flag, so
  it is a recall miss -- the brand-bias trap surfaces through **recall**, never a
  brand-default guess. Capped at 10 such rows so true positives stay >= 33.
- **R0134** (`AXKORT ROTORS ...`): the fixture deliberately mislabels this as
  Filtration/Air Filters; truth is Braking/Rotors. Included as `expect: "decode"`
  with **truth** labels so the eval records an honest **category miss** from the
  model tier (not hidden by dropping the row).
- **R0335** (`AXKORT SPARK PLUG`): fixture mislabels as Braking/Calipers; truth is
  Electrical/Spark Plugs. Kept with truth labels -- an honest category miss.
- **R0240** (`CRUMANE IGN COIL`): fixture mislabels as Visibility/Wiper Blades;
  truth is Electrical/Ignition Coils. Kept with truth labels -- an honest category
  miss.
- **R0419** (`BELVAND HDLGHT BULB SET OF 2 NEW`): the fixture maps this
  out-of-catalog row to `category="Exhaust"` (outside the taxonomy). The validator
  rejects it and the pipeline abstains, so gold keeps `expect: "abstain"` -- a
  decode expectation could never be satisfied.
- **R0022** (`CRUMANE ROTOR`): fixture maps this to `Exhaust/Mufflers`
  (structurally invalid); validator rejects, pipeline abstains, gold stays
  `expect: "abstain"`.

### Rows deliberately left out / choices made

- The 3 wrong-category fixture rows (R0134, R0335, R0240) were **included** as
  expect-decode-with-truth so the model tier's honest category misses are
  measured. They cost 3 of the 182 category comparisons (accuracy 0.9835).
- Only two stray-`L` rows exist in the batch (R0481, R0690); both are included.
- Abstains are topped up to 18 (margin above the >= 15 floor) using malformed rows
  (never covered by the fixture) plus a few out-of-catalog-brand rows whose titles
  the fixture does not contain, so the abstain bucket exercises both the
  quarantine path (malformed) and the all-tiers-abstain path (unknown brand).
