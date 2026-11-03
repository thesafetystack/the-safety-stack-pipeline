# annotate.technique_roles — decisions and measurements

Status: not in production. The stage runs only in `configs/plans/ann-scratch.yaml` (pin `technique-roles-v2`,
`openai/gpt-6-luna`); production extracts techniques with `annotate.technique_staged` (D708, D710). The current
rules are the module docstrings of `main.py`, `render.py` and `judge.py` and `prompts/README.md`; the eval set is
`evals/technique_roles/2026-10-13a`. Registry `version` 2.

What this stage is: one judge call per document over the whole techniques taxonomy `taxonomy/taxonomy-v0.8.yaml` as
a menu (rubric + menu + output contract are the client's cached system prefix, the document is the user message);
each technique row carries roles and supporting quotes.

## Prompt v2, rubric v0.4.0, loose quote verification (2026-10-17)

From a smoke review of 11 documents run with v1 (gpt-6-luna; 69 rows, identity precision 68/69, about 19 clear
recall misses, no `suspected_use` rows, no `menu_gap` flags, 5 `considered` rows, 9 of 130 quotes unverified).

- `prompts/technique-roles-v2.md` (default; `ann-scratch` pin) with `rubric-v0.4.0.md` (changelog in
  `rubric-v0.4.0.notes.md`): one sentence → 1 as a rule; an evaluation technique applied as the instrument is `used`
  (precedent 11); shipped training-time mitigations in first-party cards are `attested_use` (precedent 12); hedged
  inference about a deployed model is `suspected_use`; the prescriptive surface list for `considered`; boilerplate
  and one-item list membership are not argued critique; the document's own framework built on a catalogue technique
  goes to extras as `introduced` (precedent 6); generic surfaces with only child entries and governance instruments
  → the supported child or `menu_gap` (precedent 13). Four REQUIRED sweeps (deployment claims, recommendations,
  controls discussed, menu gaps) are an optional `sweeps` object of the output schema, written first: the schema
  stays permissive (D275), and a provider that accepts the schema as `response_format` still gets them (prose
  before the JSON would not survive that path). v1 stays selectable for A/B; v2's user part is v1's, byte for byte,
  so the eval set 2026-10-13a's input check passes for both (25/25).
- Pipeline (`judge.normalize`): `sweeps.menu_gaps` → `menu_gap:<surface>` doc flags (deduplicated, at most 30);
  `no_techniques_found` set exactly when `techniques` is empty; a sweep entry naming a menu slug with no kept row is
  recorded (`sweep_without_row:<sweep>:<slug>`), never turned into a row. Sweeps are counted, not stored.
- Quote verification: when the strict anchor fails, `judge.verify_how` retries on the `judge.loosen` projections of
  quote and text (NFKC; markdown table rows read as text, every cell kept, a converter's continuation row merged
  into its row; inline citations `[12]`, `(Smith et al., 2024; …)`, `Sharma et al. (2025)` removed from the text
  when the quote carries none; LaTeX read as text; spacing around brackets and punctuation; case). The quote's
  digits survive the projection in order, so the anchorer's digit-run rule still refuses a changed number. On the
  smoke run: 8 of the 9 unverified quotes verify, the real paraphrase does not; the anchorer's fuzzy threshold and
  digit-run rule apply unchanged. Recorded per quote (`quote_verified_loose:`), counted per run. `stack.anchor` is
  unchanged (affiliation uses it).

## Prompt files (D591, 2026-10-16)

The prompt follows `annotate.model_roles`' convention (`stack/annotate/prompt_files.py`): the plan pin `name`
selects `prompts/<name>.md` (a bad name or a missing file is refused before any call); role and model come from the
pin's `role` / `model`.

- `prompts/technique-roles-v1.md` holds the whole prompt: the system part (with `{{include:rubric-v0.3.1.md}}` and
  the system-part placeholder `{{taxonomy_menu}}`, filled once per run with `render.full_menu` of the taxonomy
  file), closed by the line `{{input:document}}{{split:user}}`, then the user part (`{{document}}`,
  `{{truncation}}`). The doc-kind and role lists of the output contract are plain text; a test checks them against
  the payload constants.
- `prompts/rubric-v0.3.1.md` is the rubric as the judge sees it (§1-§7); its frontmatter and §8 design notes are
  `prompts/rubric-v0.3.1.notes.md`.
- v1 is byte-identical to the prompt the stage sent before prompts became files, checked by a golden
  (`tests/fixtures/technique_roles/`: a frozen 31-entry taxonomy subset, 4 documents, plus the full v0.8 prefix's
  sha256) and by the eval set 2026-10-13a (25 of 25 frozen `input_digest`s reproduce).
- `prompt_files` accepts a line that ENDS in `{{split:user}}` (the text before it closes the system part) as well as
  a line that is exactly the marker.
- **Provenance**: `template_digests` = the system part's template digest per model, the prompt file's digest
  (file + includes) and the taxonomy file's sha256. `ask` refuses a taxonomy whose sha256 is not among the pinned
  digests (edited mid-run).

## Stage behaviour (v4 port, 2026-10-13)

Ported from an earlier off-database prototype (approach A only; no critic, adjudication, multi-judge union or
technique×model binding). Details in the module docstrings:

- **Menu file**: `taxonomy/taxonomy-v0.8.yaml`, loaded by the workflow, not by `stack.config`.
- **Prompt split**: no `doc_meta` header (title etc.): the basis is the text only.
- **Output**: the permissive schema goes out as `response_format` (OpenAI refuses it, so gpt-6-luna answers on the
  client's degrade path, D275); `roles` names its twelve keys as optional properties (an open map got
  `"roles": {}` on every row from one model, and such an all-empty answer is `failed_permanent[schema_invalid]`, not
  `empty`); the prompt still asks for a fenced block, which the client unwraps. `max_tokens` 16000; a reply cut off
  at the cap is re-sent once at twice the cap, then the unit is `failed_permanent[truncated]` (D273).
- **Repair rules**: no quote → the row is dropped; an unknown slug → moved to `extra_techniques`; an extra with no
  nonzero role is dropped; quotes are verified with `stack.anchor` against the whole text plus an elision rule; a
  row whose quotes all fail is kept, row flag `quote_unverified` (open policy question).
- **Judge**: one per plan pin. A production judge for this stage (a single model or a union) is an open decision.

First real runs: 30 screened-in documents of a dev store, 2026-10-13 (D275).
