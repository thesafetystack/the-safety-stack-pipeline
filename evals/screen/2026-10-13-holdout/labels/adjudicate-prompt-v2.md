You are the adjudicator for an evaluation set. Two independent labellers disagreed on some items; you decide the final label. Work only in this directory and do not use the web.

Read `rubric.md` completely first: it defines the labels (verdict, scope, sr_category, sr_agenda, buckets, confidence, rationale, quote) and how to decide them.

`disagreements.jsonl` has N_ITEMS lines. Each line has item_id, title, channel, text_file, and two anonymised labels, `labeler_1` and `labeler_2`, each with its verdict, scope, sr_category, sr_agenda, buckets, confidence, rationale and quote. The labellers disagree on the verdict and/or on the scope or SR category, which decide the two per-reader questions (Stack relevance = the verdict if scope is `core`, else irrelevant; Shallow Review relevance = the verdict if scope is `sr_only`, or `core` with an sr_category other than `none`, else irrelevant).

For each item, read the document itself (its `text_file`; texts can be long: read at least the first ~12,000 characters and the last ~3,000, and search the rest for the passages either rationale relies on and for "safety", "alignment", "risk", "interpretab", "eval", the conclusion and limitations), weigh both rationales against the text and the rubric, and give the label the rubric calls for. You may side with either labeller or give a third value. Judge each item on its own merits; neither labeller is known to be more reliable.

Output: exactly N_ITEMS lines of JSON, one per item, nothing else (no prose, no code fences), keys exactly:
{"item_id": "...", "verdict": "...", "scope": "...", "sr_category": "...", "sr_agenda": null, "buckets": ["..."], "confidence": 0.0, "rationale": "...", "quote": "...", "sided_with": "labeler_1|labeler_2|neither", "adjudication_note": "one or two sentences: what in the text decided between the two"}
The `quote` is copied character-for-character from the text file.
