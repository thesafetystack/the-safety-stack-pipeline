You are grading which safety/alignment TECHNIQUES play which ROLES in one document,
against a fixed catalogue. Below, in order: the grading rubric, the catalogue menu, your
output contract; the document follows in the next message.

The rubric and the menu are identical for every document in this run; only the next
message changes.

# THE RUBRIC

{{include:rubric-v0.9-c.md}}

# THE MENU — the technique catalogue

Every catalogue entry, by kind (intervention, monitoring, assessment); group nodes are
listed with their children. Each line: slug, kind, name, what it is also called (`aka`),
named instances (`e.g.`), its parent (`child of`, if any), and its definition.
`(provisional)` after a name marks a draft entry whose definition may still change; grade
it like any other entry. This menu is NOT evidence:
no matcher ran over the document, and most entries below are absent from it. Emit rows
only for the techniques you actually find. Group nodes are targets for generic mentions
only: grade the child the text supports, and the group node only for generic material no
child covers (rubric precedents 13-14).

{{taxonomy_menu}}

# HOW TO FIND EVERYTHING

Missing a technique the document engages is the commonest error in this task, more common
than a wrong row. The rules against inventing rows stay in force (rubric §1: a verbatim
quote, a definition the text meets). Within them, include:

1. Strength-1 rows are rows. A technique named once (a list item, a related-work sentence
   that relays a claim, a table caption, a parenthesis) gets a row at strength 1. Never
   drop a row for being minor.
2. One passage, several entries. The catalogue separates procedures from the objects they
   serve, mechanisms from domains, evaluations from their methodology. Grade every entry
   whose definition the passage meets; entries for one passage are not alternatives.
   Typical co-occurring pairs: an evaluation + the realism of its environment or simulated
   users; an evaluation + the validation of its grader or judge; an evaluation + its
   multi-turn or adaptive set-up; an evaluation + elicitation effort; a classifier + its
   hazard domain; a monitor + the trace it watches; a release decision + the access tiers
   it uses. Precedent 14 removes a parent or group node when a child covers the same
   evidence; it never removes a sibling.
3. Plain-words descriptions count. Safeguards are often described, not named (a stricter
   limit for some users or accounts, a review of usage for misuse patterns, the reasoning
   shown only as a summary). Match descriptions to definitions, not only to names and
   aliases.
4. Read to the end. Evaluation details, appendices, limitations, discussion and related
   work at the end of long documents carry many rows.

Facet checklist: check each before you write rows.
- own method: every training, filtering, monitoring or evaluation step the authors run,
  including the safety training of models they build on (precedent 5);
- evaluation set-up: environment or user simulation and its realism; elicitation effort
  and scaffolds; judge, grader or verifier validation; multi-turn or adaptive attacks;
  contamination or train/test separation; checks for reward hacking;
- first-party safeguards: refusal or safety training and safety data; classifiers by stage
  and domain; account-level or usage monitoring; access tiers; rapid response;
  sandboxing and infrastructure isolation; staged release; reasoning-trace handling;
- other work's techniques the text relays a claim about (`cited_work`), and techniques it
  criticises, recommends, or guesses a lab uses.

# YOUR OUTPUT

Emit one JSON object in a ```json fenced block, and no other text. Write `sweeps` FIRST,
and inside it `passages` first: it is your inventory of the document. The other sweeps
record rubric §7's sweeps before you grade any row.

```
{"sweeps": {
   "passages": [                          // document order, covering every section to the last;
     {"where": "<section heading, short>",  // one line per passage that engages a technique;
      "what": "<=12 words: what is done or said>",  // merge repeats of one technique
      "slugs": ["<EVERY menu slug the passage meets>"],  // within a section; at most 80 lines
      "none": "ml_method_only | too_vague | not_a_technique | menu_gap"}],
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
   "menu_gaps": ["<safety technique named in the text that has no menu slug>"]},
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
 "not_graded": [{"slug": "<a slug listed in passages or a sweep>",
                 "why": "not_in_text | parent_of_graded_child | bibliography_only"}],
 "flags": ["no_techniques_found", "menu_gap:<surface>"]}
```

A passage line has `slugs` or `none`, not both. Every slug listed in `passages` or in a
sweep must end either as a row of `techniques` (with every role the whole document
supports) or in `not_graded` with its reason. `not_graded` is for slugs you listed and
then found the text does not support; it is not for minor techniques (those are
strength-1 rows).

Every sweep entry that names a menu slug must become a row of `techniques` with the
matching role (`suspected_use` for a third-party deployment claim, `attested_use` for a
first-party one, `considered` for a recommendation), unless on reflection it is not in
the text. `menu_gaps` lists safety techniques (by the rubric's rules: not governance
instruments, not ML methods, not vague statements) that the text names but the menu does
not carry and that are not worth an `extra_techniques` row; each becomes a `menu_gap:`
document flag. Set `no_techniques_found` only when both `techniques` and
`extra_techniques` are empty.

Row flag vocabulary (optional, zero or more per row; they carry no strength):
`claims_absent` (a `suspected_use` row arguing the system does NOT use it),
`confirmed` (a `suspected_use` row relaying an official statement),
`recommended` / `rejected` (direction of a `considered` row),
`supportive` (prominent praise, or the document pitching the technique's strong
performance), `variant:<name as written>` (a variant or the document's own new method
graded on the entry it falls within). There is no `stance` flag — critique is the
`critiqued` category.

The 12 roles, each an integer 1–4 (omit a role entirely when it is 0):
mentioned, cited_work, discussed, used, evaluated, modified, introduced, analyzed, attested_use, suspected_use, considered, critiqued.
`mentioned` is the umbrella and must be >= every other role for the same technique.

Reminders that the grading depends on:
- **NO QUOTE, NO ROW.** Every row — in `techniques` and `extra_techniques` alike — must
  carry at least one verbatim quote from the document supporting its highest-strength
  role, at every strength, including a lone `mentioned: 1`. Copy the words as they stand;
  you may leave out an inline citation, but do not reword.
- **One sentence → 1.** A technique given one sentence in a role grades 1 in that role.
  Functional load is the exception: the method behind every result grades by its weight
  even when named once.
- **An evaluation applied is `used`.** Red-teaming, jailbreak, bias, over-refusal or
  capability evals run to measure a model or a mitigation are the instrument → `used`
  (+ `attested_use` in a first-party statement). `evaluated` is for the thing measured,
  or for an evaluation method that is itself validated or compared.
- **First-party deployment statements attest training too.** Every mitigation a
  first-party card, blog post or documentation page says is applied to the shipped or
  deployed system — safety/refusal training, instruction-hierarchy training, safety data,
  as well as classifiers, monitors and sandboxes — is `attested_use` + `used`. A vague
  attestation that identifies no technique ("our standard safety training") gets no row.
- **Hedged guesses about a deployed model are `suspected_use`** when they offer evidence
  (an observed behaviour, an artifact, a statement reasoned from): the techniques named
  as candidates are `suspected_use`, not `mentioned: 1`.
- **The document's own new method**: graded on the catalogue entry whose definition it
  falls within, as `introduced` with a `variant:<name as written>` flag; it goes to
  `extra_techniques` as `introduced` only when it goes beyond every entry. Grade
  independently evidenced components as their own rows.
- **ML implementation methods are never rows** (RL, PPO, DPO, LoRA, LLM-as-a-judge, SAEs
  ...): grade the technique they implement; you may note the method in `note`.
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
# THE DOCUMENT — PART {{part}} OF {{parts}}

This is one part of a long document, cut at paragraph boundaries, overlapping its
neighbours by one paragraph. Grade only what THIS part shows; the other parts are graded
in separate calls and the rows are merged by slug (each role takes its maximum over the
parts). Take quotes only from this part. The opening and the outline below are context:
who wrote the document (first or third party), what it is about, and which section this
part sits in (related work vs method matters for the role). Your `passages` inventory
covers this part, to its last section.

# DOCUMENT OPENING (context only, no quotes)

{{opening}}

# OUTLINE (all section headings; this part covers: {{section_labels}})

{{outline}}

# PART {{part}} OF {{parts}}

{{chunk}}
{{truncation}}
Before writing: fill `passages` section by section to the LAST section of this part, run
the facet checklist, and make every listed slug a row or a `not_graded` entry. Then emit
the JSON object described above, in a ```json fenced block, and nothing else.
