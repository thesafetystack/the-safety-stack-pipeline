# technique_staged eval set 2026-10-18b: gold v2 (120 documents, taxonomy-v0.9-234)

A new set (techpipe D630) for `stack eval technique_staged` against the 234-entry v0.9 draft
`taxonomy/taxonomy-v0.9-234.yaml` (D625), frozen with prompt set technique-staged-v3. 120 documents drawn from a
read-only production store copy, stratified by doc-type group and length band. Gold: two independent gpt-6-astra
labellings, reconciled by gpt-6-astra on 25 items and by a deterministic merge rule on the other 95 (Codex ran out of
credits), then a blind verification of 30 items (Opus labels, disputes adjudicated by Sonnet). Everything was made
outside the repository and staged here by `scripts/stage.py`.

## Composition
- 120 items g2-0001..g2-0120; split dev 60 / test 60 (alternating by length within the doc-type group: items sorted
  by (group, chars, item_id), the first of each group dev; deterministic). Report over all items (`--split all`).
- Groups: paper 52, forum 32, blog 18, card 8, report 10. Length bands: <20k 41, 20-60k 33, 60-100k 21, >100k 25.
  Channel groups: paper 64, forum 31, url 25. Gold `doc_kind`: paper 55, blogpost 33, commentary 15, system_card 7,
  technical_report 3, other 3, and one each of news, eval_report, model_card, policy.
- `items.jsonl` is byte-identical to the dry-run build the first trial runs used (sha256
  `fa4912e8a1a0a7dca03451de01c57d9504579c3cbdf0cdfeaea5e2ec4559ce78`), so those runs score here as they are.

## Gold counts
1,189 rows: 1,082 scored (group nodes and `dont_care` rows out), 846 of them high confidence, 939 given by both
labellers (`sources` A and B); 41 `dont_care` rows, 66 group-node rows, 10 rows with `level_alternatives`. Row
sources: A+B 1,007, A only 72, B only 78, opus 29, reconciler 3. Adjudicator: astra-reconciler 25 items, merge-rule
95. 162 extra techniques, 317 menu gaps. Every quote is found verbatim (whitespace-normalised) in its text; no row has
a role above `mentioned`; every level alternative is structurally linked to its row's slug (`manifest.json`
`gold.checks`).

## Sampling (`scripts/sampling/`, `sources/sampling.json`)
- `pool.py` over the store's batch-import heads (corpus `20261017T175503Z_rz3vvhk92wt9r`; stack_relevance,
  text_usability and doc_class `20261017T175521Z_rah6b8pyfs0e9`): documents judged relevant and usable, current text
  status ok with the blob present, 2026-10-17a's documents and their same-raw duplicates excluded: 11,752 rows (the
  pool file, 4.2 MB, is not copied; its sha256 is recorded and `stage.py --check-pool` re-derives it byte for byte).
- `select.py`: licence open or public_web (restricted texts are not sent to external labellers), seed 20261018, a
  seeded sample per (group, band) cell with quotas paper (8, 15, 14, 15), forum (14, 10, 5, 3), blog (14, 3, 0, 1),
  card (3, 2, 0, 3), report (2, 3, 2, 3) for <20k / 20-60k / 60-100k / >100k. Re-running it on the pool reproduces the
  selection byte for byte. The card <20k quota was cut from 5 to 3 and paper >100k raised from 13 to 15 after a
  first draw showed stub cards.

## Labelling (`adjudication/`)
- Packet: `GUIDE.md` (guide v2.0: v1.1 of 2026-10-17a plus `level_alternatives` and two new rulings in the text),
  `RULINGS.md` (1-40 from 17a, 41-46 new), the role rubric `rubric-v0.4.0.md` (not copied; sha256 in `packet.json`)
  and the menu = the production menu text of the 234-entry taxonomy under a one-line title.
- L1 (`adj-X.jsonl`, `prompts/L1.md`, document-first) and L2 (`adj-Y.jsonl`, `prompts/L2.md`, inventory-first), both
  gpt-6-astra: 1,092 and 1,105 rows; L1 vs L2 slug F1 0.923 (group nodes out). Both are the same model, so the two
  labellings are correlated; 17a's cross-model A vs B agreement was 0.796.
- Reconciliation (`astra-reconciler.jsonl`, `prompts/RECONCILE.md`, gpt-6-astra): 25 items, then the Codex workspace
  ran out of credits (2026-10-18, 02:12 UTC). The other 95 items take the deterministic merge rule
  (`adjudication/scripts/merge.py`, its docstring is the rule): the union of the two labellings' slugs, sources X / Y,
  per role the maximum, a single-labeller row capped at `med`, quote and reason from the higher-confidence row.
  Calibrated against the reconciler on its 25 items: strict slug F1 0.976 (the merge keeps 8 rows the reconciler
  dropped and misses 3 it added). `pre-verification.jsonl` is that gold before verification (G0).
- `final.jsonl` (the labels of record) = G0 with the verification applied.

## Verification
- Sample: 30 items (`adjudication/scripts/verify_sample.py`, seed 20261019, quotas per group and length, 10 over 60k
  characters; `verify-sample.json`), labelled blind by Opus (`opus.jsonl`).
- Disputes (`adjudication/scripts/disputes.py`): a slug only one of G0 and Opus gave, unless a level alternative of
  the other side; 110 slugs on 26 items. The labellers were blinded as P and Q with a seeded per-item swap (seed
  20261020; `verification.jsonl` keeps the key: `swap` true = gold shown as Q). The copied `disputes.py` has one
  line removed (its leak-check pattern named workspace paths and a person), so it does not run as copied;
  `packet.json` has both sha256.
- Adjudication: a Sonnet subagent (third model; Codex unavailable), with `prompts/VERIFY-ADJ.md`, which was written
  for a Codex adjudicator, behind an agent preamble. Verdicts (all slugs, group nodes included): gold-only slugs 31
  yes / 18 no / 19 borderline, Opus-only 7 yes / 13 no / 22 borderline; 8 verdicts named a better slug, all already
  gold rows. One batch (8 items) was adjudicated twice: 21 of 37 verdicts identical, one hard no -> yes flip, the rest
  moved through borderline (`verification-first-run.jsonl` holds the first run; the second is applied).
- Applied (`adjudication/scripts/verify_apply.py`, docstring = the rules): `yes` on an Opus-only slug adds the Opus
  row (`sources: ["opus"]`, `med`); `no` on a gold row removes it (listed in `dropped`, by `verification`);
  `borderline` sets `dont_care: true` (kept G0 row, or the Opus row added flagged; the scorer leaves it out of every
  variant, D611); a better slug is added with `sources: ["verification"]` when absent (none was). Each verified item
  carries an `adjudication.verification` record and `labelers` opus and sonnet-adjudicator.
- Estimates (`estimates.json`): truth on the sample = slugs both G0 and Opus gave (level-alternative matches
  included) plus adjudicated `yes` and better slugs, `borderline` excluded, group nodes out; 10,000 document
  bootstrap. G0 (pre-verification gold) precision 0.942 [0.914, 0.966], recall 0.976 [0.960, 0.993]; recall is an
  upper bound, since misses neither labeller nor Opus found are invisible. G0 precision by confidence: high 0.98 (246
  rows), med 0.79 (62). Opus precision 0.953 [0.918, 0.979], recall 0.896 [0.854, 0.927]. Opus vs G0 slug F1 0.830.
- Checked when freezing: `merge.py` on L1 / L2 reproduces the merge for all 120 items, G0 = the reconciler's labels
  on its 25 items and the merge elsewhere, `verify_apply.py` on G0 reproduces `final.jsonl` (120 of 120) and
  `estimates.json`, and `verify_sample.py` reproduces the sample (one-off runs on copies; the scripts write files, so
  `stage.py` does not run them).

## Known biases
- The 95 merge-rule items are a union of two labellings with no adjudication: rows one labeller gave wrongly stay
  (capped at `med`), so precision-leaning gold errors (false gold rows, which lower a system's measured precision) are
  likelier there than on the 25 reconciled items. The verification sample estimates G0 precision at 0.94 overall and
  0.79 for `med` rows.
- The gold was built by GPT-family labellers (gpt-6-astra twice, the same model) and checked by Claude models (Opus
  labels, Sonnet adjudication) on 30 items only. Systems from either family may be favoured where their habits match
  the labellers'.
- Recall estimates are upper bounds (see above); the adjudicator was not fully self-consistent (21 / 37 on a rerun).
- Two system cards are stubs (g2-0103, 2,209 characters; g2-0104, 1,296), g2-0061 is a 479-character link post, six
  texts carry `text_quality: flagged`, g2-0008 is in German.

## Running and scoring
`stack eval technique_staged --set 2026-10-18b --pin mode=<mode> --pin taxonomy=taxonomy/taxonomy-v0.9-234.yaml
--prompt technique-staged-v3 ...`: the input check compares the v3 user messages and the staged digests frozen with
the 234-entry file, and every mode passes under v3 with that pin. Without the pin the gold check refuses the run (the
gold names entries the 214-entry file lacks).

## Files and scripts
- `items.jsonl`: `input` (title, basis_role, text_digest, full_text_digest, full_text_chars, `staged` =
  `main.freeze_info(text, name="technique-staged-v3", taxonomy_path=taxonomy/taxonomy-v0.9-234.yaml)`),
  `input_digest` (the v3 single user message), `license_class`, `source_item` `mirror:<doc_id>`, `split`, `stratum`
  {channel, channel_group, doc_type, group, band}. `texts/g2-NNNN.txt.gz`: the store blobs, byte-identical to the
  labellers' copies (deterministic gzip).
- `gold.jsonl`: 17a's row shape plus `level_alternatives`, `dont_care` and `verification`; `sources` A = L1, B = L2,
  `reconciler`, `opus`, `verification`; `note` = the reconciler's or the merge rule's decision.
- `manifest.json`: counts, `gold.checks`, `frozen_with` (taxonomy, rubric-v0.9-c, the v3 prompt digests, segmenter and
  lexical versions, the labelling packet's digests), `sampling`, `verification` (with the estimates), `staging`.
- `uv run --no-sync python evals/technique_staged/2026-10-18b/scripts/stage.py <WORKSPACE> <STORE_DIR> --check-pool
  [--write]` stages the inputs from the labelling workspace and the store copy, both read only; neither path is
  recorded. `uv run --no-sync python evals/technique_staged/2026-10-18b/scripts/freeze.py [--write]` builds items,
  gold and manifest from the staged files and, without `--write`, checks them byte for byte (`--allow-partial` only
  for a dry run on stand-in or incomplete labels).
