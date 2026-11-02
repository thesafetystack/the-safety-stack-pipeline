# Screen gate set `2026-10-21-p4gate` (promotion gate of the P4 technique-provenance clause, scope R2 D1)

The screen half of the P4 promotion gate. Protocol, exclusions, labellers, thresholds and the two run arms are in
`evals/triage/2026-10-21-p4gate/README.md`. All items are split `test`. **Result: KEEP OFF.**

## Items (`items.jsonl`, 93; `texts/<item_id>.txt.gz`)
- Frozen with `stack.evals.freeze.screen_cands`: `input.text` = the tier-1 text as production truncates it, the whole
  text in `texts/`, from the same documents head and code (`c692e0c`) as the triage set. Licence: open 86, restricted
  5, public_web 2. Each item carries `input.published_at` / `input.channels` for screen-v13's record lines.
- The 24 triage items that production screened are in both sets, which gives end-to-end retention. The rest are seeded
  top-ups from screened documents: lineage pool 14 (split by production S verdict and era), domain-application pool 7,
  negative controls 10 (production S and R irrelevant), modern controls 8.
- **Two further top-ups before any eval run.** The minimum-size rule fixed in the thresholds (≥ 10 per gated stratum)
  failed on the first gold: negative controls 6, expected drops 5. Top-up 1 added 12 domain-application and 8 negative
  documents (`scr-p4-0064..0083`). That left expected drops at 9, so top-up 2 added 10 domain-application documents
  (`scr-p4-0084..0093`). Each round came last in the seeded order, so earlier picks and ids are unchanged. Each round was
  labelled blind by both labellers (`labels-p4/label-task-topup.md`) and adjudicated like the first.
- `collector-doc-types.json` was added after the runs below, which ran without it (it feeds only the `doc_class`
  record; no user message reads it).

## Labels and gold
- **Agreement (93 items):** P4 S keep 79/93 (κ 0.60), lineage 85/93 (κ 0.83), as-written S keep 67/93 (κ 0.40), as-written
  R keep 58/93 (κ 0.26). R is where the labellers differ most: Codex tends to give R to pre-2020 adversarial and privacy
  papers, Opus does not.
- 48 items adjudicated with written reasons (`labels-p4/adjudication.jsonl`). The conventions applied:
  - Narrow-model robustness, privacy and XAI are out as written unless P3's safety framing applies.
  - R for narrow-model lineage work is irrelevant, or borderline when the text frames it as a safety-critical-systems
    problem.
  - Privacy mitigation for LLMs is S-core and R irrelevant.
  - DNN test-coverage criteria are not a P4 kind.
- `gold.jsonl` (P4 scope; `stack_verdict` = S, `sr_verdict` = R, `buckets` and `sr_category` not labelled, null) and
  `gold-as-written.jsonl`. Strata: lineage_positive 40 (2015-19: 18, 2020-23: 22), expected_drop 12, negative_control 12,
  modern_control 17, ordinary_relevant 12.

## Runs 2026-10-21 (openai/gpt-6-luna, tier 1 only, `--no-tier2`, `--repeat 2`; not in the repo)

| arm | stratum (n) | base S keep r1/r2 | p4 S keep r1/r2 |
|---|---|---|---|
| lines | lineage_positive (40) | 25/27 | 39/39 |
| lines | expected_drop (12) | 3/5 | 9/8 |
| lines | negative_control (12) | 0/2 | 3/4 |
| lines | modern_control (17) | 9/8 | 12/12 |
| frozen | lineage_positive (40) | 25/25 | 39/39 |
| frozen | expected_drop (12) | 5/3 | 8/8 |
| frozen | negative_control (12) | 1/3 | 4/2 |
| frozen | modern_control (17) | 10/9 | 11/11 |

Lines arm (frozen arm in brackets):
- (i) PASS: +32.5 pp, 0.65 → 0.97 [+35.0].
- (ii) FAIL: agreement 0.794 → 0.765 [0.794 → 0.824], with wrong-way flips on a hematology SAE application
  (CytoSAE, both arms) and an MLLM psychophysics paper (lines).
- (iii-neg) FAIL: false keeps 1.0 → 3.5 of 12, allowed 2.2 [PASS: 2.0 → 3.0, allowed 3.2].
- **(iii-drop) FAIL**: 4.0 → 8.5 of 12, allowed 5.2 [4.0 → 8.0].
- (iv) PASS: R agreement 0.780 → 0.758 within noise 0.14 [0.790 → 0.769].

R moves little under p4. In lineage positives R keep goes 15.5 → 16.0 [17.5 → 17.0], so the S-only wording holds.
End-to-end retention of the 12 lineage positives present in both sets: 6.0 → 12.0 [5.0 → 12.0].
