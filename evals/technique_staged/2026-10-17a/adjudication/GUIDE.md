<!-- Copied verbatim from the labelling workspace (guide v1.1) for provenance. The menu file it names
(menu-v0.9-gold.md: taxonomy v0.9 + group nodes + a 20-entry `candidate:` appendix) and the rubric
file it names (rubric-v0.4.0.md) are not part of this set. -->

# Gold labelling guide v1.1: AI-safety techniques in a document (taxonomy v0.9)

You are building a GOLD reference for measuring how completely an automatic annotator finds the
AI-safety techniques a document involves. Recall will be measured against your labels, so the
priority is COMPLETENESS: every taxonomy technique the document actually involves, in any role, at
any strength >= 1, must get a row. Precision still matters: no row without textual support.

Files you have:
- `menu-v0.9-gold.md` - the technique taxonomy v0.9 (annotator view): 214 entries with kind,
  definition, synonyms and named instances; 9 group nodes; a CANDIDATE appendix (entries proposed
  but not yet admitted).
- `rubric-v0.4.0.md` - the role-grading rubric (12 roles, strength scale 0-4, boundary precedents).
  Use its sections 1-4 and 7 as written. Where it conflicts with this guide (it was written for an
  older 292-entry catalogue with dotted variant slugs), THIS GUIDE WINS.
- the document text(s).

## What to label
1. A row = one taxonomy slug the document involves, with:
   - `slug`: a v0.9 entry slug, a group-node slug, or `candidate:<slug>` from the appendix.
   - `roles`: the rubric's 12 roles (`mentioned, cited_work, discussed, used, evaluated, modified,
     introduced, analyzed, attested_use, suspected_use, considered, critiqued`), integers 1-4,
     omit zeros. `mentioned` >= every other role of the same row.
   - `quote`: one verbatim span from the document (<= 200 chars) that best supports the row;
     copy exactly, do not paraphrase or fix typos.
   - `confidence`: `high` (clearly present, slug clearly right), `med` (presence or slug choice is
     arguable: say why), `low` (borderline: say why).
   - `reason`: one short sentence (why this slug, and why this strength for the top role).
   - optional `implemented_with`: generic ML methods the document states were used to implement
     THIS technique (e.g. "LoRA", "DPO", "LLM-as-judge", "linear probe").
2. Sweep the whole document section by section (abstract, intro, related work, method, baselines,
   experiments, results tables and captions, discussion, limitations, appendices). Typical misses
   to avoid: techniques described in plain words without their name ("we ran the agent in an
   isolated container" = sandboxing); secondary methods and baselines; evaluation methods the
   paper itself applies (`used`); recommendations and proposals (`considered`); techniques the
   document says a deployed system uses (`attested_use`, system/model cards); techniques it argues
   against (`critiqued`); related-work lists (each named technique gets at least `mentioned: 1`
   and usually `cited_work: 1`).
3. A technique that appears ONLY in the bibliography/reference list (a paper title) and nowhere in
   the body gets NO row.
4. Grain:
   - Pick the most specific entry the text supports. Entries marked `{child of: X}` are children
     of X: grade the child; grade X only for material about X as such.
   - Counting rule: a specific child replaces the generic parent; co-occurring procedure / target /
     setting labels are not independent evidence (e.g. a bio-risk capability evaluation is graded
     on the bio-domain entry, not additionally on the generic dangerous-capability entry, unless the
     document also treats the generic one separately).
   - Group nodes only for generic mentions of a field or umbrella ("mechanistic interpretability",
     "AI control") where no specific child applies. If the document names the umbrella AND uses a
     specific child, grade the child; add the group node only if the umbrella itself is discussed
     substantively.
   - Generic ML methods (RL, PPO, GRPO, DPO, reward modelling, SFT for capability, LoRA, distillation,
     LLM-as-judge, rejection sampling, best-of-N, sparse autoencoders, activation patching, RAG,
     synthetic data, ...) are NOT techniques. A document that merely uses them gets no row for
     them. When they implement a safety technique, record them under `implemented_with` of that
     technique's row. A safety-specific configuration with its own entry (e.g. RLHF as preference-
     based alignment training, refusal training, adversarial training) is a technique - check the
     menu.
   - Named systems, papers, products and benchmarks map to the entry whose mechanism they are
     (the menu lists many as "e.g." instances); a benchmark used to evaluate is graded on the
     evaluation-type entry it belongs to, if any.
   - The alias lists are NOT exhaustive. Resolve identity from definitions.
5. `extra_techniques`: AI-safety techniques the document materially engages (any role >= 2, or
   `introduced` at any strength) that fit NO menu entry and no candidate. Give `name_as_written`,
   a one-sentence `gloss`, `nearest_slug` (or null), `roles`, `quote`.
6. Clarifications (guide v1.1):
   - Two entries both fit (sibling ambiguity): pick the better one as `slug`, put the other(s) in
     `alt_slugs`, confidence `med`, and say why in `reason`.
   - A classifier, judge or detector used only as the implementation of another technique (e.g. a
     classifier filtering pretraining data) is graded on that other technique (here the data-
     filtering entry) with the classifier under `implemented_with`; grade the classifier entry
     itself only when the document treats the classifier as a safety layer in its own right
     (serving-time moderation, guard model) or describes it substantively.
   - First-party statements of current deployment practice ("we deploy X", "access is restricted
     to Y", "will initially be available only to vetted testers" as the release mode) are
     `attested_use`; future commitments or options are `considered`.
   - Safety techniques the document engages that fit no entry but stay below the extras threshold
     go to `menu_gaps` (surface + short note).
7. Do not label techniques by topic adjacency. A paper about data filtering does not thereby involve
   deduplication.

## Output (one JSON object per document, in a ```json block)
{"item_id": "an-00NN",
 "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
 "techniques": [{"slug": "...", "roles": {"used": 3, "mentioned": 3}, "quote": "...", "confidence": "high", "reason": "...", "implemented_with": ["..."], "alt_slugs": []}],
 "extra_techniques": [{"name_as_written": "...", "gloss": "...", "nearest_slug": null, "roles": {...}, "quote": "..."}],
  "menu_gaps": [{"surface": "...", "note": "..."}],
 "doc_note": "anything a reconciler should know (truncation, garbled text, unclear scope)"}

Before emitting, re-scan the menu once by kind (intervention, monitoring, assessment) and ask for
each block "does the document involve any of these?" - this second pass is where completeness
is won.
