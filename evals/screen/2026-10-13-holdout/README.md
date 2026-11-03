# Screen holdout `2026-10-13-holdout`

Superseded as a measure: a one-time check of the `screen-v12` pin chosen on `2026-10-12a` (D260); production screen
output is now measured by `evals/judge-2026-10-25j/screen`.

**Use:** one pinned-config check of `screen-v12` (run once, below). Nothing was tuned on this set; a prompt or model
change was compared on 2026-10-12a dev/test, and only the final pin was run here.

- **Frozen** 2026-10-13 from a development store (575 current texts) by `python -m stack.evals.freeze screen --n 60
  --exclude-set evals/screen/2026-10-12a/items.jsonl --exclude-set evals/screen/2026-10-13-t2pool/items.jsonl
  --prefix hold --split-all holdout --seed t-screen-2026-10-13-holdout`; no document of 2026-10-12a or the tier-2
  pool (overlap 0 of 60). `items.jsonl` and `texts/` are not in the repository (long third-party text, D795):
  `tools/fetch_eval_texts.py` fetches them into the eval-text cache.
- **Stratification:** the freezer's, as for 2026-10-12a. Mix: paper 33 (arxiv 14, openalex 16, S2 3), url 24, forum
  3; kw_high 35 / kw_low 25; basis full_text 26, page 24, abstract 7, post 3; texts median 60k characters
  (2026-10-12a: 4.2k). All 60 items are split `holdout`.
- **Labels:** gold v1 (`gold.jsonl`): two independent Opus labellers per item on whole texts (rubric
  `evals/rubrics/screen-v2.md`, prompt `labels/label-prompt.md`), 4 disagreements to an Opus adjudicator. **Gold v2**
  (`gold-v2.jsonl`, the default of `stack eval` here): Codex `gpt-6-sol` as an independent cross-family labeller on
  the same packets; its 4 disagreements with v1 went to two blind adjudicators (prompt
  `labels/adjudicate-prompt-v2.md`), Codex's label taken (rule fixed in advance), the Opus adjudicator's calls kept in
  `gold-v2-alt-opus-adjudicator.jsonl`. Codex vs gold v1: verdict 56/60, κ 0.871 (the Opus pair: 57/60, κ 0.908).
- `collector-doc-types.json`: each item's collector `metadata.doc_type`, so the harness applies screen-v12's channel
  and metadata rules.

The 4 disagreements (the adjudicators split on all 4 along family lines):

| item | gold v1 (S / R) | Codex labeller | Codex adjudicator = gold v2 | Opus adjudicator (alt) |
|---|---|---|---|---|
| hold-0007 Alignment Games (HCI, conceptual repair) | borderline sr_only (irr / bl) | relevant core | relevant core (rel / rel) | = gold v1 |
| hold-0044 Diffusion Controller (v1 contested) | borderline core (bl / bl) | irrelevant | relevant core (rel / rel) | = gold v1 |
| hold-0051 latent visual reasoning (probes, logit lens) | borderline core (bl / bl) | relevant sr_only | relevant sr_only (irr / rel) | relevant core (rel / rel) |
| hold-0057 OmniMoE-VL (routing analysis) | irrelevant | borderline sr_only | borderline sr_only (irr / bl) | = gold v1 |

All four are capability-motivated papers at the rubric's capabilities and model-internals hard cases; v12 answered
`irrelevant` on all four.

## The one run (2026-10-13)
`stack eval screen --set 2026-10-13-holdout --split holdout --models openai/gpt-6-luna --plan
configs/plans/dev-v2-m4.yaml --tier2-model openai/gpt-6-luna` (screen-v12 as pinned: tier 1 luna `[12000, 2000]`,
tier 2 luna `[48000, 8000]`; the harness takes tier 2 from `--tier2-model`, not from the plan pin). Escalation 2/60;
$0.78 per 1,000 settled on texts of this length. Scored on the three golds (`rescore-gold-v2.md`):

| gold | Stack acc / κ | SR acc / κ | overall keep acc / κ | relevant recall / precision (overall) |
|---|---|---|---|---|
| v1 (Opus only) | 88.3 % / 0.773 | 90.0 % / 0.809 | 90.0 % / 0.808 | 93.9 % / 96.9 % |
| **v2** (Codex adjudicator) | **88.3 % / 0.765** | **88.3 % / 0.769** | **88.3 % / 0.767** | 86.1 % / 96.9 % |
| v2-alt (Opus adjudicator) | 88.3 % / 0.769 | 90.0 % / 0.806 | 90.0 % / 0.804 | 91.2 % / 96.9 % |

The headline differs by at most one item per question across the three golds; on this set tier 2 corrects none.
