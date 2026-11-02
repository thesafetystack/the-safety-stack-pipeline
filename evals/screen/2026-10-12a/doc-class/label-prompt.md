You are labelling the document type (genre) of documents for an evaluation set of a research-corpus pipeline about AI safety. Work only in this directory; do not use the web.

`items.jsonl` has 60 lines (item_id, title, chars, text_file). Each document's stored text is in its text_file (judge from the text; some texts are only an abstract or a landing page — an abstract or landing page stands for the document it describes; skim long texts: the opening, the end, and headers are usually enough).

For each item give:
- `doc_type` — exactly one of: paper (peer-reviewed: published in a journal or conference proceedings), preprint (a research paper not yet peer reviewed or of unknown status: arXiv, SSRN, repository copies; when you cannot tell whether it was peer reviewed, preprint), system_card (model or system card), model_card (standalone model card / release note), framework_policy (policy / framework document: RSP, safety framework, governance policy), lab_blog (lab or org blog / web article), eval_report (a standalone evaluation or benchmark report, not a research paper), product_doc (product / API / deployment documentation), regulatory (regulatory or legal filing), critique (critique, commentary, or negative-result note), news (news / press reporting), forum_post (forum or discussion-board post), tweet (microblog / X post), webpage (generic web page), dataset (dataset or benchmark release), other (none of the above). Choose the most specific genre that fits (a lab's model release note is model_card, not lab_blog).
- `self_published` — true when the publisher writes about its own work, models, products, safeguards or policies (a lab's blog post, system card or safety framework about itself); false for research papers and preprints, third-party reporting, commentary and forum posts about others' work.
- `primary_lang` — BCP-47 tag of the body's main language (en, de, zh, ...).
- `confidence` — your probability (0-1) that doc_type is right.
- `evidence` — a few words on what decided doc_type (e.g. "journal header: IEEE Access 2025", "arXiv id, no venue").

Output: exactly 60 lines, one JSON object per line with keys item_id, doc_type, self_published, primary_lang, confidence, evidence. Nothing else.
