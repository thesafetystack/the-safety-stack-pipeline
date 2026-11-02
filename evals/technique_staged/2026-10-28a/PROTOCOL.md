# Test-set confirmation of effort high for technique_staged: frozen protocol (2026-10-28)

Frozen and committed before any label of the test set was read (the judges' `labels-*.jsonl`, `scores.*` and
`disagreements.jsonl` of `evals/judge-2026-10-25j/technique_staged`). Machine-readable part: `protocol.json` (items
with text digests, arms with pins, the rule's thresholds, the bootstrap). This file is not edited after the commit
that adds it; anything that turns out to need changing is recorded as a deviation in `README.md` beside the results.

## Why
The 2026-10-27 technique round (D730) compared extraction arms on gold v3 (dev 60 / validation 40) against an
adjudicated reference (ref-v3x) and found effort high (role `extract_staged_openai_high`, production prompt and
gleaning unchanged) the best cost / gain trade-off: presence F1 +.033 [+.015, +.055] on validation against the
failure-free baseline. The held-out test set was never run. This is the confirmation step; whatever it shows,
production stays on the current setup (decided 2026-10-28): the result informs a later rollout decision only.

## Items
The 100 items of `evals/judge-2026-10-25j/technique_staged` (`jtec-0001` .. `jtec-0100`: a self-weighting sample of
build ws1qpn8myzxah's luna technique rows; preprint 49, forum_post 32, paper 14, lab_blog 5), frozen as set
`technique_staged/2026-10-28a` (`scripts/freeze.py`; texts = the store blobs of the judge items' `text_sha256`, the
texts production and the judges read). `protocol.json` `items` lists every item id, doc id and text sha256.

## Arms
| arm | what | replicates | role in the rule |
|---|---|---|---|
| PROD | the production rows already annotated and evaluated (`production.jsonl` of the judge set: prompt set `technique-staged-v5-a1.1`, role `extract_staged_openai`, single mode + gleaning, doc_cap 900,000, taxonomy 1.0.0) | 1 | baseline |
| HALL | the same pin with role `extract_staged_openai_high` (reasoning effort high), `stack eval technique_staged` on 2026-10-28a | 2 | candidate |
| BASEF | the same pin as PROD, run fresh by the same harness as HALL | 1 | secondary only (harness reproduction, matched latency and cost) |

Every pin key other than `role` is the production pin of plan `batch-2026-10a`. 64 workers. A row that fails is
retried by re-running the same command once (the response cache serves the rows that succeeded); a row still failed
counts as an empty prediction, and the counts are reported. PROD's rows are all `ok` or `empty` (the judge set was
sampled from those).

## Metric
Slug presence per document: a slug of taxonomy 1.0.0 with at least one role of strength >= 1, group nodes out (the
gold-v3 strict unit; judges' slugs that are not in the taxonomy are dropped and counted). Micro P / R / F1 over the
100 documents; HALL's two replicates pooled (per-document counts averaged over replicates). Against a reference with
`dont_care` units, a `dont_care` slug is removed from both the prediction and the reference of its document.

## References
1. **Per judge**: the slug set of each blind judge (Opus 5.5, Sonnet 5.5, Sol 6.1; released labels, status valid /
   repaired / over_length), and the **majority** (slugs >= 2 of the 3 judges). No adjudication; reported for every
   arm.
2. **ref-t (primary, adjudicated)**, built as gold v3's ref-v3x (the gold-v3 technique round), with three judges in place of six runs:
   - Universe: every (item, slug) unit that any judge or any arm observation (PROD, HALL r1, HALL r2, BASEF) gives.
     Judge support = how many of the three judges give it (0-3).
   - Candidates for adjudication: every unit with judge support 1 or 2; every arm-only unit (support 0, the pooled
     arm extras, all arms including PROD); and a calibration sample of one support-3 unit per document that has
     other candidates (seeded, 20261028). Support-3 units outside the sample are `in`.
   - Packets as 114's: per document the text as the judges saw it (`text.txt`, long lines broken at a space) and
     `meta.md`; candidates shuffled; per candidate the entry (slug, name, definition, aliases, named instances,
     parent) and up to 4 pointer quotes pooled from every source that gave the unit (judges and arms alike),
     deduplicated, unattributed, shuffled. Nothing about support, sources or arms enters a packet.
   - Two blind adjudicators of different families: Claude Opus 5.5 (Claude Code subagents, one session copy per
     batch) and GPT Sol 6.1 (codex exec, read-only, effort high), 114's guide v2
     (`../2026-10-23a/adjudication/GUIDE.md`, unchanged), rubric `rubric-v0.9-c`, the judges' catalogue, 114's
     prompts; batches of at most 4 documents / 250,000 characters. Both `present` -> `in`; both `absent` -> `out`;
     both `borderline` -> `dont_care`; any split -> arbitration.
   - Arbitration (114's `ARBITRATION.md`, unchanged): two arbiters (Sol 6.1 via codex; Fable as a Claude Code
     subagent) see both readings anonymised in random order. Both `present` -> `in`, both `absent` -> `out`,
     otherwise `dont_care`.
   - If an adjudicator or arbiter family cannot finish (usage limits), ref-t is not built and the rule is read on the
     majority reference instead, marked as that fallback.

## Decision rule (binding)
On ref-t, HALL **confirms** iff all three hold (point estimates):
- presence ΔF1 = F1(HALL) - F1(PROD) >= +0.020;
- precision loss <= 0.030, i.e. ΔP = P(HALL) - P(PROD) >= -0.030;
- HALL's median per-document latency < 180 s (each observation's `standalone_latency_ms`: the document's calls as
  made, both turns; median over the 200 observations).

Reported beside the verdict, not part of it (pre-registered readings):
- 95 % intervals: paired document bootstrap within stratum (doc_type), 2,000 resamples, seed 20261028, percentile;
  for ΔF1, ΔP, ΔR.
- "Does not confirm" is called **refuted** when ΔF1's interval upper bound is < +0.020, or ΔP < -0.030, or the median
  latency >= 180 s; otherwise **inconclusive**.
- "Confirms" is called **robust** when ΔF1 > 0 against each of the three judges and the majority, and the adversarial
  `dont_care` assignment (each `dont_care` unit labelled against HALL: `out` when HALL predicts it at least as often
  as PROD, else `in`) keeps ΔF1 > 0.
- Sensitivities on ref-t: `dont_care` as `in` / as `out`; single-adjudicator references (support-3 units + that
  adjudicator's present verdicts); per HALL replicate; leave-one-document-out (largest change of ΔF1); by stratum
  and length band (<20k, 20-60k, 60-100k, >100k characters); HALL vs BASEF and BASEF vs PROD.
- Cost: settled USD per run and standalone USD per 1,000 documents per arm (PROD's production cost is not in the
  judge set: BASEF stands in for it); latency p50 / p95 for HALL and BASEF; failures, truncations.

## Spend
Cap $15 of API spend. Ledger ceilings: HALL `--max-usd 9`, BASEF `--max-usd 4.5` (hard: a run stops at its ceiling).
Expected from 114's measured rates ($9.5-11 / 1k documents at effort high, $6-7 at default; this set's median
length 57k characters vs gold v3's 52k): HALL about $2.2, BASEF about $0.8. Adjudication and arbitration run on
subscription agents (Claude Code subagents, codex), not on the API, as in 114; their volume (calls, tokens where
reported) is recorded separately.
