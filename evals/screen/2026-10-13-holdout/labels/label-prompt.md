You are labelling documents for an evaluation set. Read `rubric.md` in this directory completely first: it defines the labels (verdict, scope, sr_category, sr_agenda, buckets, confidence, rationale, quote) and the output format.

Items: `items.jsonl` (20 lines: item_id, title, channel, chars, text_file). Each item's document text is in its `text_file`. These texts can be long (up to 500k characters). Judge the WHOLE document, not only its opening: read at least the first ~12,000 characters and the last ~3,000, and search the rest (e.g. for "safety", "alignment", "risk", "interpretab", "eval", the conclusion and limitations sections) wherever the opening leaves the decision open. The "visible text" for the `quote` field is the whole text file; copy the quote character-for-character from it.

These documents are a general sample of the corpus (some clearly in scope, many clearly out); label each on its own merits.

Output: exactly 20 lines of JSON, one per item, in the rubric's output format (keys item_id, verdict, scope, sr_category, sr_agenda, buckets, confidence, rationale, quote), nothing else. Do not use the web.
