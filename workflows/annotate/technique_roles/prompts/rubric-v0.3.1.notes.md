---
version: "0.3.1"
status: draft — prototype instrument; v0.2 was measured on 10 docs / 6 judges, the
  three v0.3 categories are unmeasured
date: 2026-08-02
scope: annotate.technique_roles (prototype, off-DB) — role grading of catalogue techniques
depends_on: taxonomy/taxonomy-v0.8.yaml (292 techniques, 17 families)
changelog:
  - 0.3.1 — `suspected_use` recall fix. In the v0.3 genre run the category fired 7 times
    across 49 runs and TWO of four judges never emitted it at all, including on the three
    documents chosen to exercise it; when it did fire it was correct, so the problem was
    recall, not definition. Adds the §7.4 deployment sweep (the same remedy `llm-as-judge`
    got in v0.3) and an explicit cue sentence in the prompt output contract. No category
    or definition changed — v0.3 and v0.3.1 rows are comparable.
  - 0.3 — genre pass (design §4b, Tomáš's genre review): the nine categories were
    paper-shaped and left commentary, deployment speculation and agenda writing with
    nowhere to land. Adds §3.10 `suspected_use`, §3.11 `considered`, §3.12 `critiqued`;
    the `stance` flag is DROPPED (uncalibratable) and replaced by the `critiqued`
    category plus a bare `supportive` row flag — deliberately asymmetric, since argued
    critiques are rare and census-critical while praise is common and secondary. §4
    gains the first-party/third-party table drawing the attested/suspected border, plus
    precedents 9 (considered needs an actor and an action) and 10 (critiqued vs
    evaluated: who draws the conclusion). §7 gains an explicit judge-model check (the
    most-disputed technique in the v0.2 run) and a commentary-genre sweep.
    NOTE: design §4b calls these "v0.2 additions"; they land in v0.3 because v0.2 was
    already spent on the anti-priming pass. Rows are not comparable across the bump.
  - 0.2 — anti-priming pass, from the v0.1 smoke run (4 of 6 rows named techniques
    occurring zero times in the document). §1 gains the no-quote-no-row contract and
    the menu-is-never-evidence rule; §6's "`mentioned: 1` needs no quote" exemption
    REMOVED (it was the loophole every false row used); §3.1, §5, §6 example and §7.4
    aligned. Menu now renders `aliases`. Rows are no longer comparable to v0.1 rows.
  - 0.1 — initial draft, expanded from technique-roles-design.md §4 following the
    annotate.model_roles rubric structure (v0.5.3). Every exemplar is
    *(constructed)*; nothing here has been measured.
---

<!-- Not sent to the judge: the rubric's metadata (above) and its design notes (below). The judge sees
rubric-v0.3.1.md, which a prompt file includes ({{include:rubric-v0.3.1.md}}); this file was split from the
workflow's rubric.md (D591) without other change than the depends_on path (the taxman/ directory moved to
taxonomy/). A new rubric version is a new pair of files. -->

## 8. Design notes (not part of the judge prompt)

- **What could not carry over from model_roles.** That rubric emits one row per menu
  node *including all-zero rows*, and requires every all-zero row to carry a flag
  saying why (`bib_only`, `matcher_fp`, `not_found`). None of that is possible here: the
  menu is 292 catalogue entries rather than a per-document matcher payload, so all-zero
  rows would be 289 of 292. `bib_only` becomes a rule about *whether to emit a row*
  (precedent 2) rather than a flag; `matcher_fp` and `not_found` have no meaning without
  a matcher. Design §5's "flag mechanics carry over verbatim" does not survive contact.
- **`beyond_window` has no producer.** With no matcher there is nothing that can detect
  a technique living past the 300k-char cap. Truncation is announced to the judge and
  otherwise silent. Recall of long documents is a calibration question.
- **`attested_use` is on probation** (design open question a). It is kept as a category
  because the deployment census is a primary consumer and a flag on `used` would be
  invisible to band-level aggregation — but if calibration shows judges splitting it
  from `used` inconsistently, folding it back is the cheap fix.
- **`evaluated`/`analyzed` is the axis most likely to break** (design open question b).
  In model_roles the analogous split (behavioral/internals) had 0% cross-judge exact
  agreement before an operational test was written for it; precedent 4 is that test,
  written pre-emptively and untested.
- **`built_on` is folded into `used`** (design open question c) via precedent 5. If
  calibration shows substrate and own-application grading at systematically different
  strengths, that is the evidence for splitting it back out.
- **Exemplar replacement.** All *(constructed)* exemplars must be replaced with verbatim
  corpus snippets from the calibration sample. The slugs used in the §6 example (`dpo`,
  `ppo-for-alignment`, `rlhf`) were checked against taxonomy v0.8 and exist; re-check
  them on any taxonomy bump, since a wrong slug in the rubric teaches a wrong slug.
- **`aliases` are on the menu as of this revision.** 90 of 292 entries carry alias
  surface forms; they cost ~3.4% of the prefix and are the taxonomy's own answer to the
  identity-resolution problem that no matcher solves here. Added after the v0.1 smoke
  run, untested — watch for the opposite failure, an alias string priming a row.
- **The no-quote-no-row rule (§1) is the response to a measured failure**, not a
  precaution. In the v0.1 smoke run (deepseek-v4-flash, approach a, doc
  `18_s905gy5qt9rvj`), four of six emitted rows named techniques occurring **zero** times
  in the document — `dpo`, `machine-unlearning.npo`, `training-data-deduplication`,
  `adversarial-data-augmentation` — each with a note conceding the technique was "not
  named explicitly" or "not present in the document text", and each with an empty
  `quotes` array. Every one of them would be blocked by §1 as now written. Whether the
  rule actually holds under grading pressure is what `quote_unverified` measures.
- **Calibration samples must be genre-stratified.** `attested_use` and `introduced` will
  never fire on a paper-heavy draw; include system cards, engineering posts, and origin
  papers deliberately.
