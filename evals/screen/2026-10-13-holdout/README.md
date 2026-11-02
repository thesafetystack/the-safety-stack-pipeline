# Screen holdout `2026-10-13-holdout` (track T-SCREEN, round 5; D-3 approved by Tomáš)

**Use:** one pinned-config check of `screen-v12` (run once, below). Nothing is tuned on this set; a prompt or model
change is compared on 2026-10-12a dev/test, and only the final pin is run here again.

- **Frozen** 2026-10-13 04:38 UTC from `v4/store-p5-dev` (gitignored; not in the repo), documents head `rp0trgetr6tfz.documents` (575 current texts),
  by `python -m stack.evals.freeze screen --n 60 --exclude-set evals/screen/2026-10-12a/items.jsonl --exclude-set
  evals/screen/2026-10-13-t2pool/items.jsonl --prefix hold --split-all holdout --seed t-screen-2026-10-13-holdout`
  (run from an isolated export of HEAD `311e99f` + the freezer's new `--exclude-set/--prefix/--split-all` options,
  because the shared worktree's uncommitted registry edits make the store unreadable; `manifest.json` records the
  worktree git state). No document of 2026-10-12a or the tier-2 pool (overlap 0 of 60).
- **Stratification:** the freezer's, as for 2026-10-12a (channel quotas proportional to the pool, keyword-proxy
  balance). The pool has changed since 2026-10-12a (more lab-blog pages, fewer forum posts left), so the mix is paper 33
  (arxiv 14, openalex 16, S2 3), url 24, forum 3; kw_high 35 / kw_low 25; basis full_text 26, page 24, abstract 7, post 3;
  texts median 60k characters (2026-10-12a: 4.2k). All 60 items are split `holdout`.
- **Labels:** two independent Opus sub-agents per item (3 batches of 20; whole texts, the 2026-10-12a/v2 rubric
  `evals/rubrics/screen-v2.md`, prompt `labels/label-prompt.md`); **Codex was out of credits, so both labelers are Opus**
  (weaker independence than the Opus + Codex protocol). Agreement: verdict 57/60, both questions 56/60; 4 adjudicated by
  an Opus adjudicator (sided with labeler 2 on all 4). Gold (`gold.jsonl`): Stack relevant 31 / borderline 2 / irrelevant
  27; SR 32 / 3 / 25. **Superseded by `gold-v2.jsonl`** (Codex `gpt-6-sol` as the second, cross-family labeller;
  section "Gold v2" below). Since `gold-v2.jsonl` exists, `stack eval` and `stack eval score` default to it here.
- `collector-doc-types.json`: each item's collector `metadata.doc_type` (read-only from v4/store-p5-dev) so the harness
  applies screen-v12's channel and metadata rules.

## The one run (2026-10-13 04:43 UTC, `v4/evals-store/screen/2026-10-13-holdout/20261013T044356Z_holdout_667ebb21`, not in the repo)
`stack eval screen --set 2026-10-13-holdout --split holdout --models openai/gpt-6-luna --plan configs/plans/dev-v2-m4.yaml
--tier2-model openai/gpt-6-luna` (screen-v12 as pinned: tier 1 luna `[12000, 2000]`, tier 2 luna `[48000, 8000]`; the
harness takes tier 2 from `--tier2-model`, not from the plan pin, so it is passed explicitly). Stack 88.3 % (53/60, κ
0.77), SR 90.0 % (54/60, κ 0.81), overall keep 90.0 % (kept recall 93.9 %, precision 96.9 %); escalation 2/60; $0.78
per 1,000 settled, $1.09 without provider cache, on texts of this length; settled $0.047.
(The "kept recall 93.9 %" is the scorer's relevant-recall column; its kept-recall column reads 86.1 %.) **Re-scored on gold v2** (stored rows, no new calls; `rescore-gold-v2.md`): Stack 88.3 % (53/60, κ 0.765), SR 88.3 % (53/60, κ 0.769), overall keep 88.3 % (κ 0.767; relevant recall 86.1 %, precision 96.9 %).

## Gold v2 (2026-10-13 12:44-12:52 UTC): Codex as the second, cross-family labeller
**Protocol.** Codex `gpt-6-sol` (`codex exec --skip-git-repo-check -m gpt-6-sol -s read-only -C <packet>`, default
settings) labelled all 60 items independently in the Opus labelers' three batches of 20, with the same packets: the
same prompt (`labels/label-prompt.md`), the rubric `evals/rubrics/screen-v2.md` as `rubric.md`, and the whole texts
(byte-identical to `texts/*.txt.gz`). It never saw the Opus labels; its logs show it read only its packet. A
disagreement is a different `verdict`, `stack_verdict` or `sr_verdict` against gold v1 (the scored fields; this set's v1
rule). Each went to two blind adjudicators, labels anonymised as labeler_1/2 in a seeded order with both rationales and
quotes shown (prompt `labels/adjudicate-prompt-v2.md`): a fresh Codex `gpt-6-sol` session and a fresh Opus sub-agent.
Gold v2 takes the Codex adjudicator's label (the brief prefers a cross-family adjudicator because both earlier labelers
were Opus; rule fixed before the adjudications came back); the Opus adjudicator is recorded, and
`gold-v2-alt-opus-adjudicator.jsonl` is the same file with its calls, for sensitivity. Per row: `labelers` (opus-1,
opus-2, codex-sol, each with derived `stack_verdict` / `sr_verdict`), `agreement` (all three agree on the scored
fields), `agreement_detail`, `contested` (any labeler disagreement, v1 or v2), `label_v1` where changed,
`adjudication_v1`, `adjudication` (both adjudicators, `adjudicators_agree`).

**Agreement (60 items), Codex vs gold v1:** verdict 56/60, κ 0.871; Stack 57/60, κ 0.903; SR 56/60, κ 0.873; scope
56/60, κ 0.877; keep/drop 58/60; sr_category on the 35 items both keep 31/35. For comparison the Opus pair: verdict 57/60,
κ 0.908; Stack 56/60, κ 0.876; SR 57/60, κ 0.909. Codex vs opus-1 verdict 55/60 (κ 0.843), vs opus-2 56/60 (κ 0.871).

**The 4 disagreements, and the adjudicators split on all 4 along family lines** (the Opus adjudicator sided with gold v1
on 3 and chose a third label on 1; the Codex adjudicator sided with Codex on 3 and chose a third label on 1):

| item | gold v1 (S / R) | Codex labeller | Codex adjudicator = gold v2 | Opus adjudicator (alt) |
|---|---|---|---|---|
| hold-0007 Alignment Games (HCI, conceptual repair) | borderline sr_only (irr / bl) | relevant core | relevant core (rel / rel) | = gold v1 |
| hold-0044 Diffusion Controller (v1 contested) | borderline core (bl / bl) | irrelevant | relevant core (rel / rel) | = gold v1 |
| hold-0051 latent visual reasoning (probes, logit lens) | borderline core (bl / bl) | relevant sr_only | relevant sr_only (irr / rel) | relevant core (rel / rel) |
| hold-0057 OmniMoE-VL (routing analysis) | irrelevant | borderline sr_only | borderline sr_only (irr / bl) | = gold v1 |

All four are capability-motivated papers at the rubric's capabilities and model-internals hard cases; v12 answered
`irrelevant` on all four (0044 and 0051 after a tier-2 escalation from `borderline`). **Gold v2 changes 4 of 60 labels**;
contested 7/60 (v1: 4), all three labelers agree on 53/60. Base rates v2: verdict relevant 36 / borderline 1 / irrelevant
23; Stack 33 / 0 / 27 (v1 31 / 2 / 27); SR 35 / 1 / 24 (v1 32 / 3 / 25).

**v12 run re-scored** (stored rows of `20261013T044356Z_holdout_667ebb21`, copied to scratch and scored there with
`stack eval score --gold`; no new calls, no OpenRouter spend; full tables in `rescore-gold-v2.md`):

| gold | Stack acc / κ | SR acc / κ | overall keep acc / κ | relevant recall / precision (overall) |
|---|---|---|---|---|
| v1 (Opus only) | 88.3 % / 0.773 | 90.0 % / 0.809 | 90.0 % / 0.808 | 93.9 % / 96.9 % |
| **v2** (Codex adjudicator) | **88.3 % / 0.765** | **88.3 % / 0.769** | **88.3 % / 0.767** | 86.1 % / 96.9 % |
| v2-alt (Opus adjudicator) | 88.3 % / 0.769 | 90.0 % / 0.806 | 90.0 % / 0.804 | 91.2 % / 96.9 % |

Stack accuracy does not move (the four items swap which of them v12 gets right: 0007 and 0057 under v1, 0051 and 0057
under v2); SR loses one item. Tier 2's two escalations (0044, 0051: `borderline` → `irrelevant`) turned two right
answers wrong under v1 (gold `borderline`); under v2 (gold `relevant`) both are wrong before and after, so on this set
tier 2 still corrects none. The headline differs by at most one item per question across the three golds; which family
adjudicates decides that item.
