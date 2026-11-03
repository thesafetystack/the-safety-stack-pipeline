# technique_staged eval set 2026-10-18a: 2026-10-17b with the `candidate:` rows mapped to taxonomy-v0.9-234

Superseded by gold v2 (`2026-10-18b` / `2026-10-18c`, 120 documents) and gold v3 (`2026-10-23a`). Kept frozen for
the runs scored on it and the tests.

A gold revision of `2026-10-17b` (techpipe D627) for the 234-entry v0.9 draft `taxonomy/taxonomy-v0.9-234.yaml`
(D625). Items, splits and texts are 17b's (and so 17a's), byte for byte: `items.jsonl` and `texts/` are copies with
the same `items_sha256`, so a run made on 17a or 17b can be scored here. Only `gold.jsonl` and `manifest.json` differ.
17a and 17b stay frozen. Provenance of the gold: `../2026-10-17a/README.md` (labelling), `../2026-10-17b/README.md`
(the blind audit).

## Why
The 17a labellers' menu had, after the 214 entries and 9 group nodes, a 20-entry appendix of proposed entries
(`candidate:<slug>`). The 234-entry draft admits all 20. In 17a / 17b the 8 `candidate:` rows were never scored; here
they are ordinary gold rows, so the new entries count in recall and precision.

## Derivation (`scripts/derive.py`, reproducible)
- A row flagged `candidate` (`candidate:<slug>`): `slug` -> `<slug>`, the `candidate` flag dropped,
  `remapped_from: "candidate:<slug>"` added; `<slug>` must be a technique of the 234-entry file. Nothing else changes.
- The 8 rows: an-0011 `value-alignment-eval`; an-0015, an-0031, an-0035 `persuasion-capability-eval`; an-0030
  `shutdown-incentive-neutralization`; an-0031, an-0033 `cot-monitorability-eval`; an-0035 `honesty-lying-eval`.
- No document ends with two rows of one slug (the script would keep the higher-confidence row with a note and list
  the pair under `derivation.duplicates`; the list is empty).
- `uv run --no-sync python evals/technique_staged/2026-10-18a/scripts/derive.py` checks that the files match byte for
  byte; `--write` rebuilds them from 17b's files.

## Counts
431 rows, as in 17b: 0 `candidate:` rows (8 remapped), 15 `dont_care`, 20 group-node rows, **396 scored rows**
(388 in 17b), **148 high-confidence** scored rows (147; an-0035's `honesty-lying-eval` is high), 281 given by both
labellers (277).

## Scoring and runs
- Score a 17a / 17b run here with `--set-dir evals/technique_staged/2026-10-18a` (`stack eval score` with
  `--summary summary-2026-10-18a`, `staged-compare`, `staged-table`); the scorer reads the 234-entry taxonomy named in
  `manifest.json` (`frozen_with.taxonomy_path`) for group nodes, kinds and the hierarchy-tolerant links (D626).
- A run made on 17a / 17b used the 214-entry menu and could not name the 8 remapped slugs: on 18a its recall is lower
  than on 17b by those rows and its precision is unchanged. Example, single gpt-6-luna with prompt set v1 (run
  `20261017T165200Z_all_5dd45f5f`, 3 repeats pooled): slugs P / R 0.971 / 0.546 on 17b, 0.971 / 0.535 here.
- A new run here pins the taxonomy: `--pin taxonomy=taxonomy/taxonomy-v0.9-234.yaml`. Without it the gold check
  refuses the run (the 8 remapped slugs are not in the 214-entry file).
- The items' `input.staged` digests and `frozen_with.prompt_digests` were frozen against the 214-entry file
  (`frozen_with.staged_inputs_taxonomy_sha256`). With the 234-entry pin, the input check (`frozen_problems`) refuses
  staged runs under every prompt set (`lexical_digest` differs on 4 items) and lexical runs under the frozen prompt
  set technique-staged-v1 (the lexical user message differs on 8 items). Single and sweep runs pass: what they check,
  the segmentation and the single and P user messages, does not depend on the taxonomy, and user messages are compared
  only under the frozen prompt set v1. Checked with the harness's `check_inputs` on this set for v1 / v2 / v3 in
  every mode.

## Known gaps
- All 20 new entries were labellable: 19 were in the labellers' appendix under the same slug, and
  `intrinsic-interpretability-training` was the appendix's `interpretability-monitorability-by-design-training`
  (renamed and narrowed). So 18a misses rows for them only where the labellers missed a candidate, as for any entry;
  but the appendix definitions were drafts. Three changed in substance: `intrinsic-interpretability-training` (above),
  `cot-monitorability-eval` (now also the model's ability to control, hide or do without its trace, and not
  monitor robustness) and, by wording only, `shutdown-incentive-neutralization`. Rows inside a widened scope can be
  missing.
- 13 of the 214 carried-over entries have a changed definition in the 234-entry file and 4 a changed name
  (`generalisation-confining-finetuning`, `safe-constrained-rlhf`, `cot-faithfulness-monitorability-eval`,
  `persuasion-manipulation-eval`); their gold rows were labelled against the old wording.
- The blind audit of 17b sampled false positives of runs on the 214-entry menu only, so it could add no row for a new
  entry.
- `alt_slugs` (a labeller field the scorer does not read) still names `candidate:model-extraction-defenses` (an-0019)
  and `candidate:persuasion-capability-eval` (an-0032); the derivation leaves them as they were.
- No row carries `level_alternatives` (D626) yet.
