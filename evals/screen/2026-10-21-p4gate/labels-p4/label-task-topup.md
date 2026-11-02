# Task: independent labels for an evaluation of a proposed prompt clause (P4, technique provenance)

You are one of two independent labellers. Read `criteria.md` first (the two readings, the lineage criteria and how
lineage maps to verdicts), then `rubric-screen-v2.md` (the general relevance rules; `rubric-triage-v2.md` is there for
reference). Do not use the network and do not look outside this directory. Label every item on its own merits from its
input.

Item file (JSONL, one object per line):

- `items-screen-3.jsonl` (20 items): documents as the screen saw them (`title`, `text` = the start of the document, up
  to 8,000 characters; `year` = publication year). The screen answers S and R separately, each `relevant`,
  `borderline` or `irrelevant`.

Output: ONLY JSON lines (no prose, no code fences, no blank lines), one per item, all 20 items, in this exact shape:

{"id": "<id>", "task": "screen", "as_written": {"S": "relevant|borderline|irrelevant", "R": "relevant|borderline|irrelevant"}, "p4": {"S": "...", "R": "..."}, "lineage": true|false, "lineage_kind": "<kind or none>", "weak": true|false, "confidence": 0.0-1.0, "why": "<one short clause, under 160 characters>"}

Rules: if `lineage` is false, `p4` equals `as_written`. P4 never lowers a verdict and never changes R (`p4.R` =
`as_written.R`). `weak` is true only for the weak boundary cases of the criteria. `confidence` is your confidence in
the P4-scope verdicts.
