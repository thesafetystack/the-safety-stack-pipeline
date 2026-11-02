# Triage gate set `2026-10-21-p4gate` (promotion gate of the P4 technique-provenance clause, scope R2 D1)

**Use:** the independent evaluation that decides whether `safety-relevance-v4-p4` / `screen-v13-p4` (the S-only
technique-provenance clause) replace `safety-relevance-v4` / `screen-v13` in the next plan revision. All items are split
`test`. Nothing is tuned on this set. The screen half is `evals/screen/2026-10-21-p4gate/`. **Result: KEEP OFF**
(below).

## Items (`items.jsonl`, 92)
- **Frozen** 2026-10-21 from a read-only mirror of the production store (documents head `r1sh6c9jm6de2`, series
  `batch-import`) at code `c692e0c`. `manifest.json` records the git state; other agents' uncommitted files made it dirty.
  The record is `stack.evals.freeze.triage_cands`'s, i.e. `document_text`, record version 1. Each item also carries
  `input.published_at` and `input.channels`, the two fields record version 2 adds (see "Harness limits").
- **Unused records only.** Excluded: every document (by doc id, normalised title or arXiv id) in the round-2 scope
  review's C13 sets, its audit samples and landmark lists, and every set under `evals/`. Also excluded: 43 documents whose
  in-band excerpt blob was missing from the mirror, since their record could not be rebuilt as production saw it.
- **Seeded draw** (seed `d1-p4gate-2026-10-21`, order sha256(seed + doc_id); papers only, years 2015-2026, rows
  production actually triaged). The pools:
  - L, lineage pool (48): title/abstract matches the catalogued-technique vocabulary (adversarial examples/training,
    certified robustness, DP training, membership inference, extraction, poisoning/backdoors, unlearning, influence/data
    attribution, saliency/attribution, probing, concept methods, representation similarity, feature visualisation, model
    cards/datasheets), with no application domain. Per era (2015-2019, 2020-2023): 16 production skips + 8 fetches. The
    skips are enriched on purpose, since a skip is where the clause can act.
  - A, domain-application pool (14): a lineage/XAI/privacy term next to an application domain (medical, wireless,
    traffic, finance, …).
  - N, negative controls (14): no lineage or application term, before 2024.
  - C, modern controls (20): 2024-2026. Lineage-term vs other × production fetch vs skip, 5 each.
- Four title-basis records (`tri-p4-0003`, `-0023`, `-0088`, `-0091`) were labelled but left out. The harness rebuilds
  every triage record with `document_text` and has no title-basis path, so it cannot send `title_text`. The title
  variants `safety-relevance-title-v2(-p4)` are **not evaluated** here.

## Labels and gold
- Two independent labellers on the same blind packet: Codex `gpt-6-astra` (reasoning high, read-only, packet only) and
  an Opus sub-agent (packet only). The packet held the production input, the year, the rubrics `evals/rubrics/triage-v2.md`
  / `screen-v2.md`, and the criteria written before labelling (`labels-p4/criteria.md`: the two readings, explicit
  lineage criteria, domain-application and homonym exclusions, weak boundary cases). Per item, each labeller gave the
  as-written verdict (rubric + the adopted P3 narrow-setting clause), the P4-scope verdict, lineage yes/no with its
  kind, weak, confidence and a reason. Raw outputs: `labels-p4/codex-gpt-6-astra.jsonl`, `labels-p4/opus.jsonl` (keyed
  `id`, not the `labels/` format of `stack.evals.gold`).
- **Agreement (96 triage items):** P4 fetch 89/96 (κ 0.85), lineage 93/96 (κ 0.94), as-written fetch 82/96 (κ 0.68),
  P4 3-way 79/96.
- **Adjudication:** every disagreement on lineage or on a binary fetch under either reading, 17 items, was decided
  with a written reason (`labels-p4/adjudication.jsonl`; `contested: true` in the gold). A relevant-vs-borderline
  difference with the same fetch resolves to `borderline`.
- `gold.jsonl` = the adjudicated **P4-scope** reading, the gate's gold and the harness default. `gold-as-written.jsonl` =
  rubric + P3 without P4. `label.gate_stratum` comes from pool and gold: lineage_positive 31 (2015-19: 12, 2020-23: 19),
  expected_drop 26, negative_control 12, modern_control 19, ordinary_relevant 4. `label.lineage`, `lineage_kind` and
  `weak` are set per item.

## Thresholds (fixed before sampling and labelling)
PROMOTE only if all of these hold on both gates. Each variant ran twice (`--repeat 2`). Noise = the share of a stratum's
items whose keep flips between a variant's two runs, max over the two variants. A difference within the noise counts as
no difference.
- (i) Lineage positives: keep(p4) - keep(base) ≥ 20 pp.
- (ii) Modern controls: no loss of agreement beyond noise, and no wrong-way flip (both base runs right, both p4 runs
  wrong).
- (iii) Negative controls and expected drops, each: false keeps(p4) ≤ base + 10 % of the stratum.
- (iv) Screen R agreement: no net regression.
- Minimum 12 lineage positives and 10 per other gated stratum, else INCONCLUSIVE (→ KEEP OFF).

## Runs 2026-10-21 (openai/gpt-6-luna, the plan's model; not in the repo)
Two arms with identical items and prompts:
- **lines**: production inputs, record version 2 with `Published:`/`Source:`. The runs used a scratch copy of the repo
  with a 30-line harness patch that rebuilds the record by `record_text(<prompt>, …)` from the frozen fields. The patch
  checks that version 1 still equals the frozen record.
- **frozen**: the repository's harness, which sends the frozen version-1 record.

| arm | stratum (n) | base keep r1/r2 | p4 keep r1/r2 | base FP (r1; r2) | p4 FP (r1; r2) |
|---|---|---|---|---|---|
| lines | lineage_positive (31) | 15/14 | 30/31 | 0; 0 | 0; 0 |
| lines | expected_drop (26) | 1/1 | 9/9 | 1; 1 | 9; 9 |
| lines | negative_control (12) | 0/0 | 1/1 | 0; 0 | 1; 1 |
| lines | modern_control (19) | 11/9 | 11/12 | 3; 1 | 3; 3 |
| frozen | lineage_positive (31) | 16/13 | 31/31 | 0; 0 | 0; 0 |
| frozen | expected_drop (26) | 1/1 | 9/10 | 1; 1 | 9; 10 |
| frozen | negative_control (12) | 0/0 | 1/1 | 0; 0 | 1; 1 |
| frozen | modern_control (19) | 10/9 | 11/12 | 2; 1 | 2; 3 |

Triage verdicts, lines arm:
- (i) PASS: +51.6 pp (0.47 → 0.98; noise 16 pp).
- (ii) FAIL: agreement 0.842 → 0.816, within noise, but one wrong-way flip, a healthcare privacy survey kept by both
  p4 runs. In the frozen arm the agreement is 0.868 → 0.868 and the wrong-way flip is an o1 reproduction roadmap.
- (iii-neg) PASS: 0 → 1 of 12, allowed 1.2.
- **(iii-drop) FAIL**: 1 → 9 of 26 (frozen 1 → 9.5), allowed 3.6.

The p4 false keeps are mostly uncontested domain applications: saliency on an astronomy classifier, interpretable MIL
for histopathology, patient phenotyping, adversarial training for NLU accuracy, hypothesis-only bias removal. On the
uncontested gold alone (76 items) (iii-drop) still fails, 0 → 7 of 24. Screen: see its README. Both arms: **KEEP OFF**.
LLM spend for all eight runs (both gates, both arms): $0.66.
