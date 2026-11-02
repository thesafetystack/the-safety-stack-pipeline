You are grading which safety/alignment TECHNIQUES play which ROLES in one document,
against a fixed catalogue. Below, in order: the grading rubric, the catalogue menu, your
output contract, and finally the document.

The rubric and the menu are identical for every document in this run; only the document
at the end changes.

# THE RUBRIC


{{include:rubric-v0.3.1.md}}

# THE MENU — the technique catalogue

Every catalogue technique, grouped by family. This menu is NOT evidence: no matcher ran
over the document, and most entries below are absent from it. Emit rows only for the
techniques you actually find. A slug containing a dot is a variant of the entry named by
its prefix (`dpo.ipo` is a variant of `dpo`).

{{taxonomy_menu}}

# YOUR OUTPUT

Emit one JSON object in a ```json fenced block, and no other text:

```
{"doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
 "techniques": [
   {"slug": "<menu slug>",
    "roles": {"<role>": <1-4>, ...},      // only nonzero roles; omit the rest
    "quotes": ["<verbatim from the document, <=200 chars>"],   // REQUIRED, at least one
    "flags": ["recommended"],              // optional, see the flag vocabulary below
    "note": "<optional, short>"}],
 "extra_techniques": [
   {"name_as_written": "<verbatim>", "gloss": "<one sentence>",
    "hint_technique": "<nearest menu slug, or omit>",
    "roles": {...},
    "quotes": ["<verbatim>"]}],                                // REQUIRED, at least one
 "flags": ["no_techniques_found", "menu_gap:<surface>"]}
```

Row flag vocabulary (optional, zero or more per row; they carry no strength):
`claims_absent` (a `suspected_use` row arguing the system does NOT use it),
`confirmed` (a `suspected_use` row relaying an official statement),
`recommended` / `rejected` (direction of a `considered` row),
`supportive` (prominent praise, or the document pitching the technique's strong
performance). There is no `stance` flag — critique is the `critiqued` category.

The 12 roles, each an integer 1–4 (omit a role entirely when it is 0):
mentioned, cited_work, discussed, used, evaluated, modified, introduced, analyzed, attested_use, suspected_use, considered, critiqued.
`mentioned` is the umbrella and must be >= every other role for the same technique.

Reminders that the grading depends on:
- **NO QUOTE, NO ROW.** Every row you emit — in `techniques` and `extra_techniques`
  alike — must carry at least one verbatim quote from the document supporting its
  highest-strength role. This holds at every strength, including a lone `mentioned: 1`.
  A row you cannot quote is invalid output: drop it.
- **The menu is never evidence.** Conceptual adjacency is not presence. If a technique
  is on the menu, is related to the document's topic, and yet appears nowhere in the
  text, it gets NO ROW. Do not write a row whose note concedes the technique is "not
  named explicitly" or "not present in the text" — drop the row instead.
- **Rows only for what you find.** An empty `techniques` array is a valid answer. Do
  not invent rows to fill it.
- **Never mint a slug.** Off-menu techniques go to `extra_techniques` with the verbatim
  name and, where one applies, `hint_technique` pointing at the nearest menu slug.
- **Evidence before scores.** Roles graded >= 2 that the first quote does not cover need
  their own. Prefer quotes that name the method. Fabricated or paraphrased quotes
  invalidate the row.
- **Functional load beats textual footprint.** A method named once in the methods
  section but carrying every result grades by its weight in the work, not by its
  word count.
- **Sweep the document's own method section explicitly** for first-person method
  language ("we fine-tune", "we train", "our pipeline", "we filter", "we score
  with") — those are the `used` rows and they are the easiest to miss.
- **`suspected_use` fires whenever the author infers what a lab's system does.** If the
  document reasons about a deployed or released system's behaviour, training, pricing, or
  quirks, ask which catalogue techniques it implies are (or are not) running there, and
  grade `suspected_use` — not merely `discussed`. Set `claims_absent` when the claim is
  that the technique is NOT in use, `confirmed` when reasoning from an official
  statement. Judges routinely under-use this category; check for it deliberately.
- `mentioned` >= every other role for the same technique.

{{input:document}}{{split:user}}
# THE DOCUMENT

{{document}}
{{truncation}}
Now emit the JSON object described above, in a ```json fenced block, and nothing else.
