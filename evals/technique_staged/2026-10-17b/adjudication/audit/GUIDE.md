# Guide

You audit whether documents involve given AI-safety techniques. For each item in ITEMS.md, decide whether the named document involves the technique, in any role, at least once in the body of the document. Roles that count: mentioned, cited, discussed, used, evaluated, modified, introduced, analyzed, attested in deployment, suspected, considered or recommended, critiqued.

Rules:
- Occurrences only in a reference list or bibliography do not count.
- Generic machine-learning methods used only as tools (for example a classifier used to build a pipeline) do not count as the safety technique.
- Topic adjacency is not involvement: the document must refer to the technique itself (by name, alias, or an unmistakable description matching the definition), not merely to a nearby subject.
- Use the definition and aliases in ITEMS.md as the meaning of the technique. Judge each item independently.

Answer per item with:
- verdict: `yes` | `no` | `borderline`
- quote: for yes or borderline, one verbatim quote of at most 200 characters copied exactly from the document supporting the verdict; empty string for no
- reason: one sentence
