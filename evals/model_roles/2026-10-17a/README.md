# model_roles eval set 2026-10-17a: every document judged, prompt model-roles-v2

The current model_roles set, for `annotate.model_roles` registry version 3: every document in scope gets a judge
call (an empty or weak-only menu included), prompts are files (`model-roles-v2` the default), and the judge may
refine a family, weak or product-name row to the specific model the document determines (`refined_name`). Earlier
sets stay as they were: `2026-10-13a` (the labelling record) and `2026-10-16a` (the merge, D591; refused by the
current code: its items were frozen without calls and with the older menu text).

## Set (37 items, dev/test 21/16)
- **an-0001..an-0025**: 2026-10-16a's documents, texts byte-identical (the 2026-10-13a selection: 25 screened-in
  documents).
- **an-0026..an-0030**: real texts from `evals/screen/2026-10-12a` with no matcher hit and no named AI model (read
  by the set's author; generic phrases such as "19 frontier models" are not models): scr-0087, scr-0006, scr-0054,
  scr-0042, scr-0040. The judge should return no models and no extras.
- **an-0031..an-0034**, synthetic (`synthetic/syn-0*.md`, `synthetic: true`): realistic prose about models with
  fictional names, none in the catalogue (v0.5, v0.6 or v0.7): Zephyrine-9B and Marrowind-3 (a model release paper),
  Corvalis-2 and Brambling-7B (a red-teaming post), Orrery-Pro (commentary), Kestrel-1.3B (an interpretability
  paper). Empty menu; the judge should list them under `extra_models`.
- **an-0035..an-0037**, synthetic concretization (`synthetic/ref-0*.md`): the matcher finds only a family (or a weak
  form); an-0035 "ChatGPT version 4" → gold on `gpt-4`; an-0036 "Claude, in its 3.5 Sonnet version" → gold on
  `claude-3.5-sonnet`; an-0037 a lone ChatGPT dated "early 2024", which does not determine the version → gold stays
  on `gpt` (the negative).

11 items have an empty menu (an-0024, an-0025 and an-0026..an-0034).

Frozen by `scripts/freeze.py` (no LLM call) with vocabulary **v0.9** (`configs/models.yaml` sha256 `ce35fb957299…`,
re-frozen in place on 2026-10-18 after the v0.9 catalogue, D598 / D623; v0.7 `999f6a332989…` on 2026-10-17, D595;
first frozen with v0.5 `7672a43bac3a…`) and prompt
`model-roles-v2` (`manifest.json`). Per item: `input.model_mentions` (the scan, for `stack eval
model_mentions`), `input.menu`/`input.beyond_window`, `production_asks` (true for all), `input.fills_digest` (the
document window, truncation note and menu text, prompt-independent) and `input_digest` (the whole user message under
`model-roles-v2`). `stack eval` refuses the set when production builds other fills; the whole message is compared
only for `model-roles-v2`, so `--prompt model-roles-v1` (or a new version) runs on the same set.

**Catalogue v0.5 → v0.7** (the re-freeze): menus changed on 16 of 37 items.
- *Nodes* (4 items): an-0001 nllb-200 → nllb (NLLB-200 folded into the family); an-0017 + llama-fine-tunes,
  qwen3.5-9b, qwen3.6-27b, qwen3.6-35b-a3b, − alpaca-7b, qwen3.5-397b-a17b (Alpaca removed; Qwen sizes split out);
  an-0019 − kimi-k3-swarm ("K3 Swarm" now an alias on kimi-k3); an-0021 qwen3.6 → qwen3.6-35b-a3b.
- *Text only* (12 items: an-0002, -0003, -0004, -0005, -0007, -0009, -0010, -0011, -0015, -0016, -0022, -0023):
  names, makers (DeepMind models now `google`) or glosses changed; no node moved.
- *Score risk*: the gold follows every node change (the v0.7 remap below), so no gold row is off its menu. Left
  by design: an-0035 / an-0036 (the concretization items: the menu has the family, gold the refined node; a judge that
  does not refine scores a spurious family row and misses the gold row). Fixed in this re-freeze: an-0022 (asked since
  2026-10-17a, but its weak gpt-6-astra and janus entries had no gold rows: added, see below).

**Catalogue v0.7 → v0.9** (the second re-freeze, in place; no LLM call): menus changed on 17 of 37 items.
- *Nodes* (10 items): an-0001 − nllb (node removed, no alias); an-0010 qwen3.8-27b → qwen3.8 (the stub is gone, "qwen
  3.8 27b" is an alias on the group); an-0017 llama-fine-tunes → alpaca-7b (Alpaca-7B is a node again, D623), − albert-1,
  deepseek-r1 (weak aliases dropped); an-0019 embeddinggemma-2 → gemma, nano-banana-2.1 → nano-banana, − bloom-176b;
  an-0007 − luma-ray, stable-diffusion; an-0018 − albert-1; an-0020 − elevenlabs, mai, svd; an-0021 − bloom-176b,
  deepseek-r1; an-0006 (codex, yolo-1) and an-0016 (glm) keep their nodes but become weak entries (their aliases were weakened).
- *Text only* (7 items): an-0002, -0004, -0005, -0009, -0011, -0012, -0015 (names, makers or glosses).
- The Vicuna / Zephyr nodes (D623) move no gold row: the set's only gold on those families was Alpaca (an-0017).
- *Score risk*: an-0035 / an-0036 as before (concretization items); an-0001 loses its NLLB-200 grade (below), so its
  in-vocabulary gold has four models. an-0019 and an-0010 now grade the group-level node (`gemma`, `nano-banana`,
  `qwen3.8`) where the text names a specific version: a judge that refines to the version (`refined_name`) is not
  penalised for the family row, but there is no version node to grade.

Re-freezing after a catalogue change: `uv run --no-sync python evals/model_roles/2026-10-17a/scripts/freeze.py
[<candidate models.yaml>]`; with a candidate file it also writes `candidate-moves.jsonl` (the items whose menu, nodes
or text, would change), so the next re-freeze is a known step.

## Gold
`gold.jsonl` = 2026-10-16a's gold (2026-10-13a's v0.5-aligned labels) with `gold-remap.jsonl` applied, plus
`gold-new.jsonl` for an-0026..an-0037.

**The remap** (`gold-remap.jsonl`, 64 entries, adjudicated by the set's author on 2026-10-17, each with its reason
in the file and in the row's `note`; quotes checked verbatim). First the 35 entries for the 8 items whose menus moved
under v0.5:
- *Moves* (14): rows the labellers had graded on an older sibling for the newer model the text names, per their own
  notes ("text names GLM 5.3 (no vocab node) … graded here per variant rule"), moved to that model's node with their
  roles and quote: an-0005 claude-opus-5 → claude-opus-5.5, gpt-5.6-sol → gpt-6.1-sol; an-0010 glm-5 → glm-5.3,
  nemotron-3 → nemotron-3.5-lightning-30b-a3b, qwen3.8-max → qwen3.8-27b; an-0019 claude-opus-5 → claude-opus-5.5,
  claude-sonnet-5 → claude-sonnet-5.5, deepseek-v4 → deepseek-v4.1-flash, embeddinggemma → embeddinggemma-2,
  gemini → gemini-4-argon, glm-5 → glm-5.3, grok-4 → grok-4.6, mistral-large → mistral-large-4, nano-banana-2 →
  nano-banana-2.1.
- *Adds* (17), graded from the text where gold had only an `off_menu_models` name or had folded the model into
  another row: an-0003 claude-opus-5.5 (evaluated 2: the benchmark's one named result); an-0005 jev-1.13 (evaluated
  4: the subject), jev all-zero (shorthand for Jev 1.13); an-0010 inkling-small (modified 2, evaluated 2, as its
  siblings), claude-opus-5.5 (cited 1: a system-card excerpt); an-0019 claude-haiku-5.5 (discussed 2, cited 1),
  gpt-6 (discussed 2), claude-fable-5.1 (discussed 1), gpt-6.1-sol (discussed 1), kimi-k3-swarm (cited 2),
  gpt-6-astra (discussed 2, cited 1; bare "Astra" is the model there); an-0021 inkling (modified 4, evaluated 4);
  an-0023 gpt-6-astra (usage_experience 3); all-zero rows for the weak false positives gpt-6-astra on an-0010,
  an-0011, an-0017 ("Astra Fellowship", a monitor named Astra) and opt on an-0023 ("opt into").
- *Sets* (2), anti-bleed after an add: an-0019 claude-haiku (left: the generic "Haikus") and kimi-k3 (left: the
  compute comparison).
- *Drops* (2): all-zero rows no longer on the menu (an-0003 claude-opus-5, an-0005 gpt).

Then `section: v0.7` (10 entries, catalogue v0.7 folds and removals, D595): an-0001 nllb-200 → nllb; an-0017
alpaca-7b → llama-fine-tunes (still all-zero, bib_only), qwen3.5-397b-a17b → qwen3.5-9b (the row's note: the text
names Qwen3.5-9B), qwen3.6 → qwen3.6-35b-a3b with the roles of its experiments (modified 4, evaluated 4, instrument
2; internals moved to) qwen3.6-27b (added: steering-vector teacher and fine-tuned student, modified / evaluated /
internals 3), qwen3.6 kept all-zero (figure-label shorthand), a gold mention for Qwen3.6-27B; an-0019 kimi-k3-swarm
merged into kimi-k3 (roles by max: cited 2); an-0021 qwen3.6 → qwen3.6-35b-a3b. And `section: always-call review`
(2): an-0022 gpt-6-astra mentioned 1 ("all Astra"; gold's own mention list), janus all-zero (a person).

Then `section: v0.9` (17 entries; catalogue v0.9, D598 / D623), each with its reason in the file and the row's `note`:

| item | v0.7 gold id | v0.9 id | why |
|---|---|---|---|
| an-0001 | nllb (instrument 2, mentioned 2) | none: `off_catalogue` | node removed, no NLLB alias; row and mention leave the in-vocabulary gold, "NLLB-200" kept in `mentions_out_of_vocab` and `off_menu_models` (no `expected_extras`: the item's other off-catalogue models were never enumerated) |
| an-0010 | qwen3.8-27b (row + mention) | qwen3.8 | stub removed; alias "qwen 3.8 27b" on the group |
| an-0019 | embeddinggemma-2 (row + mention) | gemma | stub removed; alias "embedding gemma 2" on the family (the matcher's menu entry) |
| an-0019 | nano-banana-2.1 (row + mention) | nano-banana | stub removed; alias "nano banana 2.1" on the group |
| an-0019 | gpt-6.1-luna (mention "Luna") | gpt-6.1 | stub removed; alias "gpt 6.1 luna" on the group (mention only; gpt-6.1 is not on the menu, the row stays on gpt-6.1-sol) |
| an-0017 | llama-fine-tunes (Stanford alpaca, all-zero) | alpaca-7b | D623: own node; same bib_only reading |

and 11 `drop` entries: all-zero rows whose node left the menu (an-0007 luma-ray, stable-diffusion; an-0017 albert-1,
deepseek-r1; an-0018 albert-1; an-0019 bloom-176b; an-0020 elevenlabs, mai, svd; an-0021 bloom-176b, deepseek-r1).

Gold mentions (for `stack eval model_mentions`) follow moves and merges.

**New items' gold**: no model rows for the no-model and off-catalogue items; `label.expected_extras` lists the
off-catalogue models the judge should report (by name, with aliases; empty for the no-model items) — the scorer's
`extras` block (`stack/evals/score.py`) measures extras precision/recall by loose name match and counts items with a
spurious menu grade. The concretization items carry gold rows on the specific node (an-0037 on the family).

## Run
From the repo root (the eval verbs need a plan; the prompt is `--prompt`, default `model-roles-v2`):

    uv run --no-sync stack --plan configs/plans/ann-scratch.yaml eval model_roles --set 2026-10-17a --split all \
        --models openai/gpt-6-luna [--prompt model-roles-v1] [--estimate]

A/B of the prompt versions: run once with `--prompt model-roles-v2` and once with `--prompt model-roles-v1` (same
set, same menus). The matcher alone (no LLM): `uv run --no-sync stack eval model_mentions --set 2026-10-17a --split
all`.

## Results
Baseline runs with the production pin (`configs/plans/ann-scratch.yaml`: `model-roles-v2`, `openai/gpt-6-luna`,
role `extract_main`), `--split all`, 2 repeats, the README's command. Cells = (node, role) at strength ≥ 2, micro over
(item, repeat).

| catalogue | run | cells≥2 P / R / F1 | cells≥1 F1 | nodes F1 | extras F1 | menu spurious | repeat agree | USD / 1k docs | run USD |
|---|---|---|---|---|---|---|---|---|---|
| v0.7 (2026-10-17) | - | 0.820 / 0.760 / 0.789 | 0.912 | 0.974 | 1.000 | 0 | n/r | n/r | 0.092 (42 of 74 rows from the response cache) |
| v0.9 (2026-10-20) | `20261020T211236Z_all_27ecf7e1` | 0.838 / 0.749 / 0.791 | 0.909 | 0.976 | 1.000 (24) | 0 | 70.3% | 1.96 | 0.145 (no cache) |

v0.9: input check passed for all 37 items (the run reported no refusal; the vocabulary digest equals the frozen one).
Per role at ≥ 2 (tp / fp / fn over both repeats): mentioned 128 / 16 / 40, cited_work 14 / 3 / 24 (recall 0.37, the
weakest role), evaluated_behavioral 72 / 10 / 14, modified 23 / 0 / 9, discussed 17 / 17 / 7 (precision 0.50),
instrument 38 / 5 / 6, introduced 0 / 0 / 4, built_on 4 / 0 / 4, usage_experience 5 / 3 / 1, analyzed_internals
24 / 9 / 0. Items with most errors: an-0010 (40 / 0 / 36: the judge grades fewer of the many named models at ≥ 2),
an-0019 (50 / 27 / 14), an-0018 (39 / 18 / 1), an-0002 (0 / 0 / 14). Against v0.7: F1 +0.002, precision +0.018,
recall −0.011, within the repeat noise (agreement 70%). Gold is LLM-built (no human pass).

### Other models (2026-10-21)
Same command, `--models` changed. Gemini reserves $0.075 per call, so one `--split all` repeat is over a $2 per-run
cap: repeat 1 ran as `--split dev` and `--split test`, scored together from the response cache; repeat 2 only on the
first 10 test items (`--limit 10`). mimo needs a local `llm.yaml` price (0.14 / 0.28) and an overlay route
`xiaomi: deepinfra`, neither committed.

| model | rows | run | cells≥2 P / R / F1 | cells≥1 F1 | nodes F1 | repeat agree | USD / 1k docs | run USD | schema degraded |
|---|---|---|---|---|---|---|---|---|---|
| gpt-6-luna, repeat 1 only (cache) | 37 | `20261021T002606Z_all_cf45d098` | 0.819 / 0.770 / 0.793 | 0.899 | 0.970 | - | 1.96 | 0 | 0 |
| google/gemini-3.8-flash, repeat 1 | 37 | `20261021T002413Z_all_eee507b1` (= `..._dev_bff0fbd2` + `..._test_83bc66d6`) | 0.886 / 0.894 / 0.890 | 0.927 | 0.969 | - | 32.5 | 1.431 | 0 |
| google/gemini-3.8-flash, 2 repeats, 10 test items | 20 | `20261021T003134Z_test_31056e83` | 0.853 / 0.779 / 0.814 | 0.891 | 0.953 | 70.0% | 46.0 | 0.460 | 0 |
| gpt-6-luna, same 10 test items, 2 repeats (cache) | 20 | - | 0.904 / 0.721 / 0.802 | 0.892 | 0.978 | 70.0% | - | 0 | 0 |
| anthropic/claude-haiku-5.5, 2 repeats | 74 | `20261021T001621Z_all_5660d8c1` | 0.789 / 0.864 / 0.825 | 0.894 | 0.972 | 64.9% | 10.04 | 0.743 | 74 |
| xiaomi/mimo-v2.6-flash, 2 repeats | 74 | `20261021T002430Z_all_91927d5d` | 0.348 / 0.694 / 0.464 | 0.351 | 0.863 | 37.8% | 1.30 | 0.096 | 0 |

Paired item bootstrap of cells≥2 F1 (10k): luna repeat 2 − repeat 1 −0.005 [−0.051, 0.049]; Gemini repeat 1 − luna
repeat 1 +0.097 [0.043, 0.146] (− luna repeat 2 +0.102 [0.028, 0.157]; without an-0010 +0.080 [0.031, 0.127]); haiku
− luna +0.039 [−0.018, 0.092] (repeat 1). The Gemini gain is on the dev split's dense multi-model items (dev F1 0.916
vs luna 0.799 / 0.772; test 0.808 vs 0.776 / 0.837): an-0010 tp / fp / fn luna 20 / 0 / 18 in both repeats vs Gemini
37 / 8 / 1; an-0018 false positives 9 → 1. Gemini accepts the strict schema (0 degraded); prompt-cache share 0 % on
this task. mimo: 244 repairs, precision 0.35.

Round 2 (2026-10-21; local prices / routes as in the technique set README; Gemini low effort = a local `extract_main`
with `reasoning_effort: low`). F1 − luna = paired item bootstrap against luna's same repeat on the same items.

| model | rows | run | cells≥2 P / R / F1 | cells≥1 F1 | nodes F1 | repeat agree | failed | USD / 1k | F1 − luna [95 % CI] |
|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna-pro, 2 repeats | 74 | `20261021T005210Z_all_9d57182e` | 0.866 / 0.728 / 0.791 | 0.913 | 0.983 | 81.1% | 0 | 5.50 | r1 −0.019 [−0.069, 0.033], r2 +0.019 [−0.023, 0.055] |
| deepseek/deepseek-v4.1-flash-1017, 2 repeats | 74 | `20261021T005210Z_all_c388e043` | 0.810 / 0.696 / 0.748 | 0.574 | 0.805 | 61.1% | 2 | 3.13 | r1 −0.089 [−0.334, 0.103], r2 −0.001 [−0.103, 0.081] |
| inception/mercury-3, 2 repeats | 74 | `20261021T005210Z_all_0690154f` | 0.915 / 0.419 / 0.575 | 0.551 | 0.603 | 66.7% | 10 (truncated) | 7.49 | r1 −0.079 [−0.277, 0.101], r2 −0.385 [−0.579, −0.177] |
| nvidia/nemotron-3.5-super-vl, 2 repeats | 74 | `20261021T005210Z_all_1e139c6f` | 0.802 / 0.208 / 0.330 | 0.343 | 0.497 | 65.2% | 15 (truncated) + 3 not attempted (budget) | 12.1 | r1 −0.491, r2 −0.424 (CIs exclude 0) |
| google/gemini-3.1-flash-lite, repeat 1 | 37 | `20261021T010841Z_all_3bbed08a` | 0.596 / 0.788 / 0.679 | 0.620 | 0.902 | - | 0 | 5.98 | −0.115 [−0.222, 0.024] |
| google/gemini-3.8-flash effort low, repeat 1, first 16 dev items | 16 | `20261021T010920Z_dev_83156a35` | 0.873 / 0.824 / 0.848 | 0.937 | 0.975 | - | 0 | 21.1 | +0.054 [−0.003, 0.133] (Gemini default on the same items 0.914) |

Round 3 (2026-10-21): `inception/mercury-3` at reasoning effort low (local `extract_main` with `reasoning_effort: low`),
repeat 1, `20261021T015848Z_all_ecc213fa`: 37 / 37 answered, cells≥2 P / R / F1 0.897 / 0.806 / 0.850, cells≥1 F1 0.894,
nodes F1 0.966, schema degraded 0, USD 4.28 / 1k docs. Paired item bootstrap of cells≥2 F1: − luna repeat 1 +0.056
[−0.006, 0.123], − luna repeat 2 +0.061 [−0.021, 0.127], − Gemini 3.8 Flash repeat 1 −0.040 [−0.104, 0.005].
nemotron-3.5-super-vl at low effort was not run on this set.
