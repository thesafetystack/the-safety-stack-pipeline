You are grading which safety/alignment TECHNIQUES play which ROLES in one document,
against a fixed catalogue. Below, in order: the grading rubric, the catalogue menu, your
reading procedure, your output contract; the document follows in the next message.

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
than a wrong row. Most misses are not names overlooked: they are practices described
without their name, secondary material in dense sections, material in the last quarter of
the text, and one-sentence relays. The rules against inventing rows stay in force (rubric
§1: a verbatim quote, a definition the text meets). Within them, read in four passes;
each pass adds candidates to `passages`.

**Pass 1 — the walk.** Go section by section from the first heading to the LAST line of
the supplied text, appendices and supplementary material included; a reference list ends
the document only when nothing follows it. For each section, note the technique-bearing
statements; a section may yield nothing. Then look at the last quarter of the text once
more on its own: appendices, additional experiments, defences and mitigations discussed
after the main results, release notes and what is withheld, limitations and discussion,
ethics, data-work and crowd-work statements, acknowledgements that describe a safety
practice. These carry rows in most long documents and are where a first reading goes thin.

**Pass 2 — the secondary material.** In every section that yielded a technique, look
again at what sits next to it: each measured dimension or scored trait of an evaluation
(each can be its own assessment entry — honesty, sycophancy, bias, over-refusal, reward
hacking, ethical behaviour, dangerous capabilities, evaluation awareness); each
sub-evaluation and stress test; each one-sentence attested practice (what ran in
isolation, what was withheld or released in stages, what was reviewed, replayed, filtered
or suspended); each component of the set-up (environment or user simulation and its
realism; elicitation effort and scaffolds; judge, grader or verifier validation;
multi-turn or adaptive attacks; contamination control; checks for reward hacking); the
safety training of the models the authors build on (precedent 5). Dense documents —
system cards, long reports, surveys — hold most of their rows here, at strength 1–2, and
a strength-1 row in a dense document counts exactly as much as one in a short post.

**Pass 3 — practices in plain words.** Safeguards and evaluations are usually described,
not named. Wherever the text says what was done to inputs, outputs, accounts, training
data, tools, reasoning traces, releases, weights or architectures — filter, screen, block,
suspend, review, inspect, score, flag, isolate, withhold, instruct, replay, neutralise,
verify, gate, log — ask which entry's definition describes that action, and grade the
entry although no name, alias or acronym appears. Match the definition's mechanism (what
is read, what is changed, what is tested), not its vocabulary. Plain-words forms you will
meet: a stricter limit for some users or accounts (access tiers); misuse found by
reviewing account activity and acting on the accounts (abuse / behavioral monitoring);
tools or agents run in an isolated environment (execution sandboxing); a variant withheld
or a capability disabled before release (staged / structured release); instructions in
the deployed prompt that shape safety behaviour (system-prompt steering); reasoning shown
only as a summary (hidden chain-of-thought); agent logs or reasoning read for unsafe
plans, by what is done with them (chain-of-thought monitoring, or audit logging when they
are only retained); claims checked against tool output or references (truthfulness /
groundedness evaluation); an architecture chosen because it is easier to analyse
(intrinsic-interpretability training); human comparisons used to improve a model (RLHF);
a model's plan or code checked before it may act (approval gates, gated self-modification).

**Pass 4 — relays and positions.** Related work, introductions and discussions relay
other work's claims one sentence at a time, and forum posts, position papers and policy
sections recommend, weigh and guess. A sentence relaying a specific claim about a
technique (a result, a mechanism, a failure) is a `cited_work: 1` row; a sentence
proposing, urging or setting a technique aside is `considered` (precedent 9); a guess
about what a deployed system runs, with any evidence offered, is `suspected_use` (§3.10);
a technique thought through as a design option or a future application is `considered`
or `discussed` at 1–2. These rows are small, and strong graders do grade them. The guard:
the sentence must name or unmistakably describe THAT entry — a sibling named only
generically, or a title in the bibliography, is not a row.

Within every pass:
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
3. A broad entry needs its own set-up shown. The broad assessment entries (automated
   behavioral auditing, multi-turn evaluation, the generic capability and harm
   evaluations) are rows when the text shows that entry's defining set-up, not because an
   evaluation of some kind took place; a passage that fits a specific entry's definition
   goes on the specific entry.

Facet checklist: run it once over the whole document after the passes.
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
   "passages": [                          // document order, one line per section or run of
     {"where": "<section heading, short>",  // adjacent sections, to the LAST section of the text;
      "what": "<=8 words: what is done or said>",  // at most 60 lines: merge adjacent sections
      "slugs": ["<EVERY menu slug the section meets>"],  // and keep the union of their slugs
      "none": "ml_method_only | too_vague | not_a_technique | menu_gap"}],
   "deployment_claims": [                 // §7 step 4: claims or guesses, hedged ones included,
     {"system": "<named system>",          // about what a deployed or released system was
      "technique": "<menu slug or surface>",  // trained or served with
      "quote": "<verbatim, short>",
      "direction": "uses | does_not_use"}],
   "recommendations": [                   // §7 step 5: should / must / need to / recommend /
     {"technique": "<menu slug or surface>",  // urge / propose / can reduce
      "quote": "<verbatim, short>",
      "direction": "recommended | rejected"}],
   "controls_discussed": [                // §7 step 6 (commentary, forum, policy, news): every
     {"surface": "<as written>",           // control, mitigation or technique the text talks
      "technique": "<menu slug, or omit>"}],  // about, in whatever words
   "menu_gaps": ["<safety technique named in the text that has no menu slug>"]},
 "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
 "techniques": [
   {"slug": "<menu slug>",
    "roles": {"<role>": <1-4>, ...},      // only nonzero roles; omit the rest
    "quotes": ["<verbatim from the document, <=200 chars; the clause that names or describes the technique>"],   // REQUIRED, at least one
    "flags": ["recommended"],              // optional, see the flag vocabulary below
    "note": "<optional, short; omit when nothing needs saying>"}],
 "extra_techniques": [
   {"name_as_written": "<verbatim>", "gloss": "<one sentence>",
    "hint_technique": "<nearest menu slug, or omit>",
    "roles": {...},
    "quotes": ["<verbatim>"]}],                                // REQUIRED, at least one
 "not_graded": [{"slug": "<a slug listed in passages or a sweep>",
                 "why": "not_in_text | parent_of_graded_child | bibliography_only"}],
 "flags": ["no_techniques_found", "menu_gap:<surface>"]}
```

A passage line has `slugs` or `none`, not both. The last `passages` line is the last
section of the supplied text; if the last quarter of the document produced no line, read
it again before going on. Every slug listed in `passages` or in a sweep must end either
as a row of `techniques` (with every role the whole document supports) or in `not_graded`
with its reason. `not_graded` is for slugs you listed and then found the text does not
support; it is not for minor techniques (those are strength-1 rows).

Budget. The output is capped; a dense document can need sixty or more rows, and every
row needs its quote. Spend the output on rows, not on bookkeeping: quotes are the clause
that names or describes the technique (rarely more than 120 characters; never more than
200), notes are omitted unless a rule asks for one, sweep quotes are short, `passages`
lines are merged rather than multiplied. A supported row found late in the reading is
never dropped to save space — shorten the bookkeeping instead.

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
# THE DOCUMENT

{{document}}
{{truncation}}
Before writing: run the four passes of HOW TO FIND EVERYTHING to the LAST line of the
supplied text — the walk with its second look at the last quarter, the secondary material
of every dense section, the practices described in plain words, the relays and positions
— fill `passages` as you go, run the facet checklist, and make every listed slug a row or
a `not_graded` entry. Then emit the JSON object described above, in a ```json fenced
block, and nothing else.
