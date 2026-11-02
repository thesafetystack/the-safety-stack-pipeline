# technique_staged eval set 2026-10-17b: 2026-10-17a with the blind gold audit applied

A gold revision of `2026-10-17a` (techpipe D610). Items, splits and texts are 17a's, byte for byte (`items.jsonl`
and `texts/` are copies; the same `items_sha256`, so a run made on 17a can be scored here). Only `gold.jsonl` and
`manifest.json` differ. 17a stays frozen and is still the set the earlier results were reported on; report both
until one is retired. Provenance of the 17a gold: `../2026-10-17a/README.md`, `../2026-10-17a/adjudication/`.

## The audit (`adjudication/audit/`)
A blind audit of 90 (item, slug) pairs on 2026-10-17, made after the first round of runs on 17a. Two Codex judges
(gpt-6-sol, gpt-6-astra); each document went to the judge that had not been 17a's labeller B on it (sol 41 items,
astra 49). Judges saw `GUIDE.md`, `PROMPT.md`, their `ITEMS-<judge>.md` and the texts. They did not see the pool,
the gold quote or confidence, or which arms predicted the slug. The question was presence: does the document involve the
technique, in any role, in its body. Answers: `yes`, `borderline`, `no` with a verbatim quote (`out-<judge>.txt`).
Pools, sampled with seed 20261017 from 11 runs on 17a (slug presence per item over all repeats, group nodes and
`candidate:` rows excluded; `key.jsonl` holds pool, item, slug, judge, 17a confidence and quote, and the predicting
arms per id):

| pool | what | sampled | yes | borderline | no |
|---|---|---|---|---|---|
| A | 17a gold rows no run predicted (all 30) | 30 | 17 | 8 | 5 |
| B-staged | false positives of staged gpt-6-luna, prompt set v1 (79) | 30 | 13 | 5 | 12 |
| B-single | false positives of single gpt-6-luna not already sampled (24) | 15 | 10 | 2 | 3 |
| C | controls: gold rows every run predicted in every repeat (110) | 15 | 15 | 0 | 0 |

## Derivation (`scripts/derive.py`, reproducible; `scripts/arm_roles.py` once)
- Pool A: `no` -> row removed (5); `yes` -> unchanged (17); `borderline` -> row kept with `dont_care: true` (8).
- Pools B: `yes` -> row added (23); `borderline` -> row added with `dont_care: true` (7); `no` -> nothing (15
  stay false positives). An added row has `slug`, the judge's `quote`, `confidence: med`, `sources: ["audit"]`,
  `source: blind-audit-2026-10-17`, an `audit` record and a `note` with the judge's reason. Its `roles` are the
  audited arm's modal roles over its repeats (`adjudication/audit/arm-roles.jsonl`, extracted from the two runs by
  `scripts/arm_roles.py`). The arm's whole roles dict is copied, not a single role: every predicted row carries
  `mentioned` at the row's top strength beside its specific roles, so "the most common role" alone would always be
  `mentioned`. The `{"used": 1}` fallback was not needed.
- Pool C: no change. Every changed item lists its changes in `audit_actions`.
- All 30 judge quotes of added rows are found verbatim (whitespace-normalised) in the texts.
- `uv run --no-sync python evals/technique_staged/2026-10-17b/scripts/derive.py` checks that the files match byte for
  byte; `--write` rebuilds them.

## Counts
431 rows = 406 - 5 + 30: 8 `candidate:` rows, 15 `dont_care` rows, 20 group-node rows, **388 scored rows** (378 in
17a), **147 high-confidence** scored rows (unchanged: the removed and don't-care pool-A rows were low or med; added rows
are med), 277 scored rows given by both labellers. Added rows are in no `both` variant (their source is the audit).

## Scoring (`dont_care`, D611)
A `dont_care` row is in no gold variant, so it leaves every recall denominator. A predicted slug that matches it on
that document is neither a true nor a false positive, under every variant, for slugs and cells alike. Score a 17a
run here with `--set-dir evals/technique_staged/2026-10-17b` on `stack eval score` (add
`--summary summary-2026-10-17b` to keep the run's 17a `summary.json`), `staged-compare` and `staged-table`. The run's
`set_items_digest` must match this `items.jsonl`.

## Known bias
Only false positives of the two audited arms could be credited: staged gpt-6-luna with prompt set
technique-staged-v1 and single gpt-6-luna. On 17b those arms gain precision relative to any other arm, whose distinct
false positives were never audited. That includes staged v2, which shares most but not all of v1's predictions. Pool
A removals and `dont_care` rows apply to every arm alike. The audit is
also LLM-only (no human pass), and the judges' `borderline` share is high (15 of 90).
