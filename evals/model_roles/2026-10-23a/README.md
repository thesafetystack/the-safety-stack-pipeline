# model_roles eval set 2026-10-23a: gold v3 sample (100 documents, input-only, no `gold.jsonl`)

The 100 documents of `evals/technique_staged/2026-10-23a` (same item ids g3-0001..g3-0100, same texts, same sample,
weights and splits; sampling, exclusions and the allocation table are in that README), frozen for `stack eval
model_roles` with vocabulary v0.9 (`configs/models.yaml` sha256 `ce35fb957299…`) and prompt `model-roles-v2`, as
2026-10-17a. There is no `gold.jsonl`: the set was labelled blind by strong agentic labellers (Opus 5.5, Sonnet 5.5,
Sol 6.1, two repeats each) and the production prompt `model-roles-v3b` was chosen on those labels (D708); the labels
are not in this repository.

## Where the set lives
No text is in the repository (the sample includes `restricted` texts). This directory holds `items.jsonl` and
`manifest.json` (byte-identical to the private copy) and the labelling material (`labelling/`); the complete set
(plus `texts/` and an empty `gold.jsonl`) is in a private evals root, written by
`evals/technique_staged/2026-10-23a/scripts/freeze.py`. Run:

    uv run --no-sync stack --plan configs/plans/ann-scratch.yaml eval model_roles --set 2026-10-23a \
        --evals <PRIVATE_EVALS_ROOT> --split all --models openai/gpt-6-luna --max-usd 2.0

(prompt `model-roles-v2` and role `extract_main` are the defaults; no-cache reservation $1.14).

## Items
As 2026-10-17a: `input.model_mentions` (the scan), `input.menu` / `input.beyond_window`, `input.fills_digest`,
`input_digest` = sha256 of the `model-roles-v2` user message, `production_asks` true; plus `stratum` and `weight` as
the technique set. Menus: 906 entries over 100 items (median 5.5, max 73); 22 empty, 5 weak-only; 2 items have
matcher hits beyond the 300,000-character window (g3-0051, g3-0100, which the judge sees truncated).
`split.json`: the dev / test split shared with the technique set (60 / 40, stratified by source kind x length
band; written by `evals/technique_staged/2026-10-23a/scripts/split.py`; not `items.jsonl`'s `split` field).

## Labelling material (`labelling/`)
- `GUIDE.md`: the 2026-10-13a labelling protocol (whose labels, remapped, are 2026-10-17a's gold) for this task only,
  blind, brought to the production judge's rules (rubric 0.6.0 as `model-roles-v2` includes it: every document
  judged, empty / weak-only menus, refinement, variant rows, extra models). Output per document = 2026-10-17a's
  `label` shape (`mentions`, `mentions_out_of_vocab`, `models` with all 10 roles and a quote; `extra_models` maps to
  `expected_extras` and `off_menu_models`; a refined row grades `refined_node_id`), plus per row `confidence`, `flag`
  and the variant rows.
- `INSTRUCTIONS.md` (agentic procedure), `schema.json`; the checker is
  `evals/technique_staged/2026-10-23a/labelling/check_labels.py` (`model_roles` mode: one row per menu node, node ids
  in the catalogue, quotes verbatim, flags on all-zero rows). The packet adds `rubric-v0.6.0.md`, `vocab-index.tsv`
  (from `configs/models.yaml`) and per document the production menu text (`render.menu_text` of `main.fills`).

## Results
luna, production configuration (the command above), 2026-10-23, `20261023T181614Z_all_4308aaa1`: 100 of 100 answered
(76 ok, 24 empty), none schema-degraded, $0.475 settled (estimate $0.41 expected, $1.14 reserved). Model rows per
document: median 5.5 (one per menu entry); rows with a role >= 1: 667 (mean 6.7 per document), >= 2: 375; extra models
11 on 5 documents; 18 variant rows. Not scored (no gold).
