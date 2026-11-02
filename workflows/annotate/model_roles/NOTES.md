# annotate.model_roles — operating notes

Status: the "v4 merge" section is current (with `main.py`'s module docstring as the reference); production pins
`model-roles-v3b` (`batch-2026-10a`, D710; `prompts/README.md`). The "v4 (round 5)" section is the port's record,
history where the merge changed it (marked). The v3 record (sections A–E: judge modes, concurrency, pilot fixes,
provenance, gaps, calibration, measurements), whose mechanics (`configs/workflows.yaml`, mode 2, receipts, Postgres)
do not exist in v4, was removed on 2026-11-02 (not in this repository).

What this stage is: for each corpus document in scope, find **which vocabulary models it names** (the LLM-free
matcher over `configs/models.yaml`) and grade **which roles each plays in it** (one judge call over the matcher's
menu, against the rubric of the pinned prompt: ten non-exclusive categories × strength 1–4).

## Registry version 3 (2026-10-17; read this first)

- **Every document is judged**, an empty or weak-only menu included; no answer without a call. Status `empty` =
  called, nothing graded. Counts `menu_empty`, `menu_weak_only`, `refined`, `refinement_dropped`, `instruct_folded`.
- **Prompt `model-roles-v2`** (default and the ann-scratch pin; `model-roles-v1` kept for A/B), rubric 0.6.0: the
  empty-menu rule (list every named model under `extra_models`), the ChatGPT rule (one boxed rule: a lone ChatGPT is
  a `gpt` family mention, never `matcher_fp`), the grain rule (Instruct/IT/Chat releases are the node; `judge.py`
  folds such a variant row back, `instruct_folded:<base>`), system-card and addendum rules, "one sentence → 1".
- **Menu** shows each node's matched text as written (`gpt-5` ← "gpt-5-thinking"), distinct, at most 6.
- **Refinement** (concretization): a model row may carry `refined_name`, the specific model the document
  determines behind a family, weak or product-name mention ("ChatGPT version 4" → GPT-4). The pipeline resolves it
  with the matcher (`match.resolve_name`, exactly one strong node) and accepts it in the menu node's lineage (itself,
  a descendant, or a descendant of its parent: `main.in_lineage`); the stored row's `node_id` is the refined node and
  `matched_node_id` the menu node; refused refinements are repairs (`refinement_dropped:<node>:<why>`), rows ending
  on one node merge (`refinement_merged`). Before: nothing supported it (only matched nodes on the menu, unknown node
  rows dropped, and rubric 0.5.3 §5 said not to re-attribute a family mention).
- **Record `model_regexp_matches`**: the matcher's hits (strong/weak, ambiguous spans) are a record of the same call
  (as `doc_class` of screen), its own family and annotation, folded in ann-scratch; `matches`/`ambiguous` left the
  payload. Formats: model_roles / model_roles_evidence v3 `Incompatible` (older files are not read: re-run the
  stage); documents/corpus v4: the v3 → v4 step drops an always-null legacy field, `annotations.model_mentions`
  (no data conversion; a file where it holds data is refused, naming the file and the field), then the additive
  conform (production documents are v3 with the field always NULL).
- **Eval**: `stack eval model_roles --set 2026-10-17a` (37 items: the 25, five real no-model texts, synthetic
  off-catalogue and concretization items; gold remapped for the 8 moved items; an `extras` score block).

## v4 merge (D591; history where version 3 changed it)

- **One stage.** `annotate.model_mentions` (D266) merged into this one: `match.py` is its matcher (moved, not
  changed; its fixture suite is `tests/test_annotate_model_roles_match.py` over `tests/fixtures/model_mentions/`).
  Basis `text` (was `chained` over model_mentions' payload), registry `version` 2.
- **Payload** (history: `matches`/`ambiguous` are the record `model_regexp_matches` since version 3; `stack/schemas/payloads/model_roles.py`, additive): `matches` (every hit, strong then weak, with
  surfaces), `ambiguous` (the spans the matcher could not attribute), `extra_models[].node_id` (an off-menu model
  the judge named that the vocabulary knows). (History, until registry version 3: no strong mention → no call; weak
  hits only → `ok` with the flag `not_judged:no_strong_match`, no hit at all → `empty`. Since D593 every document is
  judged: "Registry version 3" above.)
- **Prompts are files** (`prompts/README.md`): the pin `name` selects `prompts/<name>.md`, which holds the
  system part (the cached prefix, ex-`judge.md`) and the user part (ex-`prompts/v1.md.j2`, which v4 had
  re-implemented in `render.py`), the rubric included from `prompts/rubric-v0.5.3.md` (its metadata and §8
  design notes in `rubric-v0.5.3.notes.md`, not sent). `model-roles-v1` renders the pre-merge prompts byte for
  byte (golden test over `tests/fixtures/model_roles/`; 14 real eval items' frozen `input_digest`s reproduce), so
  cached replies and eval numbers stay comparable.
- **Provenance.** The config digest covers the system part's template digest, the prompt file (with its
  includes) and the vocabulary's sha256: a prompt, rubric or vocabulary edit re-asks every document in scope;
  a re-asked document whose request did not change is served from the LLM cache.
- **Format.** `documents`/`corpus` v4 drop `annotations.model_mentions` (function step, arrow-native) and gain
  the payload fields; `model_roles`/`model_roles_evidence` v2 (additive). Rows written before the merge read
  the new fields as NULL, but their `basis_kind` is `chained` and their `result_digest` covers the old payload
  shape, so V7/V8 flag them on a store that has them (scratch stores only; production never ran the stage).
- **Eval (history: now 2026-10-17a).** `stack eval model_roles --set 2026-10-16a` (set 2026-10-13a re-frozen at the merge with vocabulary
  v0.5; 2026-10-13a refuses the 8 items whose scan moved). The input check compares the prompt-independent fills
  for any `--prompt`, so prompt versions can be compared on the set. Its gold is 2026-10-13a's (v0.5 mentions,
  role grades over the v0.4 menus): 8 items have gaps, listed with their scoring cost in the set's README.
  `stack eval model_mentions` scores the matcher alone.

## v4 (round 5, T-ANN; history where the merge changed it)

The v4 stage is `main.py` (its module docstring is the reference; D267, D273, D275). Where the v3 operating notes
(Postgres, `configs/workflows.yaml`, receipts, run accept; removed, see the status line) describe mechanics, v4
differs:

- **One judge only** (v3's mode 1). Mode 2 (verifier, `low_confidence`, `verify: true`,
  `version: 2`, `model_roles_verify`) is not ported; the payload has no `judge`, `verifier`,
  `low_confidence` or `vocab_stamp` (D267). `configs/workflows.yaml` was retired at T4.5 (D42).
- **Pin** `prompts.annotate.model_roles` in the plan, e.g. `{name: model-roles-v1, role: extract_main,
  model: openai/gpt-6-luna}` (`configs/plans/ann-scratch.yaml`); output budget 16,000 tokens (D267);
  a strict-compatible output schema (D275); a reply cut off at the cap is retried once at 2x by the
  client, then `failed_permanent[truncated]` (D273).
- **Provenance (history since D591, which adds the vocabulary).** The rubric and the template are in
  the config digest, so an edit re-asks every document (no `version` bump). The vocabulary was not: the menu
  reached this stage through model_mentions' payload (the `chained` basis), so a changed mention re-asked the
  document; a menu-text-only vocabulary edit did not.
- **Concurrency, budget, halts.** The v4 stage framework (`stack.annotate`) and
  the LLM client's per-run ledger (D328) do these; `STACK_MODEL_ROLES_WORKERS`, the psycopg lock and
  the `llm_calls` table do not exist in v4.
- **Eval (history since D591).** `stack eval model_roles` ran production `ask` on the frozen set
  `evals/model_roles/2026-10-13a` with its frozen menus (vocabulary v0.4) and warned that production scans
  with v0.5 (D467).
