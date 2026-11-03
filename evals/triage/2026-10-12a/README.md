# Triage eval set `2026-10-12a`

Superseded as a measure: production triage (`openai/gpt-6-luna`, `fetch_on: [relevant, borderline]`; prompt
`safety-relevance-v3`, since D656/D663 its revision `safety-relevance-v4`) was chosen on this set (D201); production
triage output is now measured by `evals/judge-2026-10-25j/triage` and `triage_w`.

Task: the triage gate's LLM question (`workflows/triage/ask.py`): from a catalogue record (title, authors, abstract,
in-band excerpt) decide `relevant | borderline | not_relevant`. Frozen 2026-10-12 from a development store (1,806
documents) by `python -m stack.evals.freeze triage` (seed `track-e-2026-10-12a`; `manifest.json` has the code
digests).

## Items (`items.jsonl`, 241)
`items.jsonl` is not in the repository (long third-party excerpts, D795): `tools/fetch_eval_texts.py` fetches it
into the eval-text cache, checked against `evals/archived.sha256` and the manifest.
- `input.record` is the exact user message production sent at freeze time (`workflows.triage.ask.document_text`
  over the gate view row); `input_digest` = sha256(record). `stack eval` recomputes it and refuses the set if
  production changed.
- Items 1-200: paper 100 (openalex 45, arxiv 35, semantic_scholar 20), forum 40 (lesswrong), url 60 (six lab blogs).
  Within each channel about half `kw_high` / `kw_low` on a safety-keyword proxy (106 / 94); 39 a-priori hard cases
  (capabilities 14, policy 12, governance 8, safety_marketing 5). The sample is stratified, not representative.
  The 60 url items are **title-only probes** (`production_asks` false: production fetches url documents without
  asking). Licence: open 120, public_web 60, restricted 20. Split 50/50 inside each stratum: `dev` 100, `test` 100.
- Items `tri-0201` … `tri-0241` (amendment, 2026-10-12): 41 Shallow-Review-heavy items appended by
  `python -m stack.evals.sr append --task triage` (items 1-200 byte-identical; `manifest.json` `amendments[0]` has
  the hashes before and after and, per item, the query, topic and selection intent: 27 likely in, 14 hard
  negatives). Split 21 dev / 20 test. Items of `evals/triage/sr2025-recall` are excluded.

## Labels and gold
- **Gold v1** (`gold.jsonl`, items 1-200; rubric `evals/rubrics/triage.md`): Opus and Codex `gpt-6-sol`
  independently, from the rubric and `{item_id, record}`; the 35 disagreements to an Opus adjudicator with the
  labellers anonymised (`labels/adjudication.jsonl`; gold `contested: true`). Agreement 165 / 200, κ 0.705; fetch
  (verdict ≠ `not_relevant`) 184 / 200, κ 0.836. Disagreement is almost entirely about `borderline`. Caveat: the
  adjudicator sided with Opus 31 of 35 times.
- **Gold v2** (`gold-v2.jsonl`, all 241; rubric `evals/rubrics/triage-v2.md`: scope = Stack ∪ Shallow Review, from
  `docs/scope/shallow-review.md`, plus a `scope` label `core` / `sr_only` / `out` and calibration rules). Relabelled
  166 items (the v1 `not_relevant` / `borderline` items, a seeded 15 % control of v1 `relevant`, the 41 appended
  items) by Opus + Codex, 38 disagreements to an Opus adjudicator; 75 v1 `relevant` items carried
  (`carried_from: v1`, scope null). The maintainer's labels on 20 items (`labels-v2/tomas-part1.jsonl`) are the
  reference where present. Verdict agreement 134 / 166, κ 0.677; scope κ 0.711. Base rates: relevant 132,
  borderline 24, not_relevant 85.

## Results (2026-10-12)
Primary metric: recall of gold fetch under the gate's `fetch_on`; cost = settled USD per 1,000 records.
- **Gold v1:** `openai/gpt-6-luna` beat the then incumbent `claude-haiku-4.5` (McNemar p = 0.015 on dev). A prompt
  written from dev errors only, `safety-relevance-v2`, raised luna's test fetch correctness 79 → 90 / 100
  (p = 0.003). Stronger models (claude-sonnet-5.5, gemini-3.8-flash, luna-pro) agree more with the 3-way label but
  park more gold-fetch records in `borderline`, so they do not beat luna, at 15-25× its cost; votes add nothing.
  With `fetch_on: [relevant, borderline]` luna + v2 reached recall 95.2 % at precision 96.8 % on test, ≈ $0.15 per
  1,000 records. A title-only question is not good enough to replace "fetch every url document".
- **Gold v2:** `safety-relevance-v3` on luna: recall under `[relevant, borderline]` dev 100 % / test 97.5 %, test
  precision 97.1 %, ≈ $0.15 per 1,000; v3 widens without costing precision, its gain concentrated on `sr_only` items
  (luna test 11 → 15 / 15). The Shallow Review recall set is `evals/triage/sr2025-recall`.
