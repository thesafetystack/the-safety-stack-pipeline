You are grading which safety/alignment TECHNIQUES play which ROLES in one document,
against a fixed catalogue. Below, in order: the grading rubric, the catalogue menu, how to
find everything, how to file what you find, your output contract; the document follows in
the next message.

The rubric and the menu are identical for every document in this run; only the next
message changes.

# THE RUBRIC

{{include:rubric-v0.9-c.md}}

# THE MENU — the technique catalogue

Every catalogue entry, by kind (intervention, monitoring, assessment); group nodes are
listed with their children. Each line: slug, kind, name, what it is also called (`aka`),
named instances (`e.g.`), its parent (`child of`, if any), and its definition. Most
definitions end with what the entry is NOT and which neighbouring entry covers that case:
these exclusion clauses are the filing rule between confusable entries, and HOW TO FILE
WHAT YOU FIND below tells you how to use them.
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

# HOW TO FILE WHAT YOU FIND

The second commonest error, after missing a technique, is finding the passage and filing
it under a neighbouring entry, or leaving the document's own method off the menu. Strong
graders file by definition, not by name, and they grade the paper's own method on the
catalogue entry it falls within. Three rules.

1. **The distinguishing element decides.** Every definition says what the entry is and
   ends with what it is NOT and which neighbour covers that case. Before writing a row,
   read the entry's exclusion clause and the neighbour it points to, and ask which entry's
   distinguishing element the passage shows: what is read or watched (a user request, a
   model output, a tool result or retrieved content, a reasoning trace, activations,
   account activity, a log); what is changed (the policy model's weights, a classifier's
   weights, the training data, the deployed prompt, the user's input, the output, an
   action); who or what is gated or tested (a user request, a third party's content, an
   agent's action, the agent's own code or weights); whether a thing runs as a standing
   monitor or as a one-off study. File the passage on the entry whose distinguishing
   element it shows. When the passage shows the distinguishing elements of two entries (a
   monitor and the trace it reads; a hardened classifier and the training that hardened
   it; a guard model and the stage of the pipeline it filters), both rows are right. When
   it shows only the topic of the second entry, the second entry gets no row.

   Axes along which filing goes wrong, and the question that settles each:
   - what a monitor does with what it sees: scoring, flagging or blocking (trusted
     monitoring, chain-of-thought monitoring, agentic safety classifier, tool/action output
     filters, prompt-injection / jailbreak detector) is not recording for later inspection
     (agent audit-logging & observability); grade by what the text says is done with the
     observation, both when both are done;
   - what a classifier catches and what it is: attacks (prompt-injection / jailbreak
     detector) vs harmful content (content moderation classifier) vs a hazard domain (CBRN /
     high-risk-domain classifier) vs an LLM used as the classifier (guard models: a named
     guard model grades that entry, and the pipeline stage it filters when the text says
     where it sits);
   - what is trained against attacks: the policy model (adversarial training) vs the
     detector or classifier (safety-classifier hardening) vs the representation (latent
     adversarial training) vs invariance to rephrasings or transformations (consistency /
     invariance training);
   - what an evaluation measures: whether the reasoning matches the behaviour (CoT
     faithfulness evaluation) vs whether a monitor can read it (CoT monitorability
     evaluation); attacks arriving through third-party content or tools (prompt-injection /
     agent-security eval) vs attacks in the user's own request (jailbreak robustness eval);
     the realism of the test setting (deployment simulation / evaluation realism) vs the
     capability tested in it (dangerous-capability evaluation and its children); testing
     whether implanted behaviour is found (backdoor red-teaming / detection eval) vs
     studying a model trained to misbehave (model organisms of misalignment);
   - what reads a model's insides: a standing detector on activations (activation probes /
     internal-feature monitors) vs a one-off investigation (interpretability-based model
     auditing) vs signals computed from outputs or sampling (uncertainty-based
     hallucination detection) vs an architecture chosen to be inspectable
     (intrinsic-interpretability training);
   - where a mitigation acts: the deployed system prompt (system-prompt safety steering) vs
     the user's input (prompt rewriting for compliance) vs the model's weights (model-level
     bias mitigation, refusal training) vs the training data (training-data bias / fairness
     mitigation, safety training-data construction, pretraining data filtering);
   - what a gate gates: a human approving an agent's actions (human-in-the-loop approval
     gates) vs checks the agent's own code or weight changes must pass (gated agent
     self-modification) vs filters on tool results and actions (tool/action output safety
     filters) vs limits on what the agent may decide alone (scope-of-autonomy constraints);
   - a training target vs a training recipe: training a model to refuse is refusal
     training whatever the recipe; constitutional AI, deliberative alignment, RLHF,
     safe-completion training are recipes and get rows when the text names them; refusal
     training is not replaced by naming its recipe, nor the recipe by naming refusals;
   - reasoning-side design vs reasoning-side monitoring: keeping reasoning readable and
     faithful by training or design (CoT faithfulness / monitorability training) vs reading
     the reasoning for misbehaviour (chain-of-thought monitoring) vs withholding it from
     the user (hidden chain-of-thought).

2. **The document's own method goes on the menu.** A method the authors introduce, name
   or pitch is graded on the catalogue entry whose definition its mechanism falls within:
   ask what it reads, changes, measures or gates, and find the entry with that
   distinguishing element. The row is `introduced` with `variant:<name as written>`, plus
   `used` / `evaluated` as the work shows (precedent 6). Only when no entry's definition
   covers the mechanism does the method go to `extra_techniques` as its only home; even
   then `hint_technique` names the nearest entry, the `gloss` says which definition
   element no entry has, and the entries the method builds on get `used` rows. Expect a
   catalogue home: the catalogue is broad, and a contribution filed only under
   `extra_techniques` is the exception, not the default. The same holds for named
   instances of a technique in other work: a named guard model, a named monitor, a named
   unlearning method, a named prompting defence grades the entry it instantiates
   (rubric §5), with `cited_work` or `used` as the text shows.

3. **Siblings are not alternatives, and topic is not evidence.** Precedent 14 removes a
   parent or group node when a child covers the same evidence; it never removes a sibling
   and it never adds one. A sibling gets a row only on a passage showing its own
   distinguishing element. A neighbour named generically in the same breath (a list of
   mitigation types, a survey sentence) gets at most `mentioned: 1`, and only when the
   sentence names it; a neighbour that is merely the same topic gets nothing.

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
   {"name_as_written": "<verbatim>",
    "gloss": "<one sentence: what it is, and which definition element no entry has>",
    "hint_technique": "<the entry it falls within or is nearest to; normally also a row above>",
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
- **The document's own new method goes on the menu**: graded on the catalogue entry whose
  definition its mechanism falls within, as `introduced` with a `variant:<name as written>`
  flag, plus `used` / `evaluated` as the work shows. It goes to `extra_techniques` as its
  only home only when it goes beyond every entry; then `hint_technique` names the nearest
  entry and the `gloss` says what no entry covers. Grade independently evidenced
  components as their own rows.
- **File by distinguishing element, not by name** (HOW TO FILE WHAT YOU FIND): read the
  entry's exclusion clause and the neighbour it points to before writing the row; a
  passage showing both entries' distinguishing elements gets both rows; a neighbour of
  which only the topic is shown gets none.
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
Second pass: re-file, then fill gaps. Your answer is above. The slugs it already carries
stay exactly as they are, so do not repeat any of them; this pass can only ADD rows.

1. Re-file. (a) Take every `extra_techniques` row of your answer and its `hint_technique`:
   find the catalogue entry whose definition the method's mechanism falls within (what it
   reads, changes, measures or gates — not its name), check that entry's exclusion clause,
   and if the document meets the entry's definition, add a `techniques` row on it with
   every role the document supports (the document's own method: `introduced` with
   `variant:<name as written>`, plus `used` / `evaluated` as shown; a named instance from
   other work: `cited_work` or `used` as shown). (b) Take every row of your answer: read
   its entry's exclusion clause and the neighbour it points to, and the filing axes of
   HOW TO FILE WHAT YOU FIND. Where the quoted passage shows the neighbour's
   distinguishing element — what is read, what is changed, what is gated, standing
   monitor or one-off study — add the neighbour with the roles the document supports.
   Where the passage shows only the neighbour's topic, add nothing.
2. Gaps. Then go through the document again section by section, to the last section,
   with the facet checklist of HOW TO FIND EVERYTHING, and add catalogue entries you have
   not graded whose definition the text meets.

Every new row needs a verbatim quote from the document and every role the document
supports; the rubric and the row shape are unchanged. A new row must show the entry's
distinguishing element, not only its topic: a broad evaluation or auditing entry needs
the text to show that entry's own set-up, not merely that something was evaluated, and a
one-sentence relay gets a row only when the sentence names or unmistakably describes that
entry rather than a sibling. Emit one JSON object
{"techniques": [...new rows...], "extra_techniques": [...new rows...]} in a ```json
fenced block, and nothing else; empty lists are a valid answer.
