---
version: "0.9-b"
status: draft — rubric 0.9-a plus precision precedents 16-19; post hoc (made from an error analysis of the
  0.9-a runs on the full technique_staged eval set), unmeasured
date: 2026-10-17
scope: annotate.technique_staged prompt set technique-staged-v2 (included by technique-staged-grading-v2)
depends_on: rubric-v0.9-a.md (byte-identical except the inserted block), taxonomy/taxonomy-v0.9.yaml
changelog:
  - 0.9-b — 0.9-a with a "Precision rules" block appended to §4 (precedents 16-19), from the boundary
    rulings of the eval gold, stated generally: 16 a method or technique used only as an instrument or
    stimulus is no row unless applied for its safety purpose or analysed; 17 a benchmark used to test a
    classifier or monitor makes no evaluation-type row by itself; 18 a vague first-party mitigation
    statement maps to an entry only when it clearly meets the definition; 19 a cited work's title in the
    body gives at most `mentioned: 1` / `cited_work: 1`, and only when the title names the technique.
    Nothing else changed (a test asserts that removing the block gives 0.9-a).
---

`rubric-v0.9-b.md` is the rubric as the v2 grader sees it. Prompt files cannot include nested files, so it is a
full copy of 0.9-a with the block inserted, not an include of it. This file is not sent.
