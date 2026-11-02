# technique_staged prompts

The plan pin `prompts.annotate.technique_staged` selects a **prompt set** by its `name` (default
`technique-staged-v1`); `main.PROMPT_SETS` maps a set to one file per call type, and the pin's `mode` decides which
call types run. The mechanism is `stack/annotate/prompt_files.py` (its module docstring is the reference): the line
that is, or ends in, `{{split:user}}` splits a file into the system part (the fixed prefix the client caches and
hashes as `template_digest`) and the user part (the per-call message); `{{include:<file>.md}}` inserts a file of this
directory.

| File | Call | Modes | What it is |
|---|---|---|---|
| `technique-staged-prepass-v1.md` | P, one per chunk | sweep, staged | menu-free reading: paragraph-numbered text in, compact occurrence-level notes out (strict schema) |
| `technique-staged-selection-v1.md` | S, one per document (batches of 150 mentions) | staged | maps each note, with its code-cut snippet, to 0-5 catalogue candidates or a `none` reason; the full v0.9 menu in the system part (strict schema) |
| `technique-staged-grading-v1.md` | G_sub, one per document | staged, lexical | the whole document plus the candidate sub-menu (full definitions, pointers); rubric, compact catalogue index and output contract (with `sweeps` and `rejected`) in the system part |
| `technique-staged-single-v1.md` | the single call | single | the current one-call design on v0.9: rubric, full menu, output contract with `sweeps` |
| `technique-staged-sweep-v1.md` | the sweep call | sweep | the single call plus a `# READING NOTES` block (P's notes as pointers) in the user part; **its system part is byte-identical to the single file's** (a test asserts it), so the two arms differ only in the notes |
| `rubric-v0.9-a.md` | - | included by grading, single, sweep | the role rubric v0.4.0 adapted to v0.9 (mode-neutral: menu or candidate wording lives in each output contract). Changelog: `rubric-v0.9-a.notes.md`. P and S do not include it. |

**Set `technique-staged-v2`** (post hoc: made from an error analysis of v1 staged runs on the whole eval set, so
its eval numbers are not held-out; select it with the pin `name`, in `stack eval technique_staged` with
`--prompt technique-staged-v2`). It changes S and G only and is meant for the staged and lexical modes:

| File | Call | Change against v1 |
|---|---|---|
| `technique-staged-selection-v2.md` | S | list EVERY entry the passage supports (procedure + object, mechanism + domain, evaluation + its methodology facets); candidates are not alternatives; `none` only when no entry fits; over-inclusion of supported entries over choosing one |
| `technique-staged-grading-v2.md` | G_sub | includes `rubric-v0.9-b.md`; candidates of one note are not alternatives (keep each whose definition the text meets); `wrong_sibling` only when a kept slug fits the same evidence strictly better and the rejected one does not fit at all, named in `rejected[].kept_instead` |
| `rubric-v0.9-b.md` | - | `rubric-v0.9-a.md` plus §4 "Precision rules", precedents 16-19 (instrument or stimulus is no row; a benchmark testing a classifier makes no evaluation row by itself; vague first-party mitigation statements; cited-work titles in the body). Changelog: `rubric-v0.9-b.notes.md`. A full copy, since includes do not nest |

P, single and sweep use the v1 files, so a v2 P request is byte-identical to the v1 one and the response cache serves
P answers to both sets (a test asserts it); single and sweep under v2 are the v1 calls (rubric 0.9-a) under another
pin name. `kept_instead` is in the answer only: the grading schema (shared with v1, unchanged) allows it, and the
payload's `prepass.rejected` keeps `slug` and `reason` alone.

**Set `technique-staged-v3`** (post hoc, from a review of single's misses on the same eval set, so not held-out;
`--prompt technique-staged-v3`). It changes the single call only; every other call is v2's file:

| File | Call | Change against single-v1 |
|---|---|---|
| `technique-staged-single-v2.md` | the single call | includes `rubric-v0.9-c.md`; a `# HOW TO FIND EVERYTHING` section after the menu (strength-1 rows are rows; one passage, several entries; plain-words descriptions; read to the end; a facet checklist); the output contract writes `sweeps.passages` first (every technique-bearing passage in document order with every slug it meets, or a `none` reason) and adds `not_graded` (each listed slug ends as a row or there, with a reason); the reminder "The menu is never evidence" is dropped (the rubric states it); the user part ends with a short reminder after the document instead of "Now emit ..." |
| `rubric-v0.9-c.md` | - | `rubric-v0.9-a.md` plus 0.9-b's precedents 16, 17 and 19 (not 18, which contradicts gold rows), numbered as in 0.9-b. Changelog: `rubric-v0.9-c.notes.md`. A full copy |

The answer's `passages` and `not_graded` are bookkeeping: `main.py` counts them (`stages.inventory_accounting`:
`inv_passages`, `inv_passage_slugs`, `inv_not_graded`, `inv_unaccounted`, `inv_not_graded_with_row`, only for an
inventory file, `main.INVENTORY_FILES`) and never turns them into rows; a `not_graded` slug that also has a row keeps
the row (repair `not_graded_with_row:<slug>`). The call sends `main.INVENTORY_SCHEMA` (single-v1's permissive schema plus
the two keys): luna refuses the strict schema and the client folds it into the prompt, so it must name what the
contract asks for (with single-v1's schema, two of three smoke answers left both keys out).

**Single-mode options on v3** (pin keys `window: true`, D614, and `glean: true`, D615; v3's plain single call is
unchanged, a test pins its digests). Both files' system parts are single-v2's byte for byte (a test asserts it), so
every call of a document reuses the cached prefix:

| File | Call | What it is |
|---|---|---|
| `technique-staged-window-v1.md` | one per part of a body (references excluded) over 60,000 characters | the user part: part k of n, why it is a part, a 3,000-character opening (context only, no quotes), the outline of all body headings, the part's section labels, the part's paragraphs (no `[pN]` markers), the reminder adapted to the part; the answers are merged by slug in `main.merge_answers` |
| `technique-staged-glean-v1.md` | after each single or window call | the user tail of a second turn (system + the first call's user part + assistant: its parsed JSON + this tail): only catalogue entries not yet graded, section by section to the last, with the facet checklist; schema `main.GLEAN_SCHEMA` (`techniques`, `extra_techniques`); merged by `main.glean_merge` (the first answer wins on a slug or extra name) |

**Single-mode sizes** (pin keys, any single-mode set; not set = the defaults, whose digests are unchanged):
`doc_cap` (the characters of the text the single call reads, and a windowed call segments; default 300,000, at
most 1,000,000: gpt-6-luna's context is 1,050,000 tokens, and its listed input price doubles above 272k prompt
tokens, so about 900,000 characters with the ~40k-token system part) and `window_min` (with `window: true`: the
body length above which the document is read in parts, default 60,000; `window_min: 300000` windows only what one
300k call could not read). Another value adds `{"doc_cap": N}` / `{"window_min": N}` to the versions digest.

**Sets `technique-staged-v4-<v>`** (experimental, gold-v3 prompt round; eval only, no plan pins them). Each is v2's
files with one new single file (inventory contract, `main.INVENTORY_SCHEMA`, rubric 0.9-c); none has window or
glean files:

| Set / file | Origin | What changes against single-v2 |
|---|---|---|
| `v4-a1` / `technique-staged-single-v4-a1.md` | Codex Astra, verbatim | a rewrite: same rules, ordered as inventory, sweeps, evidence and grading, reconciliation |
| `v4-a2` / `...-single-v4-a2.md` | Codex Astra, verbatim | section coverage: account for every section in `passages` (ranges allowed), compress bookkeeping not rows, an output-size target |
| `v4-a3` / `...-single-v4-a3.md` | Codex Astra, verbatim | one internal audit of every menu definition against the document before emitting, then a role audit |
| `v4-c1` / `...-single-v4-c1.md` | hand-written | the user part shows the document's OUTLINE (the segmenter's body headings, `{{outline}}`) before the text; `passages` follows it to the last heading (up to 150 lines); dense documents are long answers |
| `v4-c2` / `...-single-v4-c2.md` | hand-written | THE MENU BY AREA (every slug in the taxonomy file's thematic order, 19 areas, generated from `taxonomy-v0.9.1.yaml`; regenerate if slugs change) and an area pass before the rows |

Set `v4-a2` also has `technique-staged-window-v4-a2.md` and `technique-staged-glean-v4-a2.md` (system part a2's
single file's byte for byte; user tails window-v1's / glean-v1's with a2's section names), so it takes the pin keys
`window` / `glean`; the other v4 sets refuse them. A single file whose user part takes `{{outline}}` (c1) adds the
segmenter version to the pin's versions digest.

**Sets `technique-staged-v5-<v>`** (gold-v3 prompt round 2; eval only except `v5-a1.1`, the production set, D708): v2's files
with a new single file (inventory contract, rubric 0.9-c) and a gleaning file whose system part is that single
file's byte for byte (user tail: the variant's own second turn), so they take the pin key `glean` and refuse
`window`. Written against the dev-split error taxonomy of v3 + glean:

| Set / files | Origin | What changes |
|---|---|---|
| `v5-a1` / `technique-staged-{single,glean}-v5-a1.md` | Codex Astra, verbatim | catalogue identity before grading: own methods, proposed extras and confusable entries resolved by definition comparison; the gleaning tail re-checks filing |
| `v5-a1.1` / `technique-staged-{single,glean}-v5-a1.1.md` | v5-a1, one sentence changed (D708) | the shared system part's output-size sentence no longer states a generation budget (v5-a1: "the 16,000-token total generation budget", the default `max_tokens`; the production pin sets 64,000); otherwise v5-a1's files byte for byte. **The production set** (plan `batch-2026-10a` revision 7) |
| `v5-a2` / `technique-staged-{single,glean}-v5-a2.md` | Codex Astra, verbatim | secondary evidence outside the main threads (secondary operations, measured dimensions, unnamed practices, relays); the gleaning tail reads the document backward by section |
| `v5-f1` / `technique-staged-{single,glean}-v5-f1.md` | Fable, verbatim | filing: a HOW TO FILE section (the entry's exclusion clause and its neighbour decide; the paper's own method goes on the menu, `extra_techniques` only when no definition covers it; siblings are not alternatives); the gleaning tail re-files extras and neighbours before the gap hunt |
| `v5-f2` / `technique-staged-{single,glean}-v5-f2.md` | Fable, verbatim | finding: secondary techniques, late sections, unnamed practices and relays (see its rationale); its own gleaning tail |

**Sets `technique-staged-v6-<v>`** (2026-10-27 round on the adjudicated gold-v3 reference; eval only, no plan pins
them). Tuned on the gold-v3 dev split only; measured on its validation split.

| Set / files | What changes |
|---|---|
| `v6-s1`: v5-a1.1's single and gleaning files + `technique-staged-sector-v6-s1.md` + `technique-sectors-v1.yaml` | the **sector turns** (pin key `sectors: true`, single mode, not with `window`): after the first answer and its gleaning turn, one more turn per sector of the sector file (6 sectors over the 234 techniques, from the 19 areas of `technique-staged-single-v4-c2.md`), each listing that sector's entries (`- slug: name`) and adding only new rows. The sector file's system part is v5-a1.1's byte for byte, and every sector turn continues the same conversation, so the document and both earlier answers are one cached prefix. Without `sectors`, v6-s1 sends exactly v5-a1.1's calls |
| `v6-p1` / `technique-staged-{single,glean}-v6-p1.md` | v5-a1.1 with two edits of the shared system part: a FIND THE EVIDENCE bullet (evaluation and attack methods applied as tools are their entries: optimisation or attacker-model attacks are automated red-teaming also when they only test a defence; named benchmarks that are run are their evaluation entries) and the interpretability-auditing check spelled out (an audit of a specified safety property) |
| `v6-s2`: v6-p1's files + `technique-staged-sector-v6-s2.md` | v6-p1 with the sector turns (sector file: v6-p1's system part, v6-s1's user tail) |

`technique-sectors-v1.yaml` (`version`, `sectors`: `id`, `title`, `scope`, `areas`, `slugs`) is read by
`main.sectors_of`, which refuses a technique in no sector or in two, a group node or an unknown slug; its sha256 is in
the pin's template digests under `sectors`. Sector user part placeholders: `{{sector_index}}`, `{{sector_count}}`,
`{{sector_title}}`, `{{sector_scope}}`, `{{sector_entries}}`.

Placeholders `main.py` fills (every placeholder of a part must be filled, nothing else may be):

- prepass, user part: `{{opening}}` (empty for the first chunk, else a `# DOCUMENT OPENING (context only)` block with
  the first 1,500 characters of the body), `{{chunk_index}}` / `{{chunk_total}}` (a half after a split reads
  `2 (first half)`), `{{section_labels}}` (the chunk's section labels, `; `-joined), `{{chunk}}` (`[pN] <paragraph>`
  per paragraph);
- selection: `{{taxonomy_menu}}` (system part; `taxonomy.menu_text`), user part `{{opening}}`, `{{mention_count}}`,
  `{{batch_note}}` (empty, or `; batch k of n, leave doc_level empty` for a later batch), `{{mentions}}` (one line per
  mention: `m12 | "surface" | gloss: ... | stance: used | implicit: no | section: 3 Method | [p17] "snippet"`);
- grading: `{{catalogue_index}}` (system part; `taxonomy.compact_index`), user part `{{document}}`, `{{truncation}}`,
  `{{candidate_count}}`, `{{candidates}}` (`taxonomy.submenu_text`; `(none)` when the sub-menu is empty: the call
  still runs);
- single / sweep: `{{taxonomy_menu}}` (system part), user part `{{document}}`, `{{truncation}}`, optionally
  `{{outline}}` (single), and in sweep
  `{{reading_notes}}` (one line per note: `[<section>, pN] "surface" - gloss (stance hint: x): "snippet"`; `(no
  reading notes)` when P found nothing).

`{{document}}` is the text up to 300,000 characters; `{{truncation}}` is empty or the line `[DOCUMENT TRUNCATED AT
CHARACTER CAP]` with a blank line before it, as in `technique_roles`.

**A new prompt version** is new files under a new set name (`technique-staged-v2`, with
`technique-staged-<call>-v2.md` for the calls that change), never an edit of a file a plan pins: every file a mode
uses (with its includes) is in the pin's template digests, so an edit re-asks every document.

**Made from** the v2 design drafts after two critique rounds; the round-2 changes applied here: pre-pass recall
wording, gloss and occurrence rules, examples E12 and E16, one mention per technique in related work (prepass);
`sweeps` and `rejected` in the grading contract; `no_techniques_found` only when both row lists are empty (grading,
single, sweep, rubric); `(provisional)` explained in every menu header; rubric changes listed in
`rubric-v0.9-a.notes.md`.
