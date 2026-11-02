---
version: 0.6.0
status: draft — 0.5.3 plus the smoke-review fixes (11 docs, 2026-10-16) and the always-call decision (2026-10-17)
date: 2026-10-17
scope: annotate.model_roles (registry version 3) — model detection and role grading in one stage
depends_on: configs/models.yaml vocabulary (v0.5; v0.6 pending), the menu as rendered by render.menu_text
changelog:
  - 0.6.0 — empty and weak-only menus are judged (§1; §5 "Empty menu": list
    every named model under extra_models); product-name rule, boxed in §5
    (a lone ChatGPT is a `gpt` family mention, never `matcher_fp`; precedent 9
    points to it); refinement: a row may name the specific model the text
    determines (`refined_name`), replacing 0.5's "do not re-attribute a family
    mention" (§5, §6); menu shows the matched text as written (§5);
    grain rule: standard Instruct/Chat/IT/Base releases are the node (§5);
    precedent 5 extended to system-card addenda (documented model and the card
    being extended `introduced`, base model `modified`, comparison columns
    `evaluated_behavioral` by prominence); `discussed` excludes first-party
    documentation and own-result interpretation (§3.3); "one sentence → 1"
    rule restated first in §2.
  - 0.5.3 — quote-selection preference for method-naming quotes (join
    anchoring; from the technique-model join design).
  - 0.5.2 — pilot fixes (98-doc live pilot, hand-audit): `not_found` flag added
    (absence is not `bib_only`); every all-zero row must carry a flag; family
    rows grade only on text naming no specific version (anti-bleed);
    analyzed_internals explicitly includes gradient/influence-function methods.
  - 0.5.1 — usage_experience precision (9-doc genre probe): advice/second-person
    prescriptions excluded (→ discussed); first-person-report requirement;
    single-sentence own-use anchored at 1; "tips-and-tricks" wording removed.
  - 0.5 — round-2 repairs (Tomáš go, 2026-08-02): built_on positive definition
    (system-as-contribution) + precedent 7 forked into contribution vs
    measurement-harness with round-2 doc exemplars; subsumed guard tightened to
    menu-node-or-variant-row targets (extra_models never); variant rows
    generalized to any named sub-node model (members and derivatives).
  - 0.4.1 — variant rows (Tomáš 2026-08-02): named public derivatives of menu
    models grade as variant_models rows keyed to the base node (all
    derivatives of one base share one row; concrete names recorded);
    extra_models narrowed to no-menu-base models.
  - 0.4 — calibration fixes (4-judge, adjudicated): built_on/instrument
    operational test (the broken axis, 0% cross-judge exact); subsumed guard
    (family stays graded when the specific model is off-menu); matcher_fp
    scope caution; worked merely-mentioned + bib-only exemplars; §8 note on
    kind-stratified calibration samples.
  - 0.3 — experience-pass fixes (20 docs, Sonnet 5): anaphora precedent 14 +
    `subsumed` flag; functional-load-over-textual-footprint rule in §2;
    bib-adjacent deletion-test precedent 15 (old 14 → 16); §6 schema example
    fixes (mentioned in extra_models, evidence roles list).
  - 0.2 — critic fixes: own-work rule over precedents; commentary layering
    (cited_work vs discussed); agent-eval backbone ruling; modified attribution
    (base + derivative, third-party); baseline-row provenance default; doc_kind
    enum widened; precedent-3 cap removed; mentioned operationalized; variant
    checkpoints and unnamed models; quote-count fix.
  - 0.1 — initial draft.
---

<!-- Not sent to the judge: the rubric's metadata (above) and its design notes (below). The judge sees
rubric-v0.6.0.md, which prompts/model-roles-v2.md includes. A new rubric version is a new pair of files. -->

## 8. Design notes (not part of the judge prompt)

- **Why prominence, not confidence.** md_quality calibration measured
  self-reported judge confidence as flat-to-inverted; the field was dropped.
  Strength carries prominence; disagreement is measured across judges, not
  self-reported.
- **Why 5 levels.** More degrees invite central-tendency mush and cross-judge
  noise; fewer lose the Zvi pair (§2). If the sample pass shows systematic
  in-betweenness, half-steps can be allowed without redefining anchors.
- **Independence (self vs third party) is not graded here** — it derives from
  document origin metadata, kept orthogonal by design.
- **Exemplar replacement.** All *(constructed)* exemplars are to be replaced
  by verbatim corpus snippets harvested during the 20-doc sample pass.
- **Calibration samples must be kind-stratified.** A random paper-heavy draw
  exercised only 8 of 10 axes (`introduced` and `usage_experience` never
  fired in 1,180 cells) — include model/system cards and usage-report posts
  deliberately.
