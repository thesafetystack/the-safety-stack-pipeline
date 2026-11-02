# annotate.technique_roles — decisions and measurements

Status: the v2 prompt and rubric v0.4.0 (2026-10-17), the v4 prompt-file change (2026-10-16), then the design record
of the v4 port (2026-10-13). The prototype's notes, which followed verbatim, were removed on 2026-11-02 (not in
this repository). The current rules are the module docstrings of
`main.py`, `render.py` and `judge.py` and `prompts/README.md`; the stage runs in `configs/plans/ann-scratch.yaml`
only.

## v4: prompt v2, rubric v0.4.0, loose quote verification (2026-10-17)

From the smoke review of 11 documents run with v1 (gpt-6-luna; 69 rows, identity precision 68/69, about 19 clear
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
  digits survive the projection in order, so the anchorer's digit-run rule still refuses a changed number
  (round 2b: the first version dropped numeric table cells on both sides and let a wrong number verify). On the smoke run: 8 of the 9 unverified quotes verify, the real paraphrase does not; the
  anchorer's fuzzy threshold and digit-run rule apply unchanged. Recorded per quote (`quote_verified_loose:`),
  counted per run. `stack.anchor` is unchanged (affiliation uses it).
- Registry `version` 1 → 2 (verifier and flag behaviour; payload schema unchanged).

## v4: prompt files (D591, 2026-10-16)

The prompt moved onto `annotate.model_roles`' convention (`stack/annotate/prompt_files.py`): the plan pin `name`
selects `prompts/<name>.md` (default `technique-roles-v1`; a bad name or a missing file is refused before any call);
role and model still come from the pin's `role` / `model`.

- `prompts/technique-roles-v1.md` holds the whole prompt: the system part (the prototype template's text before
  `# THE DOCUMENT`, with `{{include:rubric-v0.3.1.md}}` and the system-part placeholder `{{taxonomy_menu}}`, filled
  once per run with `render.full_menu` of the taxonomy file), closed by the line `{{input:document}}{{split:user}}`,
  then the user part (`{{document}}`, `{{truncation}}`). The doc-kind and role lists of the output contract are
  plain text now (they were Jinja expressions over `DOC_KINDS` / `ROLE_NAMES`); a test checks them against the
  payload constants.
- `prompts/rubric-v0.3.1.md` is the rubric as the judge sees it (§1-§7); its frontmatter and §8 design notes are
  `prompts/rubric-v0.3.1.notes.md` (`depends_on` corrected to `taxonomy/taxonomy-v0.8.yaml`). `rubric.md` at the
  workflow root and `prompts/a.md.j2` are gone, and so is the Python re-implementation of the template
  (`render.split_template` / `render_prefix` / `render_user`, `judge_rubric`, `rubric_version`): editing the old
  `.j2` file changed nothing sent, now the file is what is sent.
- **Byte-identical**: the system template and the user message are the bytes the old renderer produced, checked
  by a golden rendered with the old code before it was deleted (`tests/fixtures/technique_roles/`: a frozen
  31-entry taxonomy subset, 4 documents, plus the full v0.8 prefix's sha256), and by the eval set 2026-10-13a
  (25 of 25 frozen `input_digest`s reproduce). The system template digests per model are unchanged, so the
  LLM cache keys of the same requests are too.
- To end the system part in the marker with no newline (as the old prefix did), `prompt_files` now also accepts a
  line that ENDS in `{{split:user}}` (the text before it closes the system part); a line that is exactly the marker
  behaves as before.
- **Provenance**: `template_digests` = the system part's template digest per model, the prompt file's digest
  (file + includes) and the taxonomy file's sha256 (was: sha256 over `rubric.md`, `a.md.j2` and the taxonomy
  sha256). The config digest therefore changes once (ann-scratch pin: `a30bcdff…` to `7cf00511…`), so the next
  run re-asks every document; the requests are byte-identical, so they are served from the LLM response cache.
  Registry `version` stays 1. `ask` refuses a taxonomy whose sha256 is not among the pinned digests (edited mid-run).

## v4 port (round 5, T-ANN B2, 2026-10-13)

Ported from the off-DB prototype on branch `tomas/technique-roles` (df411eb): `rubric.md` (v0.3.1)
and `prompts/a.md.j2` verbatim; `render.py` (taxonomy loader, menu, prompt) and the parse and
clamps of `tools/technique_roles_smoke.py` (now `judge.py`) adapted; `main.py` new (the v4 stage,
pattern of `model_roles`). Approach A only. Not ported: approach B, the critic, the adjudication,
the multi-judge union, the technique×model binding, the experiments (they stay on the branch).

What differs from the prototype (details in the module docstrings):

- **Menu file**: `taxonomy/taxonomy-v0.8.yaml` (the cleanup moved `taxman/` there; byte-identical),
  loaded by the workflow, not by `stack.config` (which pins v0.6 and rejects v0.8's `aliases`).
- **Prompt split**: rubric + menu + output contract are the client's fixed prefix (cached `system`
  block); the document is the user message. No `doc_meta` header (title etc.): the basis is the
  text only. Rendered without jinja2 (v4 has none); the template's document section is checked
  against a constant, a template expression the renderer does not fill raises.
- **Output**: the permissive schema goes out as `response_format` (OpenAI refuses it, so gpt-6-luna
  answers on the client's degrade path, D275); `roles` names its twelve keys as optional properties
  (R6: the earlier open map got `"roles": {}` on every row from claude-haiku-5.5, and such an
  all-empty answer is now `failed_permanent[schema_invalid]`, not `empty`); the template
  still asks for a fenced block, which the client unwraps. `max_tokens` 16000 (the prototype's
  first budget). The prototype's 60000-token retry of a truncated reply is NOT ported; the LLM
  client's rule applies instead (D273, after this port): a reply cut off at the cap is re-sent once
  at twice the cap, then the unit is `failed_permanent[truncated]`, and the prior carry keeps it.
- **Repair rules**: no quote → the row is DROPPED (prototype: kept, flagged); an unknown slug →
  MOVED to `extra_techniques` (prototype: dropped); an extra with no nonzero role is dropped;
  quotes are verified with `stack.anchor` against the whole text (prototype: model_roles' v3
  ladder) plus the prototype's elision rule; a row whose quotes all fail is kept, row flag
  `quote_unverified` (open policy question).
- **Provenance**: the rendered prefix's template digest per model plus sha256(rubric, template,
  taxonomy sha256) are the pin's `template_digests`, so any of the three files re-asks every
  document. The prototype stamped `judge`, `rubric_version`, `taxonomy_stamp` on the payload.
- **Judge**: one per plan pin (`ann-scratch`: `openai/gpt-6-luna` on `extract_main`; the
  prototype's default was `openai/gpt-5.6-luna`, not re-measured). The prototype's production candidate (union of
  deepseek-v4-flash, gpt-5.6-luna-pro, tencent/hy3) is an open decision.

Real runs: 30 screened-in documents of the dev store, 2026-10-13 (the 2026-10 run log (not in this repository); D275).
