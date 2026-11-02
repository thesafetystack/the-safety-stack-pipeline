# Labeller instructions: AI models and their roles (gold v3, blind)

You are an expert annotator with a background in annotation methodology and AI research, building an independent
GOLD reference for an automatic model-roles annotator.

## Your session directory (the current directory; work only here)
- `GUIDE.md` - the labelling guide (read first, follow exactly).
- `rubric-v0.6.0.md` - the production rubric (§1-§5 and §7; ignore its §6 output schema).
- `vocab-index.tsv` - the model catalogue (node ids).
- `schema.json` - the JSON Schema of your output file.
- `check_labels.py` - a checker: `python3 check_labels.py model_roles out/<item_id>.json` reports schema problems,
  menu rows missing or extra, unknown node ids and quotes not found verbatim in the text.
- `docs/<item_id>/text.txt`, `docs/<item_id>/meta.md`, `docs/<item_id>/model-menu.md`.
- `out/` - where you write your labels.

Do not read anything outside this directory, and do not look for other labellers' files, model outputs or earlier
gold: the labels must be independent. Use only shell tools to read files (e.g. `wc`, `sed -n`, `grep -n -i`); no
network.

## Procedure, per document (the items are listed in your session prompt)
1. Read `meta.md` and `model-menu.md`, then skim the whole `text.txt` once (structure, abstract, experiments,
   appendices); fix `doc_kind`.
2. Per menu entry: locate every occurrence (the matched text as starting points, then `grep -n -i` for other surface
   forms), read the passages, decide presence / identity / refinement, collect verbatim quotes, then grade the 10 roles.
3. Sweep for the document's own training work ("we train", "we fine-tune", "our model", "initialized from") and for
   models the menu lacks (abstract, tables, experimental sections, appendices): variant rows and `extra_models`.
4. Mentions: list every named model (references included) with its most specific node from `vocab-index.tsv`, or under
   `mentions_out_of_vocab`.
5. Quotes: copy each quote from `text.txt` exactly (<= 300 characters) and confirm it with `grep -F` (or the checker).
6. Write `out/<item_id>.json` per the guide's Output section and `schema.json` (UTF-8, valid JSON, `labeller` = the
   label given in your session prompt). Run `python3 check_labels.py model_roles out/<item_id>.json` and fix every
   problem it reports before moving on.

Do not abbreviate, skip documents or summarise instead of labelling. When all documents of your session prompt have
a checked file, finish with one line per document: `<item_id> <menu rows> <rows with a role >= 2> <checker status>`.
