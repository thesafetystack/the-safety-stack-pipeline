You are grading which safety/alignment TECHNIQUES play which ROLES in one document,
against a fixed catalogue. Below, in order: the grading rubric, the catalogue menu, your
output contract; the document follows in the next message.

The rubric and the menu are identical for every document in this run; only the document
changes.

# THE RUBRIC

{{include:rubric-v0.4.0.md}}

# THE MENU — the technique catalogue

Every catalogue technique, grouped by family. This menu is NOT evidence: no matcher ran
over the document, and most entries below are absent from it. Emit rows only for the
techniques you actually find. A slug containing a dot is a variant of the entry named by
its prefix (`dpo.ipo` is a variant of `dpo`). Some techniques exist only as children
(there is no generic "red-teaming" or "scalable oversight" entry): grade the child the
text supports, or list the surface under `menu_gaps`.

{{taxonomy_menu}}

# YOUR OUTPUT

Emit one JSON object in a ```json fenced block, and no other text. Write `sweeps` FIRST:
it is required, and it is where you do the four sweeps of rubric §7 before grading any
row.

```
{"sweeps": {
   "deployment_claims": [                 // §7 step 4: claims or guesses, hedged ones included,
     {"system": "<named system>",          // about what a deployed or released system was
      "technique": "<menu slug or surface>",  // trained or served with
      "quote": "<verbatim>",
      "direction": "uses | does_not_use"}],
   "recommendations": [                   // §7 step 5: should / must / need to / recommend /
     {"technique": "<menu slug or surface>",  // urge / propose / can reduce
      "quote": "<verbatim>",
      "direction": "recommended | rejected"}],
   "controls_discussed": [                // §7 step 6 (commentary, forum, policy, news): every
     {"surface": "<as written>",           // control, mitigation or technique the text talks
      "technique": "<menu slug, or omit>"}],  // about, in whatever words
   "menu_gaps": ["<technique-shaped surface with no menu slug>"]},   // §7 step 7
 "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
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

Every sweep entry that names a menu slug must become a row of `techniques` with the
matching role (`suspected_use` for a deployment claim, `considered` for a recommendation),
unless on reflection it is not in the text. Each `menu_gaps` surface becomes a
`menu_gap:` document flag; set `no_techniques_found` when `techniques` is empty.

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
- **NO QUOTE, NO ROW.** Every row — in `techniques` and `extra_techniques` alike — must
  carry at least one verbatim quote from the document supporting its highest-strength
  role, at every strength, including a lone `mentioned: 1`. Copy the words as they stand;
  you may leave out an inline citation, but do not reword.
- **The menu is never evidence.** A technique on the menu that is related to the topic
  but appears nowhere in the text gets NO ROW.
- **One sentence → 1.** A technique given one sentence in a role grades 1 in that role.
  Functional load is the exception: the method behind every result grades by its weight
  even when named once.
- **An evaluation applied is `used`.** Red-teaming, jailbreak, bias, over-refusal or
  capability evals run to measure a model or a mitigation are the instrument → `used`
  (+ `attested_use` in a first-party card). `evaluated` is for the thing measured, or for
  an evaluation method that is itself validated or compared.
- **System cards attest training too.** Every mitigation a first-party card says was
  applied to the shipped model — safety/refusal training, instruction-hierarchy training,
  safety data, as well as classifiers, monitors and sandboxes — is `attested_use` + `used`.
- **Hedged guesses about a deployed model are `suspected_use`.** "Our best guess is that
  this came from RL", "it may be that a training step induced this": the techniques named
  as candidates are `suspected_use`, not `mentioned: 1`.
- **The document's own new framework built on technique X**: the framework goes to
  `extra_techniques` as `introduced` (hint: X); X gets `used` (and `modified` if changed).
  Never `introduced` on X unless the document is X's origin.
- **`critiqued` needs an argument.** Limitations boilerplate ("further work is needed"),
  an unflattering number with no drawn conclusion, or one item in a list is not an argued
  critique (at most `critiqued: 1`, and only if the item states a failure).
- **Never mint a slug.** Off-menu techniques go to `extra_techniques` with the verbatim
  name and `hint_technique` where one applies, or to `menu_gaps`.
- **Sweep the document's own method section** for first-person method language ("we
  fine-tune", "we train", "our pipeline", "we score with"): those are the `used` rows.
- `mentioned` >= every other role for the same technique.

{{input:document}}
{{split:user}}
# THE DOCUMENT

{{document}}
{{truncation}}
Now emit the JSON object described above, in a ```json fenced block, and nothing else.
