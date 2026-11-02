# Task: independent labels for an evaluation of a proposed prompt clause (P4, technique provenance)

You are one of two independent labellers. Read `criteria.md` first (the two readings, the lineage criteria and how
lineage maps to verdicts), then `rubric-triage-v2.md` and `rubric-screen-v2.md` (the general relevance rules). Do not use
the network and do not look outside this directory. Label every item on its own merits from its input.

Item files (JSONL, one object per line):

1. `items-triage.jsonl` (96 items): catalogue records as the triage stage saw them (`record`; `basis` = `title` when
   the record had no abstract or text and only a title plus bibliographic subjects; `year` = publication year). The
   triage decision is whether to fetch the full text: `relevant` and `borderline` both mean fetch, `not_relevant` means
   skip.
2. `items-screen-1.jsonl` (32) and `items-screen-2.jsonl` (31): documents as the screen saw them (`title`, `text` = the
   start of the document, up to 8,000 characters). The screen answers S and R separately, each `relevant`,
   `borderline` or `irrelevant`.

Output: ONLY JSON lines (no prose, no code fences, no blank lines), one per item, all 159 items, in this exact shape.

Triage items:
{"id": "<id>", "task": "triage", "as_written": {"verdict": "relevant|borderline|not_relevant", "scope": "core|sr_only|out"}, "p4": {"verdict": "...", "scope": "..."}, "lineage": true|false, "lineage_kind": "<one kind from the criteria table, or none>", "weak": true|false, "confidence": 0.0-1.0, "why": "<one short clause, under 160 characters>"}

Screen items:
{"id": "<id>", "task": "screen", "as_written": {"S": "relevant|borderline|irrelevant", "R": "relevant|borderline|irrelevant"}, "p4": {"S": "...", "R": "..."}, "lineage": true|false, "lineage_kind": "<kind or none>", "weak": true|false, "confidence": 0.0-1.0, "why": "<one short clause, under 160 characters>"}

Rules: `scope` is `out` exactly when the verdict is `not_relevant`; `core` when the record is in scope for the catalogue
(S) by either route, `sr_only` when only for the Review. If `lineage` is false, `p4` equals `as_written`. P4 never lowers
a verdict and never changes R (`p4.R` = `as_written.R`). `weak` is true only for the weak boundary cases of the
criteria. `confidence` is your confidence in the P4-scope verdict(s).
