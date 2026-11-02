# model_roles prompts

The plan pin `prompts.annotate.model_roles` selects the prompt by its `name`: `{name: model-roles-v2, role:
extract_main, model: openai/gpt-6-luna}` sends `model-roles-v2.md` (no name: `model-roles-v2`, the default). `role` (an
`llm.yaml` role) and `model` (replacing the role's primary model) pick the LLM; the file is the text. The
mechanism is `stack/annotate/prompt_files.py` (its module docstring is the reference).

| File | What it is |
|---|---|
| `model-roles-v2.md` | The default prompt (registry version 3): empty and weak-only menus, the ChatGPT rule, refinement (`refined_name`), the grain and system-card rules. Includes `rubric-v0.6.0.md`. |
| `rubric-v0.6.0.md` / `.notes.md` | The rubric as the judge sees it (§1–§7) / its metadata, changelog and §8 design notes (not sent). |
| `model-roles-v3a.md` | Experimental (gold-v3 prompt round, eval only): v2 plus a "Family and group rows" block in the system part (anti-bleed: a family / group row grades only text that names no specific model; otherwise all-zero with `subsumed:` or `bib_only:`). Same rubric 0.6.0. |
| `model-roles-v3b.md` | Experimental: v3a plus a "Strength calibration" block (`mentioned` is overall presence; `cited_work` counts every relayed claim; `discussed` is the document's own voice beyond its own experiments). Same rubric 0.6.0. |
| `model-roles-v4a.md` | Experimental (gold-v3 prompt round 2, eval only; Codex Astra, verbatim): v3b's blocks rewritten as role-specific checks for the 1-vs-2 boundary (substantive attention or functional weight earns 2; isolated rows and passing references stay 1) and family / generation rows that keep independent generic claims. Same rubric 0.6.0. |
| `model-roles-v4f.md` | Experimental (gold-v3 prompt round 2, eval only; Fable, verbatim): the 1-vs-2 boundary and family / generation rows, written against the same dev-split error cases. Same rubric 0.6.0. |
| `model-roles-v1.md` | The D591 prompt (the v3 / round 5 prompt as files), kept for A/B in the eval. Includes `rubric-v0.5.3.md`. |
| `rubric-v0.5.3.md` / `.notes.md` | The 0.5.3 rubric / its metadata and design notes. |

**A prompt file** holds the whole prompt. The line `{{split:user}}` splits it: above it the system part (the
fixed prefix the client caches and hashes as `template_digest`), below it the user message of each document.
`{{include:<file>.md}}` inserts a file of this directory (minus its last newline; no nesting). The user part's
placeholders, which `main.py` fills per document:

- `{{document}}`: the document's text up to the cap (300,000 characters, or the pin's `doc_cap`, at most
  1,000,000; the menu is built against the same window);
- `{{truncation}}`: empty, or the line `[DOCUMENT TRUNCATED AT CHARACTER CAP]` (with a blank line before and a
  newline after) when the text is longer;
- `{{menu}}`: the menu, one entry per candidate model (`render.menu_text`), each opening with a blank line and
  showing the matched text as written; empty when the matcher found nothing (the prompt says what to do then).

Every version fills the same three (so the eval's frozen fills digest holds across versions). A file must use exactly these (an unfilled or unknown placeholder fails the run before any call). `{{ref:…}}` and
`{{input:…}}` are the LLM client's markers and pass through.

**A new prompt version** is a new file under a new name (`model-roles-v3.md`), never an edit of a file a plan
pins: the file's digest (with its includes) and the vocabulary's sha256 are in the config digest, so an edit
re-asks every document under the old name. Copy the file, change it, include a new rubric file if the rubric
changes (`rubric-v<version>.md` + `.notes.md`), and pin the new name in a plan. Then `stack eval model_roles
--set 2026-10-17a --prompt model-roles-v3` compares it on the frozen set (`evals/model_roles/2026-10-17a/README.md`).
The reply schema (`judge.output_schema`) is code: a prompt may ask for `refined_name` and the fields above only.
