You are the adjudicator for a document-type evaluation set of a research-corpus pipeline about AI safety. Two independent labellers disagreed on some items; you decide the final label. Work only in this directory and do not use the web.

Read `definitions.md` first: it is the instruction both labellers received and defines the fields (doc_type, self_published, primary_lang) and the convention "when you cannot tell whether it was peer reviewed, preprint".

`disagreements.jsonl` has N_ITEMS lines. Each line has item_id, title, chars, text_file, `differing_fields`, and two anonymised labels, `labeler_1` and `labeler_2`, each with doc_type, self_published, primary_lang, confidence and evidence.

For each item, read the document text (its `text_file`; an abstract or landing page stands for the document it describes; for long texts the opening, the end and headers are usually enough), weigh both labels' evidence against the text and the definitions, and give the label the definitions call for. You may side with either labeller or give a third value. Judge each item on its own merits; neither labeller is known to be more reliable.

Output: exactly N_ITEMS lines of JSON, one per item, nothing else (no prose, no code fences), keys exactly:
{"item_id": "...", "doc_type": "...", "self_published": false, "primary_lang": "en", "confidence": 0.0, "sided_with": "labeler_1|labeler_2|neither", "note": "one or two sentences: what in the text decided"}
