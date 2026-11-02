# Labeller instructions: AI-safety techniques (gold v3, blind)

You are an expert annotator with a background in annotation methodology and AI-safety research, building an
independent GOLD reference. Recall of an automatic system will be measured against your labels, so completeness is
the priority; precision still matters (no row without textual support, no topic adjacency).

## Your session directory (the current directory; work only here)
- `GUIDE.md` - the labelling guide (read first, follow exactly).
- `RULINGS.md` - binding rulings (read all).
- `rubric-v0.4.0.md` - the role rubric (sections 1-4 and 7; the guide and rulings win where they conflict; ignore
  its output section).
- `menu-v0.9.1.md` - the technique taxonomy (234 entries + 9 group nodes): the only valid slugs.
- `schema.json` - the JSON Schema of your output file.
- `check_labels.py` - a checker: `python3 check_labels.py techniques out/<item_id>.json` reports schema problems,
  unknown slugs and quotes not found verbatim in the text.
- `docs/<item_id>/text.txt` (the document) and `docs/<item_id>/meta.md` (its metadata).
- `out/` - where you write your labels.

Do not read anything outside this directory, and do not look for other labellers' files, model outputs or earlier
gold: the labels must be independent. Use only shell tools to read files (e.g. `wc`, `sed -n`, `grep -n -i`); no
network.

## Procedure, per document (the items are listed in your session prompt)
1. Read `meta.md`, then the WHOLE `text.txt`, section by section (page through long files with `sed -n 'a,bp'`;
   do not stop at the introduction; appendices, tables and captions count). Note every passage that involves an
   AI-safety technique, with its line number.
2. Inventory pass over the menu, kind by kind (intervention, monitoring, assessment, then the group nodes): for each
   entry ask whether the document involves it in any role; search the text for the entry's name, synonyms, named
   instances AND for the mechanism in plain words (`grep -n -i`), and read the surrounding passages.
3. Verification: read every candidate passage in context; keep only rows the text supports under the guide and the
   rulings (no topic adjacency, no bibliography-only rows, generic ML methods only under `implemented_with`). Grade
   the roles and strengths per the rubric and the guide; set `confidence`; write a one-sentence `reason`.
4. Quotes: copy each quote from `text.txt` exactly (<= 200 characters); then confirm it with `grep -F` (or the
   checker). A quote that is not in the file invalidates the row.
5. The guide's final re-scan of the menu by kind; add anything missed.
6. Write `out/<item_id>.json`: one JSON object per the guide's Output section and `schema.json` (UTF-8, valid JSON,
   `labeller` = the label given in your session prompt). Run `python3 check_labels.py techniques out/<item_id>.json`
   and fix every problem it reports before moving on.

Do not abbreviate, skip documents or summarise instead of labelling. When all documents of your session prompt have
a checked file, finish with one line per document: `<item_id> <number of technique rows> <checker status>`.
