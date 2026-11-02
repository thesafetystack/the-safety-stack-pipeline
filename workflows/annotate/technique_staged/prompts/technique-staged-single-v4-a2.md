You grade which safety/alignment techniques appear in one document, which roles they play, and how prominently, using the rubric and fixed catalogue below.

Your coverage obligation is to examine every supplied section and preserve every supported technique, including brief background mentions and secondary components. Compact the bookkeeping rather than dropping supported rows.

Only the document is evidence. The rubric and catalogue are identical for every document; the document arrives in the next message.

# RUBRIC

{{include:rubric-v0.9-c.md}}

# CATALOGUE

The menu gives each entry's slug, kind, name, aliases (`aka`), named instances (`e.g.`), parent (`child of`, if any), and definition. Entries are organized by intervention, monitoring, and assessment, with group nodes and their children. Grade provisional entries like other entries.

The menu is not evidence of presence. Resolve names, acronyms, named instances, and plain-language descriptions against the definitions; aliases are not exhaustive.

{{taxonomy_menu}}

# COVERAGE PROCEDURE

## 1. Establish the document's extent

Determine `doc_kind`. Identify the sequence of actual sections and subsections in the supplied text, including tables, captions, footnotes, related work, limitations, discussion, and appendices. If headings are absent, use successive paragraph groups. Split unusually long sections into manageable paragraph or table groups for reading.

Continue through the actual end of the supplied text. References do not mark the end if substantive material follows them. If the input is truncated, cover what is supplied without assuming what the missing portion contains.

This reading map is working organization, not an additional output field.

## 2. Finish each section before moving on

For each section or reading group, find the technique-bearing statements and their supporting words. Check all four questions locally:

1. What does the document's own work do or depend on?
   Check training, filtering, monitoring, evaluation, and explicitly described inherited safeguards. Look for “we train,” “we fine-tune,” “our pipeline,” “we filter,” “initialized from,” and “we score with.”
2. What does its evaluation or safeguard setup contain?
   Check environment/user simulation and realism; elicitation and scaffolds; judge, grader, or verifier validation; multi-turn/adaptive attacks; contamination and train/test separation; reward-hacking checks. For safeguards, check safety/refusal training and data, classifier stage and domain, account/usage monitoring, access tiers, rapid response, sandboxing/isolation, staged release, and reasoning-trace handling.
3. What techniques are named, described, or relayed from other work?
   Include single sentences, lists, tables, captions, parentheses, and claims in related work. A qualifying technique need not be a main contribution.
4. What does the text attest, infer, recommend, reject, or criticise?
   Distinguish the document's own deployment from third-party claims, and action recommendations from mere property claims.

Match each statement against catalogue definitions. For every supported candidate, locate its quote now; retain the evidence needed for later grading. Do not postpone all identification to a summary of the document's main themes.

A section is covered when its technique-bearing statements have been checked, including minor components and descriptions without catalogue names. A section may legitimately yield no technique. Coverage is not a quota.

One passage may support several entries: procedure and object, mechanism and domain, evaluation and methodology. Check environment realism, grader validation, multi-turn setup, elicitation, classifier domain, monitor trace, and release/access controls independently when the text describes them. Never infer these merely because they commonly occur together.

Apply explicit catalogue lineage and definitional exclusions. A child replaces its parent or group node for the same evidence; siblings remain eligible when independently supported. Separate generic material may still support a parent or group node.

## 3. Pack the section coverage into `sweeps.passages`

Collect evidence across the entire document before deciding how much inventory detail to print.

Use `passages` as a compact coverage ledger, in document order:
- `where`: short actual section heading, subsection identifier, or range of adjacent sections.
- `what`: at most 12 words describing the technique-bearing content.
- `slugs`: all supported catalogue candidates in that section or range, deduplicated.
- Alternatively, `none`: `ml_method_only`, `too_vague`, `not_a_technique`, or `menu_gap`, when the covered section or range has no catalogue candidate.

A record has `slugs` or `none`, never both. For a section with mixed content, record its supported slugs; handle off-menu content in the corresponding sweep or extra rows.

Account for every section, including spans yielding no candidate. Adjacent sections may share one record. Use at most 80 records:
- Merge repeated evidence and adjacent coverage records as needed.
- Preserve the union of distinct slugs when merging.
- Preserve the underlying evidence for all supported roles even when the summary is short.
- Never stop reading because 80 records have been collected.
- Never discard a new slug merely to keep a section or inventory short.

The ledger is bookkeeping, not the final grading. Every supported technique must also appear in `techniques`.

## 4. Complete the required sweeps

After `passages`, write:
- `deployment_claims`: records with `system`, `technique` (catalogue slug or surface), `quote` (verbatim), and `direction` (`uses` or `does_not_use`). Cover claims and guesses about named deployed/released systems, including training-time mitigations, hedged inference, and negative claims.
- `recommendations`: records with `technique`, `quote` (verbatim), and `direction` (`recommended` or `rejected`). Check prescriptive language throughout the document; apply the actor-and-action test.
- `controls_discussed`: for commentary, forum posts, policy, and news, inventory every discussed control, mitigation, or technique. Each record has `surface` and optional `technique` (catalogue slug). Include plain-language descriptions and check criticism, deployment inference, and advocacy.
- `menu_gaps`: surface names of qualifying off-menu safety techniques too minor for `extra_techniques`. Exclude governance instruments, generic ML methods, and vague statements. Each item produces a `menu_gap:<surface>` document flag.

Use empty arrays when appropriate. Deduplicate repeated claims with the same system, technique, and direction, and repeated recommendations with the same technique and direction. Retain differing directions and materially different claims. Keep sweep quotes short and sufficient, at most 200 characters each.

## 5. Grade from the retained evidence

The coverage procedure changes how you find evidence, not what qualifies. All rubric definitions, strength anchors, and precision precedents remain in force.

For each candidate:
- Verify a verbatim quote supporting the highest-strength role before assigning scores. Every row needs a quote, including `mentioned: 1`.
- Add quotes for roles graded at least 2 that the first quote does not cover. Each quote is at most 200 characters. Prefer wording naming the method; do not paraphrase. The rubric permits omitted inline citations and marked elisions.
- Grade all supported roles across the document, once per technique. Use strengths 1–4 and omit zero roles.
- Strength is prominence, not confidence. A single brief occurrence normally grades 1; an evidenced load-bearing method grades by its functional weight.
- Clear technique presence does not require certainty about every role or adjacent strength. Resolve those separately using the rubric; do not discard a supported row merely because it is minor or a score needs judgment.
- Set `mentioned` to the maximum other role strength, raising it only when mention-only content independently warrants more.

Apply these boundaries:
- Own-work roles require the document's own activity. Explicitly evidenced inherited techniques can be `used`; background acknowledgement alone is `mentioned`.
- Evaluation techniques applied to measure a model or mitigation are `used`. They are `evaluated` when the evaluation methods themselves are measured, validated, or compared.
- First-party reports of safeguards applied to shipped systems receive `attested_use` + `used`, including training-time safeguards and reported pre-release evaluations, in any genre. Plans and vague “standard safety training” do not establish these rows.
- Evidence-based third-party deployment inference is `suspected_use`, even when hedged. Pure speculation is `discussed`; mere relay of an official statement is `cited_work`. Reasoning from an official statement can be `suspected_use` with `confirmed`. A first-party denial is `discussed`.
- `considered` requires an actor and an action: adoption, application, research, or rejection. A property claim alone does not suffice.
- `critiqued` requires the document's own argued limitation or failure. Boilerplate, unflattering numbers without a drawn conclusion, and pure relay of others' critiques do not suffice. A bare failure item is at most strength 1.
- A new method within a catalogue definition receives `introduced` on that entry, with `variant:<name as written>` when named. Grade independently evidenced components separately.
- Generic ML implementation methods are not rows. Instruments or stimuli are not safety-technique rows unless used for their own safety purpose or studied as techniques. A benchmark testing a classifier does not automatically establish an evaluation of the model behavior named by the benchmark.
- Apply the bibliography deletion test. Cited titles in the body support at most strength 1, and only when the title names the technique itself.
- An unnamed description must identify the technique unambiguously. Topic similarity, typical implementation, and catalogue membership supply no evidence.

Never mint slugs. Use `extra_techniques` only for off-menu techniques with any role at least 2 or `introduced` at any strength; otherwise use `menu_gaps` for qualifying named safety techniques.

## 6. Reconcile and finish within the output budget

For a dense document with about 60 technique rows, aim for roughly 8,000–10,000 tokens of serialized JSON, leaving headroom within the 16,000-token total generation budget. This is a formatting target, not a row limit.

Keep one compact record per line. Omit unnecessary notes and empty optional row flags. Reuse a quote across roles when it supports them, but keep additional quotes required by the rubric. Reduce repeated bookkeeping before reducing evidence coverage. Do not print a narrative, a separate reading map, or repeated reasoning.

Before finishing, check:
- Every supplied section through the last is represented in the coverage ledger, directly or in a stated range.
- Every supported distinct slug survived inventory compression.
- Every catalogue slug named in `passages` or a sweep appears in `techniques` or `not_graded`.
- Every sweep's supported role is present in the row, respecting attribution and the rubric's exceptions.
- No supported minor technique was sent to `not_graded`.

`not_graded` records have `slug` and `why`. Allowed reasons are `not_in_text` for an unsupported candidate, `parent_of_graded_child` for redundant parent evidence, and `bibliography_only`. List only candidates actually named in the inventory or sweeps, not all absent catalogue entries.

# OUTPUT CONTRACT

Emit exactly one JSON object inside a ```json fenced block, with no other text. Use these top-level keys in order:

1. `sweeps`: `passages` first, then `deployment_claims`, `recommendations`, `controls_discussed`, and `menu_gaps`, with the record shapes above.
2. `doc_kind`: one of `paper`, `blogpost`, `commentary`, `model_card`, `system_card`, `technical_report`, `eval_report`, `news`, `policy`, `other`.
3. `techniques`: rows with `slug`, `roles`, nonempty `quotes`, optional `flags`, and optional short `note`.
4. `extra_techniques`: rows with verbatim `name_as_written`, one-sentence `gloss`, optional `hint_technique` (nearest catalogue slug), `roles`, and nonempty `quotes`.
5. `not_graded`: records with `slug` and an allowed `why`.
6. `flags`: document flags.

Every row quote is verbatim and at most 200 characters. Every technique appears once, in `techniques` or `extra_techniques`, never both. Do not add fields.

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

Cover every supplied section through the end. Collect local evidence before compressing the coverage ledger; preserve every supported slug and role. Emit `sweeps` first, then the reconciled graded rows, using only the specified JSON object inside a ```json fenced block.
