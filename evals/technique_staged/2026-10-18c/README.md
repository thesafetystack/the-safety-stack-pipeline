# technique_staged eval set 2026-10-18c: 2026-10-18b with the reconciliation completed and a second verification

A gold revision of `2026-10-18b` (techpipe D632). Items, splits, strata and texts are 18b's, byte for byte
(`items.jsonl` and `texts/` are copies; `items_sha256` `fa4912e8a1a0a7dca03451de01c57d9504579c3cbdf0cdfeaea5e2ec4559ce78`,
which `scripts/derive.py` requires), so every run made on 18b scores here with `--set-dir
evals/technique_staged/2026-10-18c` (`stack eval score` with `--summary summary-2026-10-18c`, `staged-compare`,
`staged-table`). Only `gold.jsonl`, `manifest.json` and `adjudication/` differ. 18b stays frozen: runs and results
were reported on it, and its gold (95 items by a merge rule) is a different object; report both until one is
retired. Sampling, labelling packet, the two labellings and the round-1 verification inputs are 18b's
(`../2026-10-18b/README.md`, `../2026-10-18b/adjudication/`).

## What changed against 18b
- **Reconciliation completed.** When 18b was frozen the Codex workspace had run out of credits and the gpt-6-astra
  reconciler had covered 25 items (round 1, batches R01-R08, 2026-10-18 01:59-02:13 UTC); the other 95 had the
  deterministic L1 + L2 merge rule. After the credit reset the reconciler did those 95 (round 2, R09-R30, 09:05-09:45
  UTC). Every item's `adjudicator` is now `astra-reconciler`, and `adjudication.reconciliation_round` says which
  round; the merge rule is used for no item (it stays in 18b as history). The 25 round-1 items' reconciled labels
  are 18b's, unchanged. On the 95 round-2 items, merge -> Astra (group nodes included): 49 rows removed, 10 added, 76
  confidence changes, 158 rows with a role-strength change (275 role cells); slug F1 merge vs Astra 0.970 (group
  nodes out). Computed by `derive.py` from 18b's `pre-verification.jsonl` and `adjudication/astra-reconciler.jsonl`
  (`manifest.json` `derivation.merge_to_astra_round2`).
- **Verification round 2.** `adjudication/scripts/disputes2.py` recomputed the disputes of the reconciled gold
  against the blind Opus labels on the same 30-item sample (122 disputed slugs on the 30 items) and sent 63 to a
  Codex gpt-6-sol adjudicator: 12 newly disputed after the reconciliation, 41 Sonnet `borderline` verdicts and 10 on
  which Sonnet's two runs of one batch disagreed. P / Q blinding as in round 1 with seed 20261021 (`swap` true = gold
  shown as Q). The other 59 keep their round-1 Sonnet verdict (`../2026-10-18b/adjudication/verification.jsonl`).
  Codex verdicts: on gold slugs 22 yes / 7 no / 1 borderline, on Opus-only slugs 6 yes / 25 no / 2 borderline
  (`adjudication/verification2.jsonl`; parsed by `parse_adj2.py`). Batches A1-A3 (`round2-batches.json` with the
  packet sha256 lists); each batch prompt is 18b's `prompts/VERIFY-ADJ.md` with its items filled in.
  `verify_apply2.py` applies the verdicts with round 1's rules (yes on an Opus-only slug -> added, `sources:
  ["opus"]`; no -> removed into `dropped`; borderline -> `dont_care`; a better slug -> added with `sources:
  ["verification"]` when absent, once here). Its docstring still carries round 1's wording in places (G0, `labels/final`);
  the code reads the reconciled gold and writes the round-2 labels.
- **labelers**: astra-L1, astra-L2; the 30 verified items add opus, sonnet-adjudicator and codex-sol-adjudicator (5
  of them had no slug sent to Codex).
- Against 18b's gold: 12 rows added, 71 removed, 1,118 kept; `dont_care` rows 41 -> 3.

## Counts
120 items; 1,130 rows: 1,064 scored (group nodes and `dont_care` out), 829 high confidence, 947 given by both
labellers; 3 `dont_care`, 63 group-node rows, 8 rows with `level_alternatives`. Row sources A+B 1,000, A only 54, B
only 51, reconciler 12, opus 12, verification 1. 141 extra techniques, 257 menu gaps. Every quote is found verbatim
(whitespace-normalised), no row has a role above `mentioned`, every level alternative is linked to its slug.

## Estimates (`adjudication/estimates2.json`)
Truth on the sample as in round 1 (slugs both the gold and Opus gave, level-alternative matches included, plus
adjudicated yes and better slugs; borderline excluded; group nodes out; 10,000 document bootstrap). The reconciled
gold before round 2: precision 0.935 [0.908, 0.961], recall 0.974 [0.958, 0.991] (an upper bound: misses neither the
labellers nor Opus found are invisible); 18b's merge-based gold had 0.942 / 0.976 in round 1. Precision by
confidence: high 0.972 (252 rows), med 0.803 (71). Opus alone: precision 0.882 [0.826, 0.926], recall 0.842 [0.786,
0.884]. 3 slugs are `dont_care` (round 1: 37).

## Reproduced when freezing
On copies of the workspace inputs: `disputes2.py` reproduces `key2.json`, `parse_adj2.py` reproduces the Codex verdict
files, and `verify_apply2.py` reproduces the labels of record (120 of 120) and `estimates2.json`. The scripts write
files, so `stage.py` does not run them; it checks that the sample, the Opus labels, the Sonnet verdicts and the prompt
are 18b's, and that the Codex verdicts follow the key. The copied `disputes2.py` has one line removed (its
leak-check pattern names workspace paths and a person), so it does not run as copied; `packet.json` keeps both
sha256.

## Known biases
- The gold was built by GPT-family labellers and reconciler (gpt-6-astra) and checked on 30 items by Claude models
  (Opus labels, Sonnet adjudication) and by gpt-6-sol (round-2 adjudication).
- Recall estimates are upper bounds; Sonnet's verdicts were not fully stable across runs, which is why its borderline
  and unstable verdicts went to the second adjudicator.
- The stub and weak texts of 18b remain (g2-0103, g2-0104, g2-0061; six flagged texts; g2-0008 in German).

## Taxonomy v0.9.1 (the production catalogue, D640)
The set stays frozen on `taxonomy/taxonomy-v0.9-234.yaml` (`frozen_with`, `derive.py` and the tests pin its
sha256); production reads `taxonomy/taxonomy-v0.9.1.yaml`. The two files differ only in 23 `origin` / `note` cells
and the header's redaction count, fields no rendering shows. Checked on 2026-10-20: the full menu, the compact
index, the sub-menu of all 243 slugs, the system part of every `technique-staged-v3` call (single, prepass, select,
grade, sweep) and `main.freeze_info` of all 120 items (segmentation, lexical and user digests) are byte-identical
under both files. So the set is valid for the production pin without a re-freeze: run it with `--pin
taxonomy=taxonomy/taxonomy-v0.9.1.yaml`; the input check passes and the run carries one digest warning
(`frozen_taxonomy_digest` 07017821efe1 vs `taxonomy_digest` 09fa7705b7ba), which this check settles. A taxonomy
change that alters a slug, name, definition, alias, parent or group node needs a new gold revision.

## Results
The production configuration (D640: mode `single`, prompt set `technique-staged-v3`, role `extract_staged`,
`openai/gpt-6-luna`, taxonomy v0.9.1), `--split all`, 1 repeat:

    uv run --no-sync stack --plan configs/plans/ann-scratch.yaml eval technique_staged --set 2026-10-18c --split all \
        --models openai/gpt-6-luna --prompt technique-staged-v3 --pin mode=single --pin role=extract_staged \
        --pin taxonomy=taxonomy/taxonomy-v0.9.1.yaml --max-usd 1.95

(`--max-usd` must cover the no-cache reservation, $1.92.) Slugs without group nodes, full gold:

| run | strict P / R / F1 | tolerant P / R / F1 | len-weighted R strict | cells ≥ 2 F1 | USD (run) | USD / 1k docs actual / len-weighted | p50 s |
|---|---|---|---|---|---|---|---|
| 18b luna arm rescored on 18c (taxonomy 234, 2 repeats pooled, 2026-10-18) | 0.902 / 0.577 / 0.704 | 0.910 / 0.582 / 0.710 | 0.552 | - | - | - / 7.05 (production-weighted) | 56 |
| `20261020T210409Z_all_89829b05` (taxonomy v0.9.1, 2026-10-20) | 0.898 / 0.565 / 0.694 | 0.910 / 0.569 / 0.700 | 0.533 | 0.564 | 0.585 | 4.88 / 5.96 | 51 |

Both-labeller gold 0.896 / 0.618 / 0.731, high-confidence gold 0.890 / 0.665 / 0.761. 120 of 120 answered, none
failed; all 120 calls `schema_degraded` (strict-schema refusal, then the permissive path); prompt-cache share 0.71.
The difference to the 2-repeat baseline (F1 −0.010) is within one repeat's noise (not measured here).

Error classes (computed from `rows.jsonl` against gold; 1,064 scored rows, 601 found, 68 false-positive slugs): the
errors are misses (463 vs 68). Recall by gold confidence high 0.665 (829 rows), med 0.213 (235); by the gold row's
strongest role beyond `mentioned` / `cited_work`: 4 → 0.80, 3 → 0.71, 2 → 0.48, 1 → 0.52, mention/citation only 0.47;
rows from one labeller only 0.08 (A, 51) and 0.17 (B, 46). By length band recall <20k 0.69, 20-60k 0.64, 60-100k
0.51, >100k 0.49; by role, `introduced` (R 0.17) and `critiqued` (0.28) are weakest. Most-missed slugs:
judge-rm-verifier-robustness-audit 15, safety-sft-refusal-training 14, automated-red-teaming 11,
eval-realism-deployment-simulation 10, jailbreak-robustness-eval 10, least-privilege-minimal-tool-access 10. Most
frequent false positives: model-weight-security-exfiltration-controls 5, activation-steering-repe 4,
input-output-content-safety-classifier 4, human-red-teaming 4.

### Other grading models (2026-10-21)
Same configuration, `--models` changed. Gemini's full-set reservation ($14.4; expected $8.6 without cache) is over a
$2 per-run cap, so it ran on 40 items (every third line of `items.jsonl`; bands <20k 14, 20-60k 12, 60-100k 6, >100k
8) in three `--items` parts, then one combined `--items` run from the response cache; luna, haiku and mimo are
re-scored on the same 40 from their caches. mimo needs a local `llm.yaml` price (0.14 / 0.28) and an overlay route
`xiaomi: deepinfra` (open / public_web), neither committed. Strict slugs, full gold:

| model | items | run | strict P / R / F1 | tolerant P / R / F1 | len-w R | cells ≥ 2 F1 | USD / 1k docs | schema degraded | failed |
|---|---|---|---|---|---|---|---|---|---|
| gpt-6-luna | 40 | `20261021T002950Z_all_f0b845be` (cache) | 0.913 / 0.610 / 0.732 | 0.925 / 0.613 / 0.738 | 0.587 | 0.599 | 4.88 (120) | 40 | 0 |
| google/gemini-3.8-flash | 40 | `20261021T002701Z_all_fe95446f` (parts `ace490f1`, `de551a8f`, `9c0ba655`) | 0.853 / 0.567 / 0.681 | 0.869 / 0.569 / 0.688 | 0.539 | 0.483 | 39 standalone, ~59 with cut-off retries | 1 | 0 (2 before re-run) |
| anthropic/claude-haiku-5.5 | 120 | `20261021T000858Z_all_d82991e2` | 0.729 / 0.477 / 0.577 | 0.748 / 0.488 / 0.590 | 0.433 | 0.434 | 15.87 | 119 | 1 schema_invalid |
| xiaomi/mimo-v2.6-flash | 120 | `20261021T001553Z_all_86ff873c` | 0.609 / 0.461 / 0.525 | 0.639 / 0.471 / 0.542 | 0.444 | 0.375 | 3.12 | 0 | 2 truncated |

Paired bootstrap vs luna (`staged-compare`, 10k): Gemini (40) R −0.043 [−0.169, 0.084], P −0.060 [−0.105, −0.015],
F1 −0.050 [−0.149, 0.038]; haiku (120) F1 −0.117 [−0.159, −0.081]; mimo (120) F1 −0.168 [−0.215, −0.129]. Gemini on
the 40: recall <20k 0.80 (luna 0.67), 20-60k 0.46 (0.63), >100k 0.62 (0.57); its output reached the 16,000-token cap
on the first attempt for 10 of 42 documents (two failed the 32,000-token retry too), mostly forum / url documents
under 60k. Gemini and mimo accept the strict schema; haiku, like luna, falls back on every call. haiku's tokenizer
counts 1.6x luna's prompt tokens and prices prompts over 100k tokens at 5x. Verdict: luna stays.

Round 2 (2026-10-21), the same 40 items for every model (paired with the luna and Gemini rows above). Prices for
gemini-3.1-flash-lite, deepseek-v4.1-flash-1017, nemotron-3.5-super-vl and mercury-3 and the routes `nvidia: deepinfra`,
`inception: inception` (eval-only, open / public_web) were local, not committed. Gemini low effort = `--pin
role=extract_staged_low`. "trunc" = rows whose first attempt hit the 16,000-token cap; failed rows score as empty.

| model | run | strict P / R / F1 | tolerant P / R / F1 | len-w R | R high | cells ≥ 2 F1 | complete docs P / R / F1 (n) | failed | trunc | USD / 1k (standalone) | p50 s | F1 − luna [95 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gemini-3.8-flash, effort low | `20261021T011018Z_all_3b85dcbf` | 0.755 / 0.586 / 0.660 | 0.778 / 0.587 / 0.670 | 0.553 | 0.674 | 0.404 | (40) | 0 | 0 | 19.5 | 12 | −0.072 [−0.115, −0.029] |
| openai/gpt-6-luna-pro | `20261021T005121Z_all_9326e30f` | 0.916 / 0.601 / 0.726 | 0.923 / 0.606 / 0.731 | 0.583 | 0.705 | 0.599 | (40) | 0 | 0 | 14.2 | 65 | −0.006 [−0.033, 0.021] |
| deepseek/deepseek-v4.1-flash-1017 | `20261021T005121Z_all_07c958ac` | 0.847 / 0.644 / 0.732 | 0.856 / 0.649 / 0.738 | 0.581 | 0.740 | 0.614 | (40) | 0 | 1 | 5.85 | 134 | 0.000 [−0.064, 0.057] |
| inception/mercury-3 | `20261021T005121Z_all_5747bff9` | 0.848 / 0.325 / 0.470 | 0.848 / 0.328 / 0.473 | 0.245 | 0.353 | 0.316 | 0.848 / 0.631 / 0.724 (32) | 8 | 25 | 13.5 | 99 | −0.262 [−0.429, −0.089] |
| nvidia/nemotron-3.5-super-vl | `20261021T005121Z_all_450f1b8b` | 0.776 / 0.202 / 0.321 | 0.805 / 0.204 / 0.325 | 0.152 | 0.236 | 0.212 | 0.776 / 0.485 / 0.597 (27) | 13 | 31 | 23.1 | 106 | −0.410 [−0.547, −0.266] |
| google/gemini-3.1-flash-lite | `20261021T005121Z_all_79e8e84b` | 0.370 / 0.215 / 0.272 | 0.389 / 0.215 / 0.277 | 0.195 | 0.244 | 0.132 | 0.370 / 0.275 / 0.315 (33) | 7 | 17 | 28.0 | 10 | −0.460 [−0.548, −0.373] |

ds-1017: recall +0.034 [−0.062, 0.122], precision −0.066 [−0.127, −0.016]; luna-pro equals luna on every bootstrapped
metric. The role's luna fallback answered one document in the luna-pro (g2-0100), ds-1017 (g2-0115) and flash-lite
(g2-0061) runs, and in round 1's Gemini run (g2-0037: its one schema-degraded call is that luna answer). Median
reasoning tokens per call: nemotron 14.7k, mercury 13.9k, ds-1017 11.9k, luna-pro 7.6k: the cap failures of nemotron
and mercury follow from their default effort.

Round 3 (2026-10-21; local prices / routes as in round 2; low effort = `--pin role=extract_staged_low`):

| model | docs | run | strict P / R / F1 | tolerant P / R / F1 | len-w R | R high | cells ≥ 2 F1 | complete docs P / R / F1 (n) | failed | USD / 1k (standalone) | F1 − luna [95 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek/deepseek-v4.1-flash-1017 | 120 | `20261021T013225Z_all_650c714e` | 0.821 / 0.555 / 0.663 | 0.835 / 0.560 / 0.670 | 0.513 | 0.628 | 0.530 | 0.821 / 0.592 / 0.688 (117) | 3 truncated | 6.88 | −0.031 [−0.094, 0.023] vs `89829b05` |
| inception/mercury-3, effort low | 40 | `20261021T015158Z_all_c16937b8` | 0.820 / 0.574 / 0.675 | 0.838 / 0.578 / 0.684 | 0.555 | 0.651 | 0.470 | 0.820 / 0.625 / 0.710 (39) | 1 truncated | 9.06 | −0.057 [−0.156, 0.014] |
| nvidia/nemotron-3.5-super-vl, effort low | 40 | `20261021T015158Z_all_4af8c6f1` | 0.709 / 0.120 / 0.205 | 0.754 / 0.120 / 0.207 | 0.086 | 0.136 | 0.113 | 0.709 / 0.364 / 0.481 (23) | 9 truncated + 8 not attempted (budget) | 18.5 | −0.527 [−0.651, −0.381] |

deepseek-1017 on 120: precision −0.078 [−0.107, −0.050], recall −0.009 [−0.089, 0.070]; the 40-item tie above does not
hold on the full set. mercury low: precision −0.093 [−0.154, −0.036].

## Scripts
- `uv run --no-sync python evals/technique_staged/2026-10-18c/scripts/stage.py <WORKSPACE> [--write]` stages
  `adjudication/` from the labelling workspace (read only; its path is not recorded).
- `uv run --no-sync python evals/technique_staged/2026-10-18c/scripts/derive.py [--write]` builds the set from 18b and
  `adjudication/`, and without `--write` checks it byte for byte.
