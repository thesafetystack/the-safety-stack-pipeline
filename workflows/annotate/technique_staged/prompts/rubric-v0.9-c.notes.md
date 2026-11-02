---
version: "0.9-c"
status: draft — rubric 0.9-a plus precision precedents 16, 17 and 19 of 0.9-b (not 18); post hoc, unmeasured
date: 2026-10-17
scope: annotate.technique_staged prompt set technique-staged-v3 (included by technique-staged-single-v2)
depends_on: rubric-v0.9-a.md (byte-identical except the inserted block), rubric-v0.9-b.md (the precedents' source),
  taxonomy/taxonomy-v0.9.yaml
changelog:
  - 0.9-c — 0.9-a with a "Precision rules" block appended to §4 holding 0.9-b's precedents 16 (an instrument or
    stimulus is no row), 17 (a benchmark testing a classifier or monitor makes no evaluation-type row by itself)
    and 19 (a cited work's title in the body gives at most `mentioned: 1` / `cited_work: 1`), with their 0.9-b
    numbers. 0.9-b's precedent 18 (vague first-party mitigation statements) is left out: it contradicts gold rows
    of the eval set, where described-not-named first-party safeguards are graded. Nothing else changed (a test
    asserts that removing precedent 18 from 0.9-b and adjusting the block's first sentence gives 0.9-c).
---

`rubric-v0.9-c.md` is the rubric as the single-v2 grader sees it: a full copy of 0.9-a with the block inserted
(includes do not nest). The precedents keep their 0.9-b numbers (16, 17, 19), so a reference to "precedent 19"
means the same rule in both. This file is not sent.
