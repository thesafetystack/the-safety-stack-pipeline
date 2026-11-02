You are grading which safety/alignment TECHNIQUES play which ROLES in one document. Earlier
steps of this pipeline proposed a short list of CANDIDATE catalogue techniques, each with
pointers to where they saw it. You read the whole document, decide which candidates it
really engages, grade each one's roles, and add any catalogue technique the candidates
missed. Below, in order: the grading rubric, how to use the candidate list, a compact
index of the whole catalogue, your output contract. The document and its candidate list
follow in the next message.

The rubric, these instructions and the index are identical for every document in this
run; only the next message changes.

# THE RUBRIC

{{include:rubric-v0.9-a.md}}

# THE CANDIDATE LIST

After the document you get the candidates: for each, its slug, name, kind (intervention,
monitoring or assessment), its parent if it has one, whether it is a group node, its full
definition, what it is also called, its named instances, and POINTERS: the surface forms,
glosses, stance hints and snippets an earlier reading step noted, and how often the
entry's name or aliases occur in the body of the text. `provisional` in a candidate's
header marks a draft entry whose definition may still change; grade it like any other.

- **Candidates are hypotheses, not findings.** Several are usually wrong: a sibling of the
  right entry, a generic method mistaken for a safety technique, a reading of a passage
  the text does not support. Reject those.
- **Pointers are not evidence.** They tell you where to look. Glosses were written by an
  earlier model and can be wrong. Your quotes must come from the document itself.
- **Keep every candidate the text supports in any role**, after applying the rubric's
  bibliography deletion test (precedent 2): a single `mentioned: 1` or `cited_work: 1`
  is a row; a reference title, citation key or URL alone supports no row. The grading
  threshold is the rubric's, not "is this important".
- **Look beyond the pointers.** A technique pointed to in one section often plays other
  roles elsewhere (used in the method, evaluated in the results, recommended in the
  conclusion). Grade every role the whole document supports.
- **Siblings.** When several candidates are siblings, grade the one or ones the text
  supports, at the most specific level the text gives; reject the others as
  `wrong_sibling`.
- **Group nodes and parents.** Use the child for evidence specific to it; keep a parent
  or group node only for separate generic evidence that no child covers (rubric
  precedents 13-14). A candidate group node or parent whose evidence is all covered by a
  child is rejected as `generic_only`.
- **The document's own new method.** Grade it under the catalogue entry whose definition
  it falls within, with `introduced` and a `variant:<name as written>` flag; grade
  independently evidenced components as separate rows. It goes to `extra_techniques`
  only when it goes beyond every catalogue entry (rubric precedent 6).
- **ML methods are never rows.** If a candidate turns out to be only a generic method
  with no safety configuration in this document, reject it as `ml_method_only`.

# TECHNIQUES THE CANDIDATES MISSED

The index below lists every catalogue entry by slug, name, kind and parent (`child of`;
definitions are not shown). The candidate list can be incomplete. When the document clearly engages a
catalogue technique that no candidate covers, add a row for it under its index slug,
with quotes like any other row. Check the name against the index carefully; when you are
not sure which entry applies, or no entry fits, report the technique under
`extra_techniques` instead (threshold: any role >= 2, or `introduced`), with its name as
written, a one-sentence gloss and, where one fits, the nearest slug as `hint_technique`.

{{catalogue_index}}

# YOUR OUTPUT

Emit one JSON object in a ```json fenced block, and no other text. Write `sweeps` FIRST:
it is required, and it is where you record the sweeps of rubric §7 before grading any
row.

```
{"sweeps": {
   "deployment_claims": [                 // §7 step 4: claims or guesses, hedged ones included,
     {"system": "<named system>",          // about what a deployed or released system was
      "technique": "<catalogue slug or surface>",  // trained or served with
      "quote": "<verbatim>",
      "direction": "uses | does_not_use"}],
   "recommendations": [                   // §7 step 5: should / must / need to / recommend /
     {"technique": "<catalogue slug or surface>",  // urge / propose / can reduce
      "quote": "<verbatim>",
      "direction": "recommended | rejected"}],
   "controls_discussed": [                // §7 step 6 (commentary, forum, policy, news): every
     {"surface": "<as written>",           // control, mitigation or technique the text talks
      "technique": "<catalogue slug, or omit>"}],  // about, in whatever words
   "menu_gaps": ["<safety technique named in the text that has no catalogue slug>"]},
 "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
 "techniques": [
   {"slug": "<candidate slug, or an index slug>",
    "roles": {"<role>": <1-4>, ...},      // only nonzero roles; omit the rest
    "quotes": ["<verbatim from the document, <=200 chars>"],   // REQUIRED, at least one
    "flags": ["recommended"],              // optional, see the flag vocabulary below
    "note": "<optional, short>"}],
 "extra_techniques": [
   {"name_as_written": "<verbatim>", "gloss": "<one sentence>",
    "hint_technique": "<nearest catalogue slug, or omit>",
    "roles": {...},
    "quotes": ["<verbatim>"]}],                                // REQUIRED, at least one
 "rejected": [
   {"slug": "<candidate slug>",
    "reason": "not_in_text | wrong_sibling | generic_only | ml_method_only | not_a_technique"}],
 "flags": ["no_techniques_found"]}
```

Every sweep entry that names a catalogue slug (a candidate or an index slug) must become
a row of `techniques` with the matching role (`suspected_use` for a third-party deployment
claim, `attested_use` for a first-party one, `considered` for a recommendation), unless on
reflection it is not in the text. `menu_gaps` lists safety techniques (by the rubric's
rules: not governance instruments, not ML methods, not vague statements) that the text
names but the catalogue (check the index) does not carry and that are not worth an
`extra_techniques` row; each becomes a `menu_gap:` document flag.

Every candidate on the list must appear exactly once: either as a row of `techniques` or
in `rejected`. Rows for index slugs that were not candidates are allowed (see above). Set
`no_techniques_found` only when both `techniques` and `extra_techniques` are empty.

Row flag vocabulary (optional, zero or more per row; they carry no strength):
`claims_absent` (a `suspected_use` row arguing the system does NOT use it),
`confirmed` (a `suspected_use` row relaying an official statement),
`recommended` / `rejected` (direction of a `considered` row),
`supportive` (prominent praise, or the document pitching the technique's strong
performance), `variant:<name as written>` (a variant or the document's own new method
graded on the entry it falls within).

The 12 roles, each an integer 1-4 (omit a role entirely when it is 0):
mentioned, cited_work, discussed, used, evaluated, modified, introduced, analyzed, attested_use, suspected_use, considered, critiqued.
`mentioned` is the umbrella and must be >= every other role for the same technique.

Reminders that the grading depends on:
- **NO QUOTE, NO ROW.** Every row must carry at least one verbatim quote from the document
  supporting its highest-strength role, at every strength. Copy the words as they stand;
  you may leave out an inline citation, but do not reword.
- **One sentence -> 1.** A technique given one sentence in a role grades 1 in that role.
  Functional load is the exception: the method behind every result grades by its weight
  even when named once.
- **An evaluation applied is `used`.** An evaluation or red-teaming exercise run to
  measure a model or a mitigation is the instrument -> `used` (+ `attested_use` in a
  first-party statement). `evaluated` is for the thing measured, or for an evaluation
  method that is itself validated or compared.
- **First-party deployment statements are `attested_use` + `used`**, in system cards and
  in any other first-party genre (blog posts, documentation), training-time mitigations
  included. A vague attestation that identifies no technique supports no row.
- **Third-party claims about a deployed model with offered evidence are
  `suspected_use`**, hedged ones included, not `mentioned: 1`.
- **Prescriptions are `considered`** with the direction flag.
- **`critiqued` needs an argument**, not limitations boilerplate.
- `mentioned` >= every other role for the same technique.
{{split:user}}
# THE DOCUMENT

{{document}}
{{truncation}}
# THE CANDIDATES ({{candidate_count}})

{{candidates}}

Now emit the JSON object described above, in a ```json fenced block, and nothing else.
