# Gold v3 techniques: adjudicated reference v3x (2026-10-27)

A slug-level reference for technique presence on the 100 gold-v3 documents, replacing the unadjudicated ">= 4 of 6
runs" consensus for measurement. No document text here: `reference-v3x.jsonl` has one row per (item_id, slug) with
`verdict` (`in` / `out` / `dont_care`), `how` (which rule decided), `support` (how many of the six labeller runs had
the slug; 0 for an arm-only slug), `calibration`, and `severity` (the adjudicators' / arbiters' core / substantive /
peripheral votes). Group nodes are out, as in the strict metric.

## How it was built
1. **Universe**: every slug any of the six blind labeller runs (opus, sonnet, sol x 2) gave (780 units), plus 49 slugs
   that the compared luna arms predicted outside that union (pooled over all arms, the production baseline included).
   A slug nobody proposed is not in the reference.
2. **Adjudication** of the 455 units with support 1-5 and of a blind calibration sample of 64 support-6 units (one
   per document with disputed units): two blind adjudicators of different families (Claude Opus 5.5 as Claude Code
   subagents, GPT Sol 6.1 via Codex, read-only), guide `GUIDE.md` v2 (threshold rules R1-R10: what counts as present,
   identity checks for the commonly over-read entries, specific over generic), the rubric `rubric-v0.9-c.md` and the
   techniques 1.0.0 catalogue. They saw the labellers' quotes as unattributed pointers, never support counts or any
   model output, and wrote one decision per candidate (`check.py` validates verdicts and verbatim quotes).
   Support-6 units outside the sample stay `in`.
3. **Arbitration** of the 154 units the adjudicators split on: two arbiters (Sol 6.1 via Codex; Fable as a Claude
   Code subagent) saw both readings anonymised in random order (`ARBITRATION.md`). Both agree -> decided; otherwise
   `dont_care`.
4. **Arm extras** (49 units): adjudicated by the same two adjudicators with the arms' quotes as unattributed pointers;
   both present -> `in`, both absent -> `out`, else `dont_care`.

Counts: 504 `in` (261 support-6 unadjudicated, 218 both adjudicators present, 22 both arbiters present, 3 extras),
238 `out`, 87 `dont_care`. dev 60 / validation 40 documents: `split-dev-validation.json` (= `../split.json`, its test
part named validation).

## Agreement (the measurement ceiling)
- Labeller families before adjudication (union of a family's two runs): slug F1 .74 (sonnet-sol) to .83 (opus-sol).
- Adjudicators on the 519 candidates: present-vs-not kappa .47 (agreement .74); calibration units 61/64 both present.
  The full reference each adjudicator implies (support-6 units + its own present verdicts) agrees at F1 .875.
  A pilot on 79 candidates: guide v1 kappa .54, v2 .56; the same family across runs .76-.80, so the residual
  disagreement is systematic between families, which is why split units go to arbitration rather than to a vote.
- Arbiters agreed on 86 of 154 split units; the full references they imply agree at F1 .946.

## Scoring
Per document, `dont_care` slugs are removed from both the prediction and the reference; P / R / F1 are micro over
documents, replicates pooled; intervals are document bootstraps within source kind. A sensitivity line with
`dont_care` counted as `in` or as `out` moved the arm differences of this round by at most about 0.015 F1; an
adversarial assignment (each `dont_care` unit labelled against the candidate arm) lowered the effort-high difference
from +.033 to +.016 on validation and from +.040 to +.025 on all 100 documents.
