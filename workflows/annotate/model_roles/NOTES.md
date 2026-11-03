# annotate.model_roles — operating notes

Status: in production. `batch-2026-10a` pins `{name: model-roles-v3b, role: extract_main_openai, doc_cap: 900000}`
(chosen on gold v3, D708; plan revision 7, D710); `main.py`'s module docstring is the reference for the mechanics,
`prompts/README.md` for the prompt files.

What this stage is: for each corpus document in scope, find **which vocabulary models it names** (the LLM-free
matcher over `configs/models.yaml`) and grade **which roles each plays in it** (one judge call over the matcher's
menu, against the rubric of the pinned prompt: ten non-exclusive categories × strength 1–4).

## Registry version 3 (2026-10-17, D593)

- **Every document is judged**, an empty or weak-only menu included; no answer without a call. Status `empty` =
  called, nothing graded. Counts `menu_empty`, `menu_weak_only`, `refined`, `refinement_dropped`, `instruct_folded`.
- **Prompts** `model-roles-v2` (the code default and the ann-scratch pin) and its descendants `model-roles-v3a`,
  `v3b` (production), `v4a`, `v4f`, all on rubric 0.6.0: the empty-menu rule (list every named model under
  `extra_models`), the ChatGPT rule (one boxed rule: a lone ChatGPT is a `gpt` family mention, never `matcher_fp`),
  the grain rule (Instruct/IT/Chat releases are the node; `judge.py` folds such a variant row back,
  `instruct_folded:<base>`), system-card and addendum rules, "one sentence → 1". `model-roles-v1` (rubric 0.5.3)
  is kept for A/B.
- **Menu** shows each node's matched text as written (`gpt-5` ← "gpt-5-thinking"), distinct, at most 6.
- **Refinement** (concretization): a model row may carry `refined_name`, the specific model the document
  determines behind a family, weak or product-name mention ("ChatGPT version 4" → GPT-4). The pipeline resolves it
  with the matcher (`match.resolve_name`, exactly one strong node) and accepts it in the menu node's lineage (itself,
  a descendant, or a descendant of its parent: `main.in_lineage`); the stored row's `node_id` is the refined node and
  `matched_node_id` the menu node; refused refinements are repairs (`refinement_dropped:<node>:<why>`), rows ending
  on one node merge (`refinement_merged`).
- **Record `model_regexp_matches`**: the matcher's hits (strong/weak, ambiguous spans) are a record of the same call
  (as `doc_class` of screen), its own family and annotation, folded by fold.build. Formats: model_roles /
  model_roles_evidence v3 `Incompatible` (older files are not read: re-run the stage); documents/corpus v4 drop the
  always-null legacy field `annotations.model_mentions` (a file where it holds data is refused, naming the file and
  the field).

## Mechanics (D591 and the v4 port)

- **One stage.** `annotate.model_mentions` (D266) was merged into this one (D591): `match.py` is its matcher (its
  fixture suite is `tests/test_annotate_model_roles_match.py` over `tests/fixtures/model_mentions/`). Basis `text`.
- **One judge.** The v3 verifier mode is not ported; the payload has no `judge`, `verifier`, `low_confidence` or
  `vocab_stamp` (D267). Output budget 16,000 tokens (D267); a strict-compatible output schema (D275); a reply cut
  off at the cap is retried once at 2x by the client, then `failed_permanent[truncated]` (D273).
- **Prompts are files** (`prompts/README.md`): the pin `name` selects `prompts/<name>.md`, which holds the system
  part (the cached prefix) and the user part, the rubric included from `prompts/rubric-v<version>.md` (its metadata
  and design notes in `rubric-v<version>.notes.md`, not sent). `model-roles-v1` renders the pre-merge prompts byte for
  byte (golden test over `tests/fixtures/model_roles/`), so cached replies and eval numbers stay comparable.
- **Provenance.** The config digest covers the system part's template digest, the prompt file (with its includes)
  and the vocabulary's sha256: a prompt, rubric or vocabulary edit re-asks every document in scope; a re-asked
  document whose request did not change is served from the LLM cache. A matcher or judge code change needs a
  registry `version` bump.
- **Concurrency, budget, halts** are the stage framework's (`stack.annotate`) and the LLM client's per-run ledger
  (D328).

## Eval

- `stack eval model_roles --set 2026-10-17a`: the scored set (37 items with gold; `evals/model_roles/2026-10-17a/`).
  The input check compares the prompt-independent fills for any `--prompt`, so prompt versions can be compared on
  the set; a run records the frozen and the current vocabulary digests and warns when they differ (D467).
- `evals/model_roles/2026-10-23a`: the gold v3 sample (100 documents) on which `model-roles-v3b` was chosen (D708);
  its labels are not in the repository.
- `evals/judge-2026-10-25j/model_roles`: the three-judge evaluation of production output that the release's
  quality table reports.
- `stack eval model_mentions` scores the matcher alone.
