Read GUIDE.md and ITEMS.md in the current directory. Then read each document under texts/ that ITEMS.md refers to, in full, and judge every item following GUIDE.md.

Output exactly one ```json block and nothing else after it, containing a list with one object per item, in item order:
[{"id": "Q001", "verdict": "yes|no|borderline", "quote": "<verbatim, <=200 chars, empty for no>", "reason": "<one sentence>"}]
Include all items; do not skip any. Quotes must be copied exactly from the document text.
