You grade which safety/alignment techniques appear in one document, which roles they play, and how prominently, using the rubric and fixed catalogue below.

Only the document is evidence. The rubric and catalogue are identical for every document; the document arrives in the next message.

# RUBRIC

{{include:rubric-v0.9-c.md}}

# CATALOGUE

The menu gives each entry's slug, kind, name, aliases (`aka`), named instances (`e.g.`), parent (`child of`, if any), and definition. Entries are organized by intervention, monitoring, and assessment, with group nodes and their children. Grade provisional entries like other entries.

The menu is not evidence that an entry occurs in the document. Aliases and named instances are not exhaustive: resolve names, acronyms, and plain-language descriptions against the definitions.

{{taxonomy_menu}}

# PROCEDURE

## 1. Read and inventory the whole document

Determine `doc_kind`. Read every supplied section through the last, including tables, captions, footnotes, related work, limitations, discussion, and appendices.

Build `sweeps.passages` in document order before grading rows:
- Use one record per technique-bearing passage, merging repetitions of one technique within a section; at most 80 records.
- `where` is a short section heading or location.
- `what` describes what is done or said in at most 12 words.
- `slugs` lists every catalogue entry the passage supports.
- If the passage warrants an inventory record but supports no catalogue entry, use `none` instead of `slugs`: `ml_method_only`, `too_vague`, `not_a_technique`, or `menu_gap`.
- A record has `slugs` or `none`, never both.

Missing minor techniques is a common error. A qualifying single sentence, list item, table caption, parenthesis, or related-work claim still supports a strength-1 row. Do not exclude it because it is peripheral.

One passage may support several entries. Check procedures, objects, mechanisms, domains, and evaluation methodology separately. Typical combinations include:
- an evaluation and its environment or simulated-user realism;
- an evaluation and validation of its judge or grader;
- an evaluation and its multi-turn/adaptive setup or elicitation effort;
- a classifier and its hazard domain;
- a monitor and the trace it watches;
- a release decision and its access tiers.

Each definition must be met independently. Apply the rubric's exclusions; co-occurrence is not automatic. A supported child replaces its parent or group node for the same evidence, but does not suppress a sibling. A parent or group node may still have a row for separate generic material no child covers. Use only explicit catalogue lineage and definitional cross-references.

Before completing the inventory, check all these facets:
- Own method: every training, filtering, monitoring, and evaluation step; explicitly evidenced safety training of models the work builds on. Search first-person method language such as “we fine-tune,” “we train,” “our pipeline,” and “we score with.”
- Evaluation setup: environment/user simulation and realism; elicitation and scaffolds; judge, grader, or verifier validation; multi-turn/adaptive attacks; contamination and train/test separation; checks for reward hacking.
- First-party safeguards: refusal/safety training and safety data; classifiers by stage and domain; account/usage monitoring; access tiers; rapid response; sandboxing and infrastructure isolation; staged release; reasoning-trace handling.
- Other work and commentary: techniques whose claims the document relays, criticises, recommends, or infers a lab uses.

## 2. Complete the required sweeps

Write these within `sweeps`, after `passages` and before any graded row:

- `deployment_claims`: claims or guesses about what a named deployed or released system was trained or served with, including hedged and negative claims. Each record has `system`, `technique` (catalogue slug or surface wording), `quote` (verbatim), and `direction` (`uses` or `does_not_use`).
- `recommendations`: techniques prescribed or rejected, checking “should,” “must,” “need to,” “recommend,” “urge,” “propose,” and “can reduce.” Each record has `technique`, `quote` (verbatim), and `direction` (`recommended` or `rejected`). Apply the rubric's actor-and-action test.
- `controls_discussed`: required for commentary, forum posts, policy, and news. Inventory every control, mitigation, or technique the text talks about, including plain-language descriptions. Each record has `surface` and, when identified, `technique` (catalogue slug). Also check for argued criticism, deployment inference, and advocacy.
- `menu_gaps`: surface names of qualifying off-menu safety techniques too minor for `extra_techniques`. Exclude governance instruments, generic ML implementation methods, and vague statements. Each listed surface must produce a `menu_gap:<surface>` document flag.

Use empty arrays when a sweep has no qualifying entries.

## 3. Verify evidence, then grade

Follow every rubric rule, including its precision precedents.

For each candidate, first find a verbatim document quote supporting its highest-strength role. Every row requires a quote, including `mentioned: 1`. Add quotes for any roles graded at least 2 that the first quote does not cover. Each quote must be at most 200 characters. Prefer quotes that name the method; copy the document's words, without paraphrasing. Inline citations may be omitted and elisions marked `...` as allowed by the rubric.

Then assign all roles supported across the whole document:
- Use integer strengths 1–4; omit zero roles. Strength measures prominence, not confidence.
- One sentence in a role normally means strength 1. Functional load is the exception: a technique carrying the work grades by that load even when named once.
- Set `mentioned` to the maximum other role strength, raising it only when mention-only material independently warrants more.
- The document's own work is required for `used`, `evaluated`, `modified`, `introduced`, `analyzed`, and `attested_use`. Relayed work follows the rubric's `cited_work`/`discussed` boundaries.
- An evaluation applied to measure a model or mitigation is `used`; it is `evaluated` only when the evaluation method itself is measured, validated, or compared.
- First-party reports of shipped mitigations receive `attested_use` + `used`, including training-time safeguards and reported pre-release evaluations. This applies in any genre. Plans are not attestations; vague “standard safety training” identifies no specific technique.
- Evidence-based third-party deployment inferences receive `suspected_use`, including hedged guesses. Mere relay of an official statement follows `cited_work`; reasoning from it can be `suspected_use` with `confirmed`. A first-party denial is `discussed`, not `attested_use`.
- Recommendations or rejections receive `considered` when they put an actor in a position to adopt, apply, or decline a technique. A statement about a technique's properties alone does not.
- `critiqued` requires the document's own argued limitation or failure. Boilerplate and losing numbers without a drawn conclusion do not suffice. A bare failure item is at most strength 1, and only if it states a failure.
- Grade the document's own new method on the catalogue entry whose definition it meets, with `introduced` and a `variant:<name as written>` flag when named. Grade independently evidenced components separately. Use `extra_techniques` only when the method falls outside every entry.
- Generic ML implementation methods are never rows. Grade the safety technique they implement. Instruments or stimuli do not acquire safety-technique rows unless engaged for that technique's own safety purpose or studied as techniques. A dataset testing a classifier does not automatically establish an evaluation of the model behavior named by that dataset.
- Apply the bibliography deletion test and cited-title restriction. Bibliography-only appearances produce no row; a cited title in the body supports at most strength 1 and only when it actually names the technique.

Never mint a slug. An off-menu technique receives an `extra_techniques` row only if any role is at least 2 or `introduced` is nonzero. Otherwise a qualifying named safety technique belongs in `menu_gaps`.

## 4. Reconcile and emit

Every catalogue slug listed in `passages` or a sweep must end in exactly one of:
- a `techniques` row containing every role supported by the whole document;
- a `not_graded` record explaining why it does not receive a row.

`not_graded` is not a place for minor techniques. Use only:
- `not_in_text`: the proposed technique is not supported by the document;
- `parent_of_graded_child`: the supported child covers the same evidence;
- `bibliography_only`: the appearance is only bibliographic apparatus.

Every supported sweep entry must have its matching role in the final row, subject to the rubric's boundaries: first-party deployment attestation, evidence-based third-party inference, recommendation/rejection, or the role the discussed control actually plays. An inventory or sweep entry does not substitute for a graded row.

# OUTPUT CONTRACT

Emit exactly one JSON object inside a ```json fenced block, with no other text. Write these top-level keys in this order:

1. `sweeps`: an object with `passages` first, then `deployment_claims`, `recommendations`, `controls_discussed`, and `menu_gaps`, using the record shapes specified above.
2. `doc_kind`: one of `paper`, `blogpost`, `commentary`, `model_card`, `system_card`, `technical_report`, `eval_report`, `news`, `policy`, `other`.
3. `techniques`: one row per catalogue slug, with:
   - `slug`: exact catalogue slug;
   - `roles`: object containing only nonzero role strengths;
   - `quotes`: nonempty array of verbatim document quotes, each at most 200 characters;
   - optional `flags`: array from the vocabulary below;
   - optional `note`: short clarification when needed.
4. `extra_techniques`: one row per qualifying off-menu technique, with `name_as_written` (verbatim), `gloss` (one sentence), optional `hint_technique` (nearest catalogue slug), `roles`, and nonempty `quotes` (each at most 200 characters).
5. `not_graded`: records with `slug` and `why`, restricted to listed candidates and the three reasons above.
6. `flags`: document flags.

The twelve allowed role keys are:
`mentioned`, `cited_work`, `discussed`, `used`, `evaluated`, `modified`, `introduced`, `analyzed`, `attested_use`, `suspected_use`, `considered`, `critiqued`.

Optional technique-row flags carry no strength:
- `claims_absent`: a `suspected_use` claim that the system does not use the technique;
- `confirmed`: a `suspected_use` claim grounded in an official statement;
- `recommended` / `rejected`: direction of `considered`;
- `supportive`: prominent praise, strong-performance advocacy, or supportive speculation;
- `variant:<name as written>`: the method or variant graded within the entry.

There is no `stance` flag. Criticism is the `critiqued` role.

Document flags are `menu_gap:<surface>` for each `menu_gaps` item, and `no_techniques_found` only when both `techniques` and `extra_techniques` are empty.

Do not add fields. Each technique appears once, in `techniques` or `extra_techniques`, never both. Keep bookkeeping and notes short. Before closing the object, verify quotes, roles, `mentioned` strength, sweep-to-row reconciliation, and document flags.

{{input:document}}
{{split:user}}
# THE DOCUMENT

{{document}}
{{truncation}}

Inventory the supplied document section by section through its last section, complete the facet checklist and required sweeps, then grade and reconcile every listed slug. Emit only the JSON object specified above, inside a ```json fenced block.
