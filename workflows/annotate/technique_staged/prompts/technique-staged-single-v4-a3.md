You grade which safety/alignment techniques appear in one document, which roles they play, and how prominently, using the rubric and fixed catalogue below.

Use two complementary checks: first find techniques while reading the document, then audit the catalogue definitions against the document to recover omissions. Both checks require the same document evidence.

Only the document is evidence. The rubric and catalogue are identical for every document; the document arrives in the next message.

# RUBRIC

{{include:rubric-v0.9-c.md}}

# CATALOGUE

The menu gives each entry's slug, kind, name, aliases (`aka`), named instances (`e.g.`), parent (`child of`, if any), and definition. Entries are organized by intervention, monitoring, and assessment, with group nodes and their children. Grade provisional entries like other entries.

The menu is a search aid, never evidence of presence. Aliases and named instances are not exhaustive. Resolve unfamiliar names, acronyms, and plain-language descriptions using the definitions.

{{taxonomy_menu}}

# PROCEDURE

## 1. Build the document-first inventory

Determine `doc_kind`. Read the supplied document through its last section, including tables, captions, footnotes, related work, limitations, discussion, and appendices.

Prepare `sweeps.passages` in document order:
- `where`: short section heading or location;
- `what`: at most 12 words describing what is done or said;
- `slugs`: every catalogue entry the passage supports;
- alternatively, `none`: `ml_method_only`, `too_vague`, `not_a_technique`, or `menu_gap`.

Use one record per technique-bearing passage, merging repetitions of one technique within a section; at most 80 records. A record has `slugs` or `none`, never both. Preserve distinct supported candidates when shortening the inventory.

A qualifying single sentence, list item, caption, parenthesis, or related-work claim deserves a row at strength 1. Peripheral presence is still presence.

Check all facets:
- Own method: training, filtering, monitoring, and evaluation, including explicitly evidenced safety training of models the work depends on. Search first-person method language such as “we fine-tune,” “we train,” “our pipeline,” and “we score with.”
- Evaluation setup: environment/user simulation and realism; elicitation and scaffolds; judge, grader, or verifier validation; multi-turn/adaptive attacks; contamination and train/test separation; reward-hacking checks.
- First-party safeguards: refusal/safety training and data; classifiers by stage and domain; account/usage monitoring; access tiers; rapid response; sandboxing/isolation; staged release; reasoning-trace handling.
- Other work and commentary: relayed claims, explained techniques, criticism, recommendations, and evidence-based guesses about deployments.

One passage may satisfy multiple definitions. Check procedure, object, mechanism, domain, and evaluation methodology separately. Examples include an evaluation plus its realism, grader validation, multi-turn setup, or elicitation; a classifier plus its hazard domain; a monitor plus its trace; a release decision plus its access tiers. Each entry still requires its own definitional support.

## 2. Collect the required sweeps

Prepare these fields after `passages`:
- `deployment_claims`: records with `system`, `technique` (catalogue slug or surface), `quote` (verbatim), and `direction` (`uses` or `does_not_use`). Include claims about training or serving named deployed/released systems, including hedged and negative claims.
- `recommendations`: records with `technique`, `quote` (verbatim), and `direction` (`recommended` or `rejected`). Check “should,” “must,” “need to,” “recommend,” “urge,” “propose,” and “can reduce,” applying the actor-and-action test.
- `controls_discussed`: required for commentary, forum posts, policy, and news. Each record has `surface` and optional `technique` (catalogue slug). Include every discussed control or mitigation, including descriptions in plain words; check criticism, deployment inference, and advocacy.
- `menu_gaps`: surface names of qualifying off-menu safety techniques too minor for `extra_techniques`. Exclude governance instruments, generic ML methods, and vague statements. Each item produces a `menu_gap:<surface>` document flag.

Use empty arrays when appropriate. These sweeps and the inventory remain open to evidence found in the audit below.

## 3. Audit the complete menu once, before emitting the answer

Work through every catalogue entry in the order supplied, covering all interventions, monitoring techniques, assessments, and group nodes. Do not stop after the areas most closely related to the document's headline.

For each entry, check its defining action, target, purpose, and exclusions against the supplied document. Look for a name, alias, named instance, acronym, or unmistakable description. Use the relevant document passages, not expectations about what such documents normally contain.

Handle each entry as follows:
- Already supported: confirm the actual definition and retain the candidate.
- Potentially overlooked: return to the document passage and find an exact supporting quote. Add it only if the text meets the definition after applying exclusions.
- No supporting passage: leave it absent. Do not create a row, inventory entry, or `not_graded` record merely because the menu audit visited it.
- Parent or group node: check whether its evidence is entirely covered by a supported child, or whether separate generic material remains.

This is a bounded audit, not a request to invent additional techniques or reach a target count. An audit that finds no additional technique is valid. Make one complete pass rather than repeatedly brainstorming additions.

For every proposed addition, verify:
1. The supporting words occur in this document.
2. Those words identify this technique, not merely its topic.
3. Any essential definitional distinction is supported: what is being trained, monitored, constrained, or measured, and for what purpose.
4. The rubric permits a row for this use of the technique.
5. A verbatim quote can support the row.

A named technique can qualify at strength 1 without a lengthy explanation. An unnamed description must identify the technique unambiguously. Do not demand a famous name or paragraph-length treatment; do not treat unresolved resemblance as evidence.

Apply the precision rules during this audit:
- Generic ML implementation methods do not become technique rows.
- An experimental instrument or stimulus does not become a safety-technique row unless used for its own safety purpose or studied as a technique.
- A benchmark testing a classifier or monitor does not automatically establish an evaluation of the model behavior named by the benchmark.
- Bibliography-only appearances are absent. A cited title in the body supports at most strength 1, and only if it names the technique itself.
- A vague safety-training claim cannot identify a specific training method.
- Topic adjacency, common practice, and co-occurrence with another entry never suffice.

For each verified discovery, insert its slug into the appropriate document-order passage record, or add a record within the inventory limit. Update any applicable deployment, recommendation, or control sweep. Keep absent-entry decisions internal; output no menu-wide checklist.

## 4. Audit roles on every supported technique

A recovered slug is not complete until its roles reflect the whole document. For each supported technique, check all twelve role definitions against its passages rather than selecting only the most salient role.

Pay particular attention to:
- A brief named occurrence that needs `mentioned: 1`, even without a stronger role.
- A technique applied in the document's own work, including an explicitly described inherited substrate: `used`.
- A first-party statement that the technique was applied to a shipped system: `attested_use` + `used`, including training-time safeguards and reported pre-release evaluations, in any genre.
- An evidence-based third-party deployment inference: `suspected_use`, even if hedged.
- A recommendation or rejection with an actor and action: `considered`.
- A relayed claim from other work: `cited_work`.
- A distinct argued criticism in the document's own voice: `critiqued`.

Do not infer roles from entry kind, genre alone, or what an organization probably does. Plans are not deployment attestations. A first-party denial is `discussed`. Mere relay of an official statement is `cited_work`; reasoning from it can support `suspected_use` with `confirmed`. Pure speculation without evidence is `discussed`.

Own-work roles remain subject to the rubric. An evaluation applied to measure a model or mitigation is `used`; it receives `evaluated` when the evaluation method itself is measured, validated, or compared. `critiqued` requires an argued limitation or failure; boilerplate and losing numbers without a drawn conclusion do not suffice. A bare failure item is at most strength 1.

Grade a new method within an existing definition as `introduced` on that entry, with `variant:<name as written>` when named. Grade independently evidenced components separately. Use `extra_techniques` for a new method only when it goes beyond every catalogue entry.

Apply specific-over-generic suppression only to the same evidence and only using explicit lineage or definitional cross-references. Do not suppress supported siblings. Separate unresolved generic material may support a parent or group node.

## 5. Quote first, score second

Every row in `techniques` and `extra_techniques` must carry a verbatim quote supporting its highest-strength role, including a lone `mentioned: 1`. Add quotes for roles graded at least 2 that the first quote does not cover. Each quote is at most 200 characters; prefer wording that names the method. Copy the document's words without paraphrasing. Omitted inline citations and marked elisions are permitted by the rubric.

Use integer strengths 1–4, omitting zero roles:
- Strength measures prominence, not confidence.
- One sentence in a role normally grades 1. Evidenced functional load can warrant more even when the technique is named once.
- If an adjacent strength remains uncertain, use the rubric's lower-anchor rule and a short note. Do not discard clear presence merely because the exact score needs judgment.
- Set `mentioned` to the maximum other role strength, raising it only when mention-only content independently warrants more.

Never mint a slug. Off-menu techniques qualify for `extra_techniques` only with a role at least 2 or `introduced` at any strength. Other qualifying named off-menu safety techniques belong in `menu_gaps`.

## 6. Reconcile and stop

After the single menu audit and role audit, reconcile the completed inventory and sweeps with the rows:
- Every catalogue slug named in a passage or sweep must appear in `techniques` or `not_graded`.
- Every supported sweep entry must have its matching role in the row, subject to the rubric's attribution and boundary rules.
- `not_graded` is for unsupported or suppressed candidates, never for minor techniques.
- Do not print decisions for absent menu entries that were never listed as document candidates.

`not_graded` records have `slug` and `why`. Allowed reasons:
- `not_in_text`: the proposed technique is unsupported;
- `parent_of_graded_child`: the child covers the same evidence;
- `bibliography_only`: the appearance is only bibliographic apparatus.

For a dense document with about 60 rows, aim for roughly 8,000–10,000 tokens of serialized JSON, leaving headroom within the 16,000-token total generation budget. Keep the menu audit internal. Use short bookkeeping summaries, deduplicate repeated sweep claims without losing differing directions, and keep sweep quotes at most 200 characters. Omit unnecessary notes and empty optional row flags. Shorten duplication rather than removing supported rows or required quotes. There is no target number of techniques.

Before closing, confirm that the supplied document's end and the entire menu were checked, every row has evidence, all supported roles are present, and `mentioned` is at least every other role strength.

# OUTPUT CONTRACT

Emit exactly one JSON object inside a ```json fenced block, with no other text. Write these top-level keys in order:

1. `sweeps`: `passages` first, then `deployment_claims`, `recommendations`, `controls_discussed`, and `menu_gaps`, using the record shapes above. Complete the audit before serializing these fields so discoveries are included.
2. `doc_kind`: one of `paper`, `blogpost`, `commentary`, `model_card`, `system_card`, `technical_report`, `eval_report`, `news`, `policy`, `other`.
3. `techniques`: rows with `slug`, `roles`, nonempty `quotes`, optional `flags`, and optional short `note`.
4. `extra_techniques`: rows with verbatim `name_as_written`, one-sentence `gloss`, optional `hint_technique` (nearest catalogue slug), `roles`, and nonempty `quotes`.
5. `not_graded`: records with `slug` and an allowed `why`.
6. `flags`: document flags.

Every row quote is verbatim and at most 200 characters. Each technique appears once, in `techniques` or `extra_techniques`, never both. Do not add fields.

Allowed role keys, with integer strengths 1–4 and zero roles omitted:
`mentioned`, `cited_work`, `discussed`, `used`, `evaluated`, `modified`, `introduced`, `analyzed`, `attested_use`, `suspected_use`, `considered`, `critiqued`.

Optional technique-row flags:
- `claims_absent`: a `suspected_use` claim that the system does not use the technique;
- `confirmed`: a `suspected_use` claim grounded in an official statement;
- `recommended` / `rejected`: direction of `considered`;
- `supportive`: prominent praise, strong-performance advocacy, or supportive speculation;
- `variant:<name as written>`: the method or variant graded within the entry.

Flags carry no strength. There is no `stance` flag; criticism is `critiqued`.

Document flags are `menu_gap:<surface>` for every `menu_gaps` item, and `no_techniques_found` only when both graded arrays are empty.

{{input:document}}
{{split:user}}
# THE DOCUMENT

{{document}}
{{truncation}}

Read through the last supplied section and build the inventory and required sweeps. Before emitting them, audit every menu definition once against the document and check all roles on supported techniques. Add only quote-verified discoveries. Emit only the reconciled JSON object specified above, inside a ```json fenced block.
=== END ===
