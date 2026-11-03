# The Safety Stack: file format

One parquet file, `data/corpus.parquet`, one row per document, is the dataset (the hub's only configuration); the
other files resolve the ids its rows carry or describe them. The sections up to "Rules a reader should know" are
written for people; everything after "Generated reference" is generated from the release itself
(and from the release mapping, which describes every field and how it is derived) and is meant to be searched, not
read.

{{variant_note}}

## Files

{{files_table}}

**Which file to open.** `data/corpus.parquet` for everything: labels, roles, quotes, provenance. Flat tables (one row
per document with label lists, or one row per document and kind) are one query away; see "Recipes".

## Format versions

This release has format version `{{format_version}}`: `release.json` `format_version`, the parquet file's key-value
metadata `format_version` (beside `release_id`) and the card's front matter carry it. Many dated releases share one
format; the version changes only when the files change shape. The rules, from the reader's side (every version's
schemas, the changelog and the full table: `formats/` in the
[taxonomies repository](https://github.com/thesafetystack/the-safety-stack/tree/main/formats)):

- **Within major version {{format_major}}** every field and file of this format stays, with its JSON type and its
  Arrow type; a field that is not nullable stays so; a closed list keeps exactly its values.
- **A minor version may add** fields and files, values of an open list, a taxonomy key in `release.json`, and may
  make a nullable field non-nullable. So select fields by name, and tolerate fields, files and open-list values you
  do not know.
- **A patch version** changes prose only (descriptions, these documents). Anything else (a field removed, renamed or
  retyped, a field made nullable, a closed list changed, a change of meaning) is a new major version.

Each file's JSON Schema (2020-12) sits beside it: `release.schema.json`, `data/corpus.schema.json` (one row; every
node's exact Arrow type as `x-arrow`), `config/configs.schema.json` and `config/overrides.schema.json` (one line).
A closed list is an `enum` there. An open list has `x-open: true` and its known values in `x-values`; the open lists
are marked in [VOCABULARY.md](VOCABULARY.md). The providers' `doc_type` and the channel names are plain strings
whose values VOCABULARY.md describes; new ones can appear in any release.

To read the version, and to read a release pinned by its tag (`release-<release_id>`) so code keeps reading the
format it was written for:

```python
import json, pyarrow.parquet as pq
json.load(open("release.json"))["format_version"]                    # a downloaded release
pq.read_schema("data/corpus.parquet").metadata[b"format_version"]    # b"{{format_version}}"

REL = "hf://datasets/{{hf_repo}}@release-{{release_id}}/data/corpus.parquet"   # this release, pinned
import pandas as pd
df = pd.read_parquet(REL)                       # pandas reads hf:// through huggingface_hub
import polars as pl
d = pl.scan_parquet(REL)
import duckdb
duckdb.sql(f"SELECT decode(key) AS key, decode(value) AS value FROM parquet_kv_metadata('{REL}') "
           "WHERE decode(key) IN ('format_version', 'release_id')").show()
duckdb.sql(f"SELECT count(*) FROM '{REL}'").show()
```

**Checking a download.** `uv run tools/release-validator.py <release directory> --rows all` (the checker ships in
`tools/`) checks the format version, every file against `release.json` (sha256, nothing missing or extra) and
against its schema (every row as JSON), the ids (`doc_id`, `config_id`, the overrides) and the taxonomy ids against
the pinned files. Exit code 0 = valid, 1 = invalid, 3 = a format version the checker does not know.

## The row in one screen

- **Bibliographic columns**: `doc_id`, `title`, `abstract` (`release.abstract` says whether it is shipped, withheld
  or never existed), `authors[]`, `published_date` with `published_precision`
  (`day`, `year_inferred` for the providers' 1 January placeholders, `unknown`), `doc_type`, `is_withdrawn`,
  `canonical_url`, `field_sources[]` (which source supplied each field).
- **`identity`**: the source records (`members[]`) merged into the document and every external id (`keys[]`: arXiv,
  DOI, OpenAlex, Semantic Scholar, forum post ids, URLs). Look a paper up by its arXiv id or DOI here.
- **`content`**: what was annotated, as identifiers: the fetched file (`content.source`: url, sha256, bytes, media
  type, time) and our Markdown conversion of it (`content.text`: sha256, bytes, converter, body). `content.text.body`
  holds the text only where `release.text = inline`; it is NULL where texts are not distributed.
- **`release`**: what this release did to the row: abstract policy, text policy, `quote_chars` (characters of quoted
  document text in the row) and `cuts[]` (every quote or surface shortened to the cap).
- **`annotations`**: one column per kind, each `{metadata, payload}`; NULL when the kind was not run for the
  document. `metadata` is the same struct in every kind: `kind`, `version`, `status`, `error_class`, `config_id`
  (resolved by `config/configs.json`), `decided_at`, `origin`, `annotator`, `provenance`, `override_ids`,
  `override_fields`. Kinds: `screen` (the call-level relevance verdict), `stack_relevance` and
  `shallow_review_relevance` (its two questions), `text_usability`, `doc_class` (all five from one call),
  `affiliation` (each author's affiliations as written, with the organisations each names: `org_ids`, copied from
  `affiliation_orgs`), `affiliation_orgs` (the organisations, resolved without a model from the affiliations: its
  metadata is the provenance of `org_ids`), `safety_techniques` (techniques of the taxonomy with 12 role strengths 0-4), `model_roles` (AI
  models of the catalogue with 10 role strengths 0-4) and `model_name_matches` (the deterministic name matcher's raw
  hits, a record of the model_roles call) and `translation` (English **machine translations** of a non-English
  title and abstract, beside the originals; `abstract_en` follows the abstract policy). `annotations.other[]` is
  reserved for kinds without a typed column (empty).

The annotations are made by language models (with a deterministic name matcher for models and, for some
affiliations, rule-based parsing of publisher XML; a deterministic matcher resolves the affiliations to
organisations) and are not systematically reviewed by people. {{overrides_summary}} `metadata.provenance` says
which annotations an override touched. For {{standin_docs}} documents of this release whose main-model request was
refused, the technique and/or model annotation is a stand-in by a Claude model (`annotator`, `provenance = override`).
Annotations record what a document says, not whether it is true. The card (`README.md`) reports how well they agree
with three language-model judges.

## Unverified quotes (`quote_unverified`)

Every technique row (`safety_techniques.payload.techniques[]`, and `extra_techniques[]`) carries at least one quote the
model copied from the document, and every quote carries `anchored`: whether the pipeline found it in the converted
text (exactly, after whitespace and punctuation normalisation, or by a fuzzy match that keeps every digit; an elided
quote segment by segment; failing those, once more with inline citations, LaTeX markup, table cells and case set
aside). A row none of whose quotes was found carries the row flag `quote_unverified`. Its quotes are paraphrases, a
model's inventions, or passages our conversion garbled (formulas and tables most often); the row is kept, because
dropping it would hide the failure. {{quote_unverified_counts}}

How to weight such a row: for a claim about what a document or a lab does (own or attested use, a role at strength 3
or 4) leave it out or check it against the text (`content.text.body` in the internal variant, the source otherwise);
for counts and trends keep it at a lower weight, or report the numbers with and without it. A found quote shows that
the words are in the document, not that they support the label, and the flag says nothing about the role strengths.

## Document types: `doc_type`, `doc_class`, `doc_kind`

Three fields type a document. They are made differently, answer different questions and legitimately disagree.

| Field | Made by | Says | Use it for |
|---|---|---|---|
| `doc_type` | the provider or the collector (OpenAlex's type, the channel: forum, lab blog, a lab's card page) | the form and the venue | filtering by venue or channel (preprints, journal papers, forum posts, cards); NULL where the collector gave none: {{doc_type_null}} documents, hand-added and newsletter-linked URLs and papers |
| `annotations.doc_class.payload.doc_type` | the screen call: the channel's type where the channel decides the form (forum posts, lab blogs), the provider's paper or preprint where it says, else the model's | the same vocabulary and a few more values (`critique`, `eval_report`, `webpage`, `regulatory`) | a type for every screened document, `doc_type` NULL included |
| `doc_kind` of `safety_techniques` and of `model_roles` | the technique and the model annotator (the model) | the genre as the annotator read it: paper, blogpost, commentary, technical_report, system_card, model_card, eval_report, policy, news, other | weighting technique evidence by genre (a card's statements about its own model, a paper's experiments, commentary on another lab) |

An OpenAI system card posted on arXiv is a `preprint` by `doc_type` and a `system_card` by `doc_kind`; a forum post
summarising a paper is a `forum_post` and a `paper`. `system_card` and `model_card` follow the lab's own name for the
document (xAI, Google and Meta publish model cards, Anthropic system cards, OpenAI both): count them as one kind,
cards, when comparing labs.

## Taxonomy files

{{taxonomy_layouts}}

- `{{techniques_file}}`: `entries` are the techniques (`node: technique`; `{id, name, kind, status, origin,
  definition, parent, aliases, ml_methods, risk_note, ...}`, `kind`: intervention, monitoring or assessment,
  `status`: final or provisional, `aliases` a list of `{text, kind}`) and the group nodes (`node: group`; `{id, name,
  definition, children}`), umbrella slugs (such as `red-teaming`) that annotations also use for generic mentions.
  An entry's `id` is the `slug` of `safety_techniques`.
- `{{models_file}}`: `entries` are the model nodes `{id, kind, name, gloss, parent, maker, lab, mentions, ...}`
  (`kind`: model, family or group); `id` is the `node_id` of `model_roles`. `maker` (a list for a joint release) and
  `lab` are `{id, name}`: `id` an organisation of the organisation vocabulary (`meta.references.orgs` is its version),
  `name` its display name.
- `{{orgs_file}}`: the organisation vocabulary (version 0.x: ids may still change between versions), in the
  catalogue envelope (`version`; `meta` with `schema`, `status`, `updated`, `sources`; `entries`). `entries` is a
  list of `{id, name, parent, aliases, note, external_ids, type, country, other_ids, tags, ...}`: `id` is what
  `affiliation.payload.items[].affiliations[].org_ids` holds; `type` is one of `meta.types` (university, company,
  institute, government, nonprofit, hospital, other); `country` an ISO 3166-1 alpha-2 code; `parent` the id of the
  organisation a sub-unit belongs to (`deepmind`, `google-research` and `google-brain` under `google`, institutes
  under the Chinese Academy of Sciences); `external_ids.ror` the ROR ids it consolidates; `tags` among
  `frontier-lab`, `ai-safety-org`, `model-maker`. The other fields (`aliases`, `cased`, `segment_aliases`,
  `local_names`, `patterns`, `domains`) are what the matcher looked for, and `meta` holds its word lists.

A lookup table of slugs and names (run from the release directory), to join on `techniques[].slug` (Python with PyYAML):

```python
import yaml, polars as pl
tx = yaml.safe_load(open("{{techniques_file}}"))["entries"]
slugs = pl.DataFrame([{"slug": e["id"], "name": e["name"], "kind": e.get("kind", "group")} for e in tx])
```

The same for organisations, with each one's top-level organisation (`root`), to join on `org_ids`:

```python
orgs = yaml.safe_load(open("{{orgs_file}}"))["entries"]
parent = {o["id"]: o.get("parent") for o in orgs}
def root(i):
    while parent.get(i):
        i = parent[i]
    return i
org_table = pl.DataFrame([{"org_id": o["id"], "name": o["name"], "type": o["type"], "country": o.get("country"),
                           "parent": o.get("parent"), "root": root(o["id"])} for o in orgs])
```

## Ids and where they resolve

| In the row | Resolves via | To |
|---|---|---|
| `annotations.<kind>.metadata.config_id` | `config/configs.json` | the producing call: stage, prompt name and the digests of its files, model and parameters, taxonomy pin, the pipeline's config digest |
| `...metadata.override_ids`, `identity.override_ids`, `identity.members[].override_id` | `config/overrides.jsonl` | kind, run, author, method, note, value, created_at of each override |
| `annotations.<kind>.metadata.kind`, `version` | `data/corpus.schema.json`, the generated reference below | the kind's record and payload schema, its internal name, capped paths |
| `safety_techniques.payload.techniques[].slug`, `model_roles.payload.models[].node_id` | `taxonomies/` | the techniques taxonomy and the models catalogue |
| `affiliation.payload.items[].affiliations[].org_ids` and `org_candidates`, `affiliation_orgs.payload.org_ids` | `{{orgs_file}}` (`entries[].id`; `affiliation_orgs.payload.vocab_version` names the version) | the organisation: name, type, country, parent |
| a hand-added document (`identity.members[].channel` = `manual_url`, `manual_paper`) | `config/overrides.jsonl`, the `manual_source` row whose `key` is the member's `<key_kind>=<key_value>` | who added it and how the list was prepared |
| a document linked from a newsletter issue or a hand-added page (channel `links_url`, `links_paper`, `links_forum`) | not resolvable in this release; a hand-added page is one of the `manual_source` rows whose `value.flags` has `collect_links`, but the member does not say which | such members carry no bibliographic record of their own, so `title` and `field_sources[]` can be empty |
| `content.text.sha256` | `content.text.body` (where `release.text = inline`) | the converted text the annotations judged |
| every value word | `VOCABULARY.md` | one sentence each |

## Recipes

The recipes read a local copy and are run from the release directory (the card's download command puts it in
`stack/`, so `cd stack` first); to query the hub directly, put the card's `hf://` URL in place of
`data/corpus.parquet` (a private repository needs a token; see the card).

DuckDB (the file is the view `d`; the macros below need it):

```sql
CREATE VIEW d AS SELECT * FROM 'data/corpus.parquet';
-- documents where technique X is used with strength >= 3
SELECT doc_id, title FROM d, unnest(annotations.safety_techniques.payload.techniques) AS u(t)
WHERE t.slug = 'rlhf' AND t.roles.used >= 3;
-- models with any role in one document
SELECT m.node_id, m.roles FROM d, unnest(annotations.model_roles.payload.models) AS u(m)
WHERE doc_id = 'ssepg4p9vmv5w' AND m.roles.mentioned > 0;
-- coverage of one kind
SELECT annotations.model_roles.metadata.status, count(*) FROM d GROUP BY ALL;
-- look a paper up by arXiv id (without version suffix) or DOI (stored lowercase)
SELECT DISTINCT doc_id, title FROM d, unnest(identity.keys) AS u(k) WHERE k.kind = 'arxiv' AND k.value = '2310.10659';
SELECT DISTINCT doc_id, title FROM d, unnest(identity.keys) AS u(k)
WHERE k.kind = 'doi' AND k.value = lower('10.48550/arXiv.2310.10659');
-- for a dashboard: documents per technique and publication year (own use, strength >= 2)
SELECT t.slug, year(published_date) AS year, count(*) AS documents
FROM d, unnest(annotations.safety_techniques.payload.techniques) AS u(t)
WHERE t.roles.used >= 2 GROUP BY ALL ORDER BY ALL;   -- year is NULL for undated documents; see published_precision
-- one row per (document, catalogue model) with some role, the ten role strengths as columns
SELECT doc_id, m.node_id, unnest(m.roles) FROM d, unnest(annotations.model_roles.payload.models) AS u(m)
WHERE m.roles.mentioned >= 1;
-- the producing configuration (prompt, model) behind each technique annotation
SELECT c.config_id, c.prompt_name, c.model, count(*) AS documents
FROM d JOIN read_json('config/configs.json') AS c ON c.config_id = d.annotations.safety_techniques.metadata.config_id
GROUP BY ALL ORDER BY documents DESC;
-- documents per organisation named in an affiliation (org_ids; resolve them with the organisation table above)
SELECT o AS org_id, count(DISTINCT doc_id) AS documents
FROM d, unnest(annotations.affiliation.payload.items) AS u(it), unnest(it.affiliations) AS v(a), unnest(a.org_ids) AS w(o)
GROUP BY ALL ORDER BY documents DESC;
-- a flat table: one row per document with its technique and model lists
SELECT doc_id, title, published_date,
       list_transform(list_filter(annotations.safety_techniques.payload.techniques, lambda t: t.roles.used >= 2),
                      lambda t: t.slug) AS techniques_used,
       list_transform(list_filter(annotations.model_roles.payload.models, lambda m: m.roles.mentioned >= 1),
                      lambda m: m.node_id) AS models
FROM d;
```

DuckDB macros: one row per document and technique (`technique_rows()`, the long table most analyses start from) or
per document and model (`model_rows()`), and a kind's status, verdict or record by name. Paste them into a session
after the `CREATE VIEW` above (the pipeline's own macros of these names read its internal store, not the release):

```sql
{{release_macros}}
```

```sql
{{release_macro_examples}}
```

Expand techniques and models in separate queries: unnesting both lists in one query pairs every technique with every
model of the document. The recipes count catalogue rows (`techniques[]`, `models[]`) only; `extra_techniques[]` and
`extra_models[]` are off-catalogue names (an extra model's `node_id`, where set, mostly repeats a `models[]` row of the
same document), and `variant_models[]` are named derivatives of a catalogue model (`base_node_id`).

Every kind's metadata as one long table (one row per document and kind; a kind not run for a document has no row),
for coverage and override audits across kinds:

```sql
{{records_recipe}}
-- statuses of the records that exist (a kind not run has no record; the generated Coverage table counts not_run)
SELECT kind, status, count(*) FROM records GROUP BY ALL ORDER BY ALL;
-- records changed (mixed) or supplied (override) by overrides
SELECT kind, provenance, count(*) FROM records WHERE provenance <> 'pipeline' GROUP BY ALL ORDER BY ALL;
```

polars (the same RLHF query):

```python
import polars as pl
d = pl.scan_parquet("data/corpus.parquet")   # lazy: only the columns a query needs are read
techniques = (d.select("doc_id", t=pl.col("annotations").struct.field("safety_techniques").struct.field("payload")
                                  .struct.field("techniques"))
              .explode("t").unnest("t"))   # one row per (document, technique)
used = techniques.filter((pl.col("slug") == "rlhf") & (pl.col("roles").struct.field("used") >= 3)).collect()
```

pandas (nested columns arrive as Python dicts and lists):

```python
import pandas as pd
d = pd.read_parquet("data/corpus.parquet", columns=["doc_id", "annotations"])   # memory-heavy: every annotation as Python objects
st = d["annotations"].map(lambda a: a["safety_techniques"])
rows = [(doc, t["slug"], t["roles"]["used"]) for doc, a in zip(d["doc_id"], st)
        if a is not None and a["payload"] is not None and a["payload"]["techniques"] is not None
        for t in a["payload"]["techniques"]]
techniques = pd.DataFrame(rows, columns=["doc_id", "slug", "used"])
```

Hugging Face `datasets`, from a local copy (the card shows the same from the hub, where the corpus is the only
configuration):

```python
from datasets import load_dataset
corpus = load_dataset("parquet", data_files="data/corpus.parquet", split="train")
```

## Rules a reader should know

- **Not run vs failed vs empty.** `annotations.<kind>` NULL = the kind was not run for the document (affiliation runs
  only where `doc_type` is paper, preprint, conference or journal and `content.role` is full_text, html or xml). `status = failed_permanent` = it was asked and no answer exists (the
  payload's lists are NULL; `error_class` says why). For the five screen kinds and affiliation, `status = ok` means an answer exists, a negative verdict
  included. For `safety_techniques` and `model_roles`, `empty` = no positive label (the lists are empty, except that
  model_roles may keep zero-role candidates), and `ok` = some positive row in some list, possibly only off-catalogue
  ones (`extra_techniques[]`, `variant_models[]`, `extra_models[]`): filter on the list you need, not on status. A
  role strength of 0 on a row is an explicit "not in this role"; a technique or model with no row was not found.
- **One call, several kinds.** The five screen kinds share `config_id` and `decided_at`; so do `model_roles` and
  `model_name_matches` (whose `annotator` is `matcher`), except where an override replaced the model_roles answer:
  the matcher record then keeps the original call's metadata (and, where that call failed, has no hits).
- **Provenance.** `pipeline` = no override touched the answer (the release still caps quotes and scrubs prose as
  "Capped strings" below says); `mixed` = an override changed fields of it
  (`override_fields`), while `config_id`, `annotator`, `confidence` and the reasoning still describe the model's own
  answer; `override` = overrides supplied the whole payload (`config_id = override:<ids>`; the {{standins}} stand-ins by Claude
  models are of this kind, with `annotator` naming the model and `decided_at` the time the override was added).
  `origin = human` marks a maintainers' override, not necessarily a per-item human review.
- **Candidates vs roles.** `model_roles.payload.models[]` lists every catalogue model the name matcher found,
  including rows with every role 0. Count a model as present only where `roles.mentioned >= 1` (`mentioned` is at
  least every other role). An all-zero row is a rejected or unexplained candidate; the entry of the document-level `payload.flags[]` that
  names its node says which (rows have no flags of their own): `subsumed:`
  (shorthand for a more specific model, which carries the roles), `bib_only:` (only in the references), `matcher_fp:`
  (the text means something else), `not_found:` (not located); some rows have no flag or two (known inconsistencies).
- **What the models read.** `content.text.sha256` identifies the whole converted text; the calls read parts of it.
  The screen call (its five kinds) reads the title and, of a longer text, the first 12,000 and last 2,000 characters
  (its second, adjudicating pass the first 48,000 and last 8,000); affiliation reads the first 8,000 and last 2,000;
  the technique and model calls read the first 900,000 characters. These are this release's production settings;
  `config/configs.json` does not record all of them.
- **Strengths are prominence, not importance or confidence**: 0 absent, 1 a trace (a sentence), 2 minor but real (a
  paragraph, or a secondary component), 3 a main thread, 4 the document's headline for that role. A load-bearing part
  named once (the method behind every result) grades by its weight, not its word count (VOCABULARY.md).
- **Quotes** are excerpts the model copied from our conversion of the document. Each technique quote carries `anchored`
  (found in the converted text; see "Unverified quotes" above for the rule and the flag `quote_unverified`). A found
  quote need not support the label. Screen-call quotes carry `offset`, a code-point
  offset into the converted text, usable only with `content.text.body`. Model quotes carry neither. The producers already stop technique and model quotes at 300 characters: a
  300-character quote without `…` may have been stopped there, which `release.cuts` does not record, and a producer's
  own `…` can end a shorter quote: detect release cuts by `release.cuts`, not by the ellipsis. {{quote_cap}} Model-written text (reasoning, notes, glosses) is never cut. Quotes are the
  documents' owners' text, not covered by CC0.
- **Prompts** are not shipped: `config/configs.json` names each call's prompt and the sha256 of the template files
  it was pinned to (`components`, `template_digests`); the files are in the pipeline repository
  (public at https://github.com/thesafetystack/the-safety-stack-pipeline).
- **Dates.** `published_date` is the date the providers give for the work's first publication; for an arXiv paper
  that is its first version, while the text is the version `content.source.revision` names, which can be years and a
  new title later (a paper first posted in December 2019 and rewritten in 2026 is dated 2019-12-18). Each source's
  own date is in `identity.members[].published_at`.
- **The release is a projection.** {{remap_sentence}}
- **Stacking releases.** Every row of the corpus carries `release_id`; corpus files of different releases can be
  concatenated into snapshots keyed by (`release_id`, `doc_id`). Count documents within one release. A document merged
  into another keeps the surviving `doc_id` and lists the old one in `identity.former_doc_ids`; compare labels across
  releases with the producing configuration in view: each `config_id`'s entry in `config/configs.json` names its
  prompt, model and taxonomy version, and labels made under different configurations may differ for that reason alone.
