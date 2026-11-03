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

## Record (as written 2026-10-13)
The details as recorded when the set was frozen, labelled and run; the status at the top supersedes it.

**Freeze.** 2026-10-13 04:38 UTC from a local store (not in the repository), documents head
`rp0trgetr6tfz.documents` (575 current texts), run from an isolated export of HEAD `311e99f` plus the freezer's new
`--exclude-set` / `--prefix` / `--split-all` options, because the shared worktree's uncommitted registry edits made the
store unreadable; `manifest.json` records the worktree git state. The pool had changed since 2026-10-12a (more
lab-blog pages, fewer forum posts left), hence the different mix.

**Gold v1.** Two independent Opus sub-agents per item, 3 batches of 20, on whole texts; Codex was out of credits, so
both labellers were Opus (weaker independence than the Opus + Codex protocol). Agreement: verdict 57/60, both
questions 56/60; 4 adjudicated by an Opus adjudicator (sided with labeller 2 on all 4). Gold v1: Stack relevant 31 /
borderline 2 / irrelevant 27; SR 32 / 3 / 25.

**The run** (2026-10-13 04:43 UTC, run `20261013T044356Z_holdout_667ebb21`, in a local evals store, not in the
repository), on gold v1: Stack 88.3 % (53/60, κ 0.77), SR 90.0 % (54/60, κ 0.81), overall keep 90.0 % (kept recall
93.9 %, precision 96.9 %); escalation 2/60; $0.78 per 1,000 settled, $1.09 without provider cache, on texts of this
length; settled $0.047. (The "kept recall 93.9 %" is the scorer's relevant-recall column; its kept-recall column reads
86.1 %.)

**Gold v2 protocol** (2026-10-13 12:44-12:52 UTC). Codex `gpt-6-sol` (`codex exec --skip-git-repo-check -m gpt-6-sol
-s read-only -C <packet>`, default settings) labelled all 60 items independently in the Opus labellers' three batches
of 20, with the same packets: the same prompt (`labels/label-prompt.md`), the rubric `evals/rubrics/screen-v2.md` as
`rubric.md`, and the whole texts (byte-identical to `texts/*.txt.gz`). It never saw the Opus labels; its logs show it
read only its packet. A disagreement is a different `verdict`, `stack_verdict` or `sr_verdict` against gold v1 (the
scored fields). Each went to two blind adjudicators, labels anonymised as labeler_1/2 in a seeded order with both
rationales and quotes shown: a fresh Codex `gpt-6-sol` session and a fresh Opus sub-agent. Gold v2 takes the Codex
adjudicator's label (a cross-family adjudicator was preferred because both earlier labellers were Opus; rule fixed
before the adjudications came back). Per row: `labelers` (opus-1, opus-2, codex-sol, each with derived
`stack_verdict` / `sr_verdict`), `agreement` (all three agree on the scored fields), `agreement_detail`, `contested`
(any labeller disagreement, v1 or v2), `label_v1` where changed, `adjudication_v1`, `adjudication` (both
adjudicators, `adjudicators_agree`).

**Agreement (60 items), Codex vs gold v1:** verdict 56/60, κ 0.871; Stack 57/60, κ 0.903; SR 56/60, κ 0.873; scope
56/60, κ 0.877; keep/drop 58/60; sr_category on the 35 items both keep 31/35. For comparison the Opus pair: verdict
57/60, κ 0.908; Stack 56/60, κ 0.876; SR 57/60, κ 0.909. Codex vs opus-1 verdict 55/60 (κ 0.843), vs opus-2 56/60
(κ 0.871). On the 4 disagreements the Opus adjudicator sided with gold v1 on 3 and chose a third label on 1; the Codex
adjudicator sided with Codex on 3 and chose a third label on 1. v12 reached `irrelevant` on 0044 and 0051 after a
tier-2 escalation from `borderline`. Gold v2 changes 4 of 60 labels; contested 7/60 (v1: 4), all three labellers
agree on 53/60. Base rates v2: verdict relevant 36 / borderline 1 / irrelevant 23; Stack 33 / 0 / 27 (v1 31 / 2 / 27);
SR 35 / 1 / 24 (v1 32 / 3 / 25).

**Re-score** (stored rows of `20261013T044356Z_holdout_667ebb21`, scored with `stack eval score --gold`; no new
calls): Stack accuracy does not move (the four items swap which of them v12 gets right: 0007 and 0057 under v1, 0051
and 0057 under v2); SR loses one item. Tier 2's two escalations (0044, 0051: `borderline` → `irrelevant`) turned two
right answers wrong under v1 (gold `borderline`); under v2 (gold `relevant`) both are wrong before and after. Which
family adjudicates decides the one item per question by which the three golds differ.
