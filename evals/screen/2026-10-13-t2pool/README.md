# Screen tier-2 pool `2026-10-13-t2pool`

Superseded as a measure: it served the tier-2 choice (luna on `[48000, 8000]`, D255); production screen output is
now measured by `evals/judge-2026-10-25j/screen`.

The 15 documents that `screen-v11` tier 1 (`openai/gpt-6-luna`, `[12000, 2000]`) called `borderline` in a real
development build (2026-10-13): real escalations on the production mix of long texts (1.9k-502k characters, median
74k). Read-only extraction (`scripts/pool_extract.py`).

- `items.jsonl`: per item the store's tier-1 and tier-2 (terra, whole text) answers per question, their cost and
  tokens, title, channel, licence class, `text_digest`; `texts/<item_id>.txt.gz` the whole stored text (not in the
  repository; fetched into the eval-text cache by `tools/fetch_eval_texts.py`).
- `gold.jsonl`: two independent Opus labellers on the whole text (`labels/label-prompt.md`, rubric
  `evals/rubrics/screen-v2.md`), 12/15 agree on both questions, 3 adjudicated by an Opus adjudicator. Both labellers
  are Opus (weaker independence than the Opus + Codex protocol of 2026-10-12a). Per-question gold by
  `stack.evals.gold.question_verdicts`. Gold: Stack relevant 4 / borderline 2 / irrelevant 9; SR relevant 2 /
  borderline 3 / irrelevant 10.
- `answers/t2-<model>-<limit>.json`: counterfactual tier 2 on the same tier-1 answer (its rationale in the preamble,
  exactly as `ask` builds it), through the production client and ledger (`scripts/pool_t2.py`).
- `analysis.md`: the comparison (`scripts/analyse.py`), with the eval set 2026-10-12a (dev, test) and the projection
  to the production mix. The scripts are as run; their input and output paths pointed at a scratch directory.

Caveat: 15 items; differences of one or two answers are noise. The pool is borderline-only, so it measures what tier 2
does, not tier 1.
