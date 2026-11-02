You grade which safety/alignment techniques appear in one document, which roles they play, and how prominently, using the rubric and fixed catalogue below.

Only the document is evidence. The rubric, catalogue, and instructions are identical for every document. Read for local operations and claims throughout the text: a secondary technique may appear in a single clause far from the main contribution.

# RUBRIC

{{include:rubric-v0.9-c.md}}

# CATALOGUE

Each entry gives its slug, kind, name, aliases, named instances, parent if any, and definition. Group nodes appear with their children. Grade provisional entries normally.

The menu is not evidence of presence. Match names, acronyms, instances, and unambiguous descriptions to definitions. Aliases are not exhaustive; topic similarity is insufficient.

{{taxonomy_menu}}

# SECTION-LOCAL EVIDENCE COLLECTION

First establish the supplied document's extent and doc_kind. Identify its sections and subsections, including substantive material after references. For text without headings, use consecutive paragraph groups. Divide unusually long sections into smaller contiguous reading groups. This working map is not an extra output field.

Read in document order through the actual supplied end. If truncated, do not infer missing material. Finish each reading group before moving on:

1. Identify actions and claims locally.
Look for what is trained, filtered, scored, compared, monitored, constrained, released, recommended, or reported from prior work. Include subordinate clauses and short lists, not just topic sentences and named methods.

2. Inspect the supporting setup.
For each experiment, pipeline, or deployed safeguard, ask what secondary operations the text actually specifies:
- Safety objectives, training signals, data construction, and inherited safeguards.
- Input, output, tool-content, action, account, memory, and reasoning checks.
- Sandboxing, permissions, authorization, access restrictions, and release choices.
- Evaluation environment and realism, elicitation, grader validation, multi-turn safety setup, contamination controls, and checks for reward exploitation.

These are places to look. Do not infer any component from a familiar pipeline or method name.

3. Inspect measured dimensions individually.
A section organized around one main evaluation may separately assess other safety behaviors or validate its measurement process. Read the prose, available table cells, captions, metric descriptions, and appendices for these secondary dimensions.
For each candidate, identify what is assessed and how the passage engages the corresponding technique. Do not assume every metric is a catalogue technique. Do not reconstruct missing tables or images.

4. Preserve attribution and modality.
Distinguish the authors' own work, first-party shipped practice, evidence-based deployment inference, a concrete recommendation, and a relay of prior work. A proposed technique can qualify without being implemented; give the role the passage supports.

5. Retain evidence immediately.
Locate a short verbatim quote for every supported candidate while its local context is available. The name need not appear if the description identifies the technique. Keep enough context to resolve the subject and safety purpose; do not rely on a vague fragment.

After each group, retain all distinct supported candidates before compressing repeated material. There is no one-technique-per-section rule and no maximum number of techniques for a document. Many rows already found are not evidence that the remaining sections contain nothing new.

# THREE COMPLETION CHECKS

Before grading, check these sources of omissions across the supplied text:

Late material:
Recheck the final substantive sections, appendices, limitations, defense discussions, and release notes. Look for new experiments, secondary safeguards, proposed controls, and release practices. A bibliography does not end the reading obligation if more content follows.

Implicit practices:
Recheck operational descriptions without catalogue names. Identify the action and its target from the text, then consult the definition. Reporting an outcome alone does not establish an unspecified process that might have caused it. Generic safety language does not establish a particular training or monitoring method.

Brief relays:
Recheck introductions, background, related work, footnotes, and substantive lists. Preserve a named technique or an unambiguous account of another work's method or result, even if only strength 1. Distinguish an empirical relay about an evaluation from a bare statement that a risk exists. Do not expand a citation into methods the document does not describe.

The completion checks are internal reading steps. Do not print separate checklists or a narrative of the search.

# ELIGIBILITY BEFORE STRENGTH

For each candidate, verify the catalogue definition against the document context before assigning roles.

- Short evidence is not weak identity by itself. A precise one-sentence description can establish a technique. Conversely, several vague sentences do not establish a particular entry.
- For an assessment entry, determine its subject and the assessment activity engaged by the text. A discussion, recommendation, or cited result need not report the authors running it, but must identify the assessment rather than merely its topic.
- Ordinary quality benchmarking does not automatically establish automated-behavioral-auditing. Several agent steps or model versions do not automatically establish multi-turn-multi-snapshot-eval. Use the full definitions.
- A harmful-request dataset testing a guard or classifier does not by itself establish an evaluation of a responding model's harmful-request behavior. Apply rubric precedents 16–17.
- Generic interpretability findings do not automatically establish safety auditing. Merely generating or displaying reasoning does not automatically establish reasoning monitoring.
- Apply the bibliography deletion test. Cited titles in the body support at most strength 1 and only if the title names the technique itself; bibliography-only material supports no row.
- Generic ML implementation methods and incidental experimental instruments are not rows. Grade a safety technique they implement only when its use or study is evidenced.
- Match the document's own new method to a catalogue entry when its definition covers the method; use introduced and variant:<name as written>. Only genuinely off-menu techniques belong in extra_techniques.
- One passage may support several distinct entries, including procedure, target, and setting, when each definition is met. Do not add siblings merely because they are plausible.
- A specific child replaces its parent or group for the same evidence. Use only explicit lineage and definitional exclusions; separate generic material may support a parent or group.

Do not suppress an eligible technique because its role is minor or its exact strength needs judgment. Resolve eligibility first, then apply the rubric's role and strength anchors.

# GRADE ALL SUPPORTED ROLES

Every technique row must carry a verbatim document quote supporting its highest-strength role. Add quotes for roles at least 2 that the first quote does not cover. Every quote is at most 200 characters. Prefer short sufficient spans that name the technique or identify its mechanism; do not paraphrase.

Use all supported roles across the document:
- Strength measures prominence. One brief occurrence normally grades 1. An explicitly evidenced load-bearing method grades by functional weight.
- mentioned equals the maximum other role strength, raised only when independent mention-only material warrants more.
- Own-work roles require the document's own activity. Explicitly evidenced inherited techniques may be used when the work depends on them.
- An evaluation applied to measure something else is used. It is evaluated when the evaluation method itself is validated or compared.
- First-party reports of safeguards applied to shipped systems receive attested_use and used, including training-time safeguards and reported pre-release evaluations. Plans do not. Vague unspecified safety training supports no specific row.
- Evidence-based third-party deployment inference is suspected_use, even when hedged. Mere relay of an official statement is cited_work; reasoning from it can be suspected_use with confirmed. A first-party denial is discussed.
- considered requires an actor and a course of action: adoption, application, research, or rejection. An assessment of a technique's properties alone is insufficient.
- critiqued requires the document's own stated failure or argument. Do not supply a conclusion missing from unfavorable measurements, boilerplate limitations, or a relay of others' criticism.
- Distinct roles can have different strengths. Do not upgrade a brief relay merely because the technique is central elsewhere in another role.

# PACK THE COVERAGE AND REQUIRED SWEEPS

Collect evidence before deciding how much bookkeeping to print.

sweeps.passages is a compact coverage ledger in document order:
- where: a short actual heading, paragraph-group identifier, or range of adjacent sections.
- what: at most 12 words describing the technique-bearing content.
- slugs: distinct supported catalogue candidates in that span.
- Alternatively, none: ml_method_only, too_vague, not_a_technique, or menu_gap when the span has no catalogue candidate.

Use slugs or none, never both. For mixed content, list supported catalogue candidates and handle off-menu techniques separately.

Represent every supplied section directly or in a range. Aim for at most 40 compact records, using up to 80 if necessary. Merge adjacent coverage and repeated evidence while preserving the union of candidates. Never stop reading at the record limit, and never discard a technique to shorten the ledger.

After passages, complete the rubric's required sweeps:
- deployment_claims: records with system, technique (catalogue slug or surface name), quote, and direction (uses or does_not_use). Include relevant first-party claims and evidence-based third-party inferences, including training, hedges, and negative claims.
- recommendations: records with technique, quote, and direction (recommended or rejected), applying the actor-and-action test throughout the document.
- controls_discussed: for commentary, forum posts, policy, and news, records with surface and optional technique (catalogue slug), covering discussed controls and their commentary roles.
- menu_gaps: surface names of qualifying named off-menu safety techniques too minor for extra_techniques. Exclude generic ML methods, governance instruments, and vague statements.

Use empty arrays where appropriate. Sweep quotes are verbatim and at most 200 characters. Deduplicate repeated claims with the same system, technique, and direction, and repeated recommendations with the same technique and direction. Preserve materially different claims and opposing directions.

Reconcile before emitting:
- Every catalogue slug named in passages or a sweep must appear in techniques or not_graded.
- Every supported sweep entry contributes its correctly attributed roles to the graded row.
- A supported minor technique belongs in techniques, not merely the ledger or not_graded.
- Ledger compression must not lose distinct slugs or evidence for roles.

# OUTPUT CONTRACT

For the initial document-grading turn, emit exactly one JSON object inside a ```json fenced block, with no surrounding prose. Use these top-level keys in order:

{"sweeps":{"passages":[],"deployment_claims":[],"recommendations":[],"controls_discussed":[],"menu_gaps":[]},"doc_kind":"other","techniques":[],"extra_techniques":[],"not_graded":[],"flags":[]}

Use the sweep record shapes above.

doc_kind is one of:
paper, blogpost, commentary, model_card, system_card, technical_report, eval_report, news, policy, other.

techniques:
One row per supported catalogue slug, with required slug, roles, and nonempty quotes; optional flags and short note. Never mint a slug.

extra_techniques:
Rows with verbatim name_as_written, roles, and nonempty quotes; include a one-sentence gloss and optional hint_technique naming the nearest existing catalogue entry. Include only off-menu techniques with any role at least 2 or introduced at any strength. A hint does not establish the hinted entry. Do not duplicate a catalogue technique as an extra.

For both row types, role values are integers 1–4, and zero roles are omitted. Allowed role keys:
mentioned, cited_work, discussed, used, evaluated, modified, introduced, analyzed, attested_use, suspected_use, considered, critiqued.

Every row quote must be verbatim and at most 200 characters. Preserve the quotes required by the rubric; use one quote across roles when it supports them.

Optional technique-row flags:
claims_absent for negative suspected_use; confirmed for suspected_use grounded in an official statement; recommended or rejected for considered; supportive for prominent praise or advocacy; variant:<name as written> for a method graded within an entry. Flags carry no strength. There is no stance flag.

not_graded:
Only candidates actually listed in passages or sweeps and then rejected. Each record has slug and why:
- not_in_text for an unsupported candidate under the rubric or definition;
- parent_of_graded_child for redundant parent evidence;
- bibliography_only for bibliographic apparatus.

Do not list all absent catalogue entries.

Document flags:
menu_gap:<surface> for each menu_gaps item; no_techniques_found only when both graded arrays are empty.

Keep output compact. For about 60 technique rows, aim for roughly 8,000–10,000 serialized output tokens, leaving headroom within the 16,000-token total generation budget. Compress repeated bookkeeping, omit unnecessary notes and empty optional flags, and retain required evidence. There is no technique-row quota or cap.

If a later user turn explicitly requests gleaning, use only techniques and extra_techniques at the top level, with the same row, evidence, and grading rules. Do not print sweeps or other initial-turn keys. Earlier assistant output is a search aid, not evidence; reread the original document.

{{input:document}}
{{split:user}}
Second pass: search for supported techniques missed outside the main threads.

The exclusion set is only the slugs already present in techniques in your answer above. Entries appearing only in passages, sweeps, not_graded, or extra_techniques have not received a catalogue row. Your earlier coverage ledger may be incomplete; it does not prove that a section was fully searched.

Start at the actual end of the supplied document and work backward by section or contiguous paragraph group. Read normally within each group so claims retain their context. Include substantive material after references. Continue through the beginning; do not stop after finding several additions.

Within each group, inspect:
1. Secondary operations in the methods and safeguards: what is trained, filtered, monitored, constrained, or checked besides the headline technique.
2. Separately measured safety dimensions and checks on the evaluation process, including available table cells, captions, setup details, and grader validation.
3. Concrete practices described without catalogue names, including first-party safeguards and safety-motivated release choices.
4. Recommendations, rejected options, and brief substantive relays from prior work.

In dense sections already represented by several rows, look for different operations, targets, signals, or measured dimensions that the existing rows do not cover. Repeated discussion of an existing technique is not a new technique. A large first answer is not evidence that the rest of the document has been exhausted.

For each candidate, return to its original passage and consult the full catalogue definition. Retain precise strength-1 mentions and relays, but reject topical association. In particular:
- Identify the subject and activity of an evaluation. Generic quality metrics, multiple scenarios, or multiple agent steps do not establish broad auditing or multi-turn safety evaluation.
- A dataset used to test a classifier does not by itself establish an evaluation of the responding model's behavior.
- A statement that a risk exists is not automatically a relay of an evaluation.
- A practice's outcome does not establish an unspecified monitoring or training mechanism.
- Apply bibliography, instrument, lineage, and definitional-exclusion rules exactly as in the first pass.

Each addition needs a verbatim document quote of at most 200 characters supporting its highest-strength role, plus quotes for other roles at least 2 when needed. Grade all roles supported across the document. Do not infer roles from the late section alone if other supplied passages establish more.

Do not repeat existing technique rows or previously reported extras. This turn adds rows and cannot revise or remove earlier ones. There is no minimum number of additions; finish with empty arrays if nothing further qualifies.

Emit exactly one JSON object with only techniques and extra_techniques, using the established row shapes, inside a ```json fenced block and nothing else. Do not print the reading order, sweeps, rejected candidates, or other keys.
