# Screen eval set `2026-10-12a`

Superseded as a measure: production's screen (`screen-v12` on `openai/gpt-6-luna`, tier 2 luna on a `[48000, 8000]`
excerpt; D255, D260) was chosen on this set, `2026-10-13-t2pool` and `2026-10-13-holdout`; production screen output
is now measured by `evals/judge-2026-10-25j/screen`. The set stays as the record of those choices and as the
`doc_class` gold.

Task: `annotate.screen` (`workflows/annotate/screen/main.py`): from a document's acquired text decide
`relevant | borderline | irrelevant` (since gold v2: per question, Stack and Shallow Review) plus routing
`buckets`; tier 1 reads an excerpt, a `borderline` escalates to tier 2, which reads more. Frozen 2026-10-12 from a
development store by `python -m stack.evals.freeze screen` (seed `track-e-2026-10-12a`).

## Items (`items.jsonl`, 100; `texts/<item_id>.txt.gz`)
`items.jsonl` and `texts/` are not in the repository (long third-party text, D795): `tools/fetch_eval_texts.py`
fetches them into the eval-text cache, checked against `evals/archived.sha256` and the manifest.
- `input.text` = the tier-1 text exactly as production truncated it at freeze time; `texts/` holds the whole text
  (`input.full_text_digest`), so `stack eval` runs `screen.ask` on the whole text. Both digests are checked before
  any call.
- Items 1-80 (frozen first): paper 47 (openalex 36, arxiv 7, semantic_scholar 4), forum 24, url 9; proxy kw_high 46 /
  kw_low 34; licence open 69, public_web 9, restricted 2. Whole texts: median 4.2k characters, max 564k. Split 40 dev
  / 40 test inside each stratum. Some openalex/arXiv "texts" are landing or abstract pages; they are kept, since
  production screens them as they are.
- Items `scr-0081` … `scr-0100` (amendment, 2026-10-12): 20 Shallow-Review-heavy items appended by
  `python -m stack.evals.sr append --task screen` (items 1-80 byte-identical; `manifest.json` `amendments[0]`
  records the item hashes before and after, and the selection intent: 13 likely in, 1 marginal, 6 hard negatives).
  Split 10 / 10. Two appended texts contain U+2028; the appended rows escape U+2028/U+2029/U+0085, values unchanged.
- Injected text: scr-0034 contains a note addressed to "AI readers" on how to summarise it (data; labellers ignored
  it).

## Labels and gold
- **Gold v1** (`gold.jsonl`, items 1-80; rubric `evals/rubrics/screen.md`): Opus and Codex `gpt-6-sol` on the tier-1
  text, 15 disagreements to an Opus adjudicator. Agreement 65 / 80, κ 0.670; disagreement mostly `borderline` vs
  `relevant`.
- **Gold v2** (`gold-v2.jsonl`, all 100; rubric `evals/rubrics/screen-v2.md`: scope `core` / `sr_only` / `out`, SR
  2025 section `sr_category`, optional `sr_agenda`; the SR question is topic relevance, after
  `docs/scope/sr-2025-repo-criteria.md`): Opus + Codex on every item, 18 disagreements to an Opus adjudicator.
  Agreement: verdict 87/100, κ 0.777; scope 96/100, κ 0.936. Per-question gold: `stack_verdict` = verdict if scope
  `core` else irrelevant; `sr_verdict` = verdict if scope `sr_only`, or `core` with a category other than `none`,
  else irrelevant. Per-question labels by the maintainer on 10 items (`labels-v2/tomas-part2.jsonl`) override the
  labellers there; one further override (scr-0077, a system card: SR category `none` → `labs`). Base rates:
  relevant 46, borderline 8, irrelevant 46; scope core 39 / sr_only 17 / out 44. Caveat: items 1-80 were labelled
  before the rubric's part-2 section and system-card revision, the 20 appended items after.
- **doc_class gold** (`doc-class/`, 60 items): v1 two Opus labellers, 6 adjudicated; **v2** (`doc-class/gold-v2.jsonl`,
  current) adds Codex `gpt-6-sol` as an independent cross-family labeller (doc_type vs v1 57/60, κ 0.891;
  self_published and primary_lang 60/60), disagreements to two blind adjudicators (Codex's label taken, rule fixed
  in advance; `gold-v2-alt-opus-adjudicator.jsonl` has the Opus adjudicator's calls for sensitivity). v2 changes 2
  labels (`paper` → `preprint`).

## Results (2026-10-12 and 13; each comparison and the choice it led to)
- **Model choice, gold v1, tier 1** (`screen-v7`): `openai/gpt-6-luna` acc 90 % / κ 0.81 on test against the then
  incumbent `claude-haiku-4.5` 85 % / 0.73, at about a seventeenth of the cost; luna returned no quotes because the
  prompt and schema made them optional, so `screen-v8` requires 1-3 verbatim quotes for `relevant` and `borderline`
  (D175; every model then quotes; luna 4 items lost / 0 gained, McNemar p = 0.12).
- **One call vs two, gold v2** (`screen-v10` vs `screen-stack-v1` + `screen-sr-v1`, shared preamble, D202):
  luna one call on test S 86 % / R 86 %, keep 88 %, $0.38 per 1,000; two calls the same accuracy within noise at
  twice the cost. One call adopted.
- **Text usability in the call** (`screen-v11` = v10 + question U, record `text_usability`): no measurable
  accuracy cost (all McNemar p = 1.0, within the repeat noise of v10); input length (8,000 head vs head + tail
  excerpts up to `[28000, 4000]`) flat on this gold, which was labelled on the 8,000-character text and so cannot
  reward reading further.
- **Tier 2 and `doc_class`**: luna re-reading more of the text after its own `borderline` matches
  `gpt-5.6-terra` on every split within one answer at 1/20 to 1/80 of the cost per escalation; luna on
  `[48000, 8000]` chosen (D255). `screen-v12` (v11 + question D, the `doc_class` record) as pinned: Stack 85/100,
  SR 85/100, $0.67 per 1,000. A stronger tier 1 (`gpt-6-luna-pro`) gained nothing at 3.3× the cost (D260).
  `doc_class` on gold v2: the integrated v12 record and a separate luna call 55/60 exact doc_type, haiku-5.5 52,
  the collector's `metadata.doc_type` alone 28 (`doc-class/analysis-gold-v2.md`; `doc-class/scripts/rescore.py`
  re-scores from the stored rows). Paper vs preprint now comes from the collector's typed metadata where it exists
  (D260).
