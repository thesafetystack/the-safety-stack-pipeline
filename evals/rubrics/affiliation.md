# Labeling rubric: affiliation (who is affiliated with what, as the document prints it)

## The catalogue and your job

The catalogue reconstructs the AI safety stack as it is actually practised: which safety techniques exist, how
mature each one is, which developers use them, and where public knowledge runs out. Part of that is attribution:
which tracked organisations (AI labs, research institutes, safety organisations) stand behind which papers. That
map is built from what each paper itself prints in its byline, not from outside knowledge of where people work, so
an affiliation counts only when the document ties it to a specific author.

For each item you see its `item_id` and:
- `byline_text` — the front matter of the paper as converted text (title, author names, affiliation block, often
  part of the abstract). Superscript markers may survive as digits or symbols, or may have been lost in conversion.
- `xml_snippet` — when available, the publisher's structured author/affiliation markup; otherwise null.
- `authors_meta` — the authoritative author list from metadata, or an empty list.
- `roster_entries` — the tracked organisations, each with an id, a name, and where given aliases and domains.

## Who the authors are

- If `authors_meta` lists names: emit exactly one record per listed name, in the same order, with `name` copied
  exactly as it appears in `authors_meta`. Do not add, drop, merge, or reorder authors, even if the byline differs.
  Match each listed name to its byline appearance despite initials, middle names, accents, or name order.
- If it is empty: list the authors of this document from the byline, in byline order, with `name` as printed minus
  markers (digits, `*`, `†`, `‡`, superscript braces) and surrounding punctuation. Never include editors,
  acknowledged people, or cited authors. At most 60 authors. With no byline anywhere, return `"authors": []`.

## Which organisations belong to which author

An organisation belongs to an author only when the document ties it to that author. The layouts that tie:

1. **One shared affiliation, no markers**: a single affiliation unit printed once under the byline (sometimes as a
   heading), or a sentence such as "All authors are with X". It belongs to every author.
2. **Markers with a legend**: `Name^{1,3}` or `Name1,3` resolved against numbered or symbol entries, including
   legends that sit in a footnote line ("* Equal contribution 1 Lab A 2 University B"). Each legend entry the
   author's markers point to is one organisation.
3. **Inline after the name**: "Jane Roe Example Lab jane@example.org" — the text between name and email, or a
   name line directly followed by its own affiliation line, repeated per author.
4. **Per-author notes**: "Work done while at X", "Now at X", "Also with X" attached to that author's marker.
5. **Structured markup**: in `xml_snippet`, an author's cross-reference to an affiliation element. When the XML is
   present it decides the ties; use the byline text only for what the XML lacks.

Where a shared unit and per-author units both appear, the author gets both.

**No broadcasting.** When several affiliation units are printed but nothing ties them to particular authors (the
names in one block and the affiliations in another, markers lost in conversion, a two-column byline flattened so
that order is ambiguous), give every author `"orgs": []` and `"roster_ids": []`, and say so in the rationale. Do
not attach a unit to all authors, do not pair names and units by position or count, and do not use knowledge of
where a person works. The one exception is layout 1: exactly one unit, printed once, belongs to everyone.

Also `[]` for an author when the document prints no affiliation for them after you checked all five layouts, and
for every author when the visible text has no byline (a table of contents, a cover page).

Not affiliations: funders and grants, acknowledgements, "correspondence to", conference or journal names,
"equal contribution", and institutions listed for metadata authors that the document text does not print.

## How to copy an organisation (`orgs`)

Each element of `orgs` is one affiliation unit, copied as printed:
- A unit is one legend entry (everything between its marker and the next marker or the end of the legend), or,
  without markers, the contiguous affiliation text for that author up to an email, the next author, or a blank
  line. For layout 4, the unit is what follows "at" / "with" up to the end of the note.
- Copy it character-for-character, including department, city, and country if they are part of the unit, and any
  conversion artefacts (`&amp;`, odd hyphens). Drop only the marker itself and leading or trailing punctuation;
  join a unit broken across lines with one space.
- Never split one unit into pieces, never merge two units into one string, never normalise or expand a name.
- List an author's units in the order the document gives them; list each distinct unit once.

## Roster ids (`roster_ids`)

For each author, the ids of the roster entries named in any of that author's units, judged by the entry's name and
aliases (aliases may be regular expressions; apply them case-insensitively). One unit can name two tracked
organisations ("Lab A and University B; Lab C") and then yields both ids. When a more specific entry and a more
general one would match the same words ("Example Research" under "Example"), use only the more specific entry;
when the unit names both separately, use both. An unmatched unit (most universities) contributes no id; that is
correct, not missing. If the document prints an email for the author whose domain is listed under an entry's
`domains`, add that entry's id even when no unit names it (the email is not itself an `orgs` element). Each id
once per author, in order of first appearance.

## Hard cases

- Name list then "1 Lab A, 2 University B" with no markers on the names → markers lost: all `[]`.
- Five names, then the single line "Example Institute" → everyone gets "Example Institute" (layout 1).
- "Ann Lee1,2" with legend "1 Example Lab, London, UK 2 University B" → orgs `["Example Lab, London, UK",
  "University B"]`.
- A unit naming a fellowship or programme of a tracked organisation ("Example Lab Fellows Program") → copy it; the
  id follows the roster's name/alias match.
- `authors_meta` lists a person the byline does not print → keep the record, `[]`.

## Confidence, rationale, quote

- `confidence` — your probability (0-1) that the whole item's labels (every author's orgs and ids) are right.
  Lower it for flattened or partially marked bylines; a clean single-affiliation byline is typically 0.95.
- `rationale` — one or two sentences naming the layout you identified (shared, legend, footnote legend, inline,
  XML, untied) and anything you left empty and why.
- `quote` — a verbatim span from `byline_text` or `xml_snippet` showing the key tie (a legend line, the shared
  unit); empty string `""` when there is none.

## Output

Label every item in the batch, each on its own merits, without the web or any lookup. Emit one JSON object per
line, one line per item, these keys exactly and nothing else (no prose, no code fences):

```
{"item_id": "aff-0001", "authors": [{"name": "Ann Lee", "orgs": ["Example Lab, London, UK", "University B"], "roster_ids": ["example-lab"]}, {"name": "Bo Chen", "orgs": [], "roster_ids": []}], "confidence": 0.9, "rationale": "...", "quote": "..."}
```
