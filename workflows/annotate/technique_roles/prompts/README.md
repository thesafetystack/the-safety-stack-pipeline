# technique_roles prompts

The stage runs only in `configs/plans/ann-scratch.yaml` (not in production). The plan pin
`prompts.annotate.technique_roles` selects the prompt by its `name`: `{name: technique-roles-v2, role:
extract_main, model: openai/gpt-6-luna}` sends `technique-roles-v2.md` (no name: `technique-roles-v2`). `role` (an
`llm.yaml` role) and `model` (replacing the role's primary model) pick the LLM; the file is the text. The
mechanism is `stack/annotate/prompt_files.py` (its module docstring is the reference).

| File | What it is |
|---|---|
| `technique-roles-v2.md` | The default prompt (`ann-scratch`): rubric v0.4.0, the `sweeps` object first in the output contract, the smoke-review reminders. |
| `rubric-v0.4.0.md` | The rubric v0.4.0 as the judge sees it (§1–§7), included by v2. |
| `rubric-v0.4.0.notes.md` | Its metadata and changelog (what changed from v0.3.1 and why) and the §8 design notes. Not sent. |
| `technique-roles-v1.md` | The previous prompt (D591): byte for byte what the stage sent before prompts became files (D268). Kept for A/B. |
| `rubric-v0.3.1.md`, `rubric-v0.3.1.notes.md` | Rubric v0.3.1 (included by v1) and its metadata. |

**A prompt file** holds the whole prompt. The line that is, or ends in, `{{split:user}}` splits it: above it the
system part (the fixed prefix the client caches and hashes as `template_digest`: rubric, menu, output contract),
below it the user message of each document. v2 ends its system part with the line `{{input:document}}` (the client's
marker) and has `{{split:user}}` on its own line; v1 has `{{input:document}}{{split:user}}` on one line, so its
system part ends in the marker with no newline after it, as the earlier prefix did. The two user parts are the
same bytes, so the frozen eval set's input digests hold for both. `{{include:<file>.md}}` inserts a file of this
directory (minus its last newline; no nesting). The placeholders, which `main.py` fills:

- `{{taxonomy_menu}}` (system part, once per run): the whole taxonomy `taxonomy/taxonomy-v0.8.yaml` as the menu
  (`render.full_menu`: one line per technique, grouped under family headers);
- `{{document}}` (user part): the document's text up to the cap (300,000 characters);
- `{{truncation}}` (user part): empty, or the line `[DOCUMENT TRUNCATED AT CHARACTER CAP]` (with a blank line
  before and a newline after) when the text is longer.

A file must use exactly these (an unfilled or unknown placeholder fails the run before any call). `{{ref:…}}` and
`{{input:…}}` are the LLM client's markers and pass through. The role and doc-kind lists of the output contract are
plain text in the file; `tests/test_annotate_technique_roles.py` checks them against the payload's `ROLE_NAMES` and
`DOC_KINDS`.

**The output schema is shared by every prompt** (`judge.output_schema()`, permissive, D275): it has an optional
`sweeps` object that v2 asks for and v1 does not mention. `judge.normalize` turns `sweeps.menu_gaps` into
`menu_gap:` flags and records a sweep entry naming a menu slug with no row; the sweeps are not stored.

**A new prompt version** is a new file under a new name (e.g. `technique-roles-v3.md`), never an edit of a file a plan
pins: the file's digest (with its includes) and the taxonomy's sha256 are in the config digest, so an edit re-asks
every document under the old name. Copy the file, change it, include a new rubric file if the rubric changes
(`rubric-v<version>.md` + `rubric-v<version>.notes.md` with a changelog entry), and pin the new name in a plan. Then `stack eval
technique_roles --prompt technique-roles-v3` compares it on the frozen set (its input check needs the user part
unchanged, or a new set).
