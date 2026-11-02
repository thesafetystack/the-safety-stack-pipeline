# Consume: reading a build or a release

For a reader of the corpus: a researcher, the website builder, an operator checking a build. The queries below ran
on the synthetic world (`docs/SETUP.md` §5) and on a copy of `v4/store-dev`, from the store's root directory.

## What to open

A consumer reads only corpus files, never `documents` chains or `.part` files (DESIGN §2.8). Each is a full file in
a series directory, and a build record names the ones its build wrote:

| Build record key | Directory | File | Rows |
|---|---|---|---|
| `files.corpus` | `data/corpus.build/<series>/` | the `fold.build` output | one row per document passing the plan's build filter (default `has_text`), with the folded annotations typed under `annotations.<name>` |
| `files.corpus_input` | `data/corpus/<series>/` | the `fold.corpus` file the build's annotate stages read | same filter; the `annotations` block holds only `triage` (the other annotations are folded by `fold.build`) |
| `files.corpus_everything` | `data/corpus.everything/<series>/` | the diagnostic fold (only when the plan sets `fold.diagnostic: true`) | every document, masked and retired included, for `excluded_reason()` |

Two ways in. A verified build: the newest `ok` build record of the plan (the entry query below), whose `files` name
its files. A series: `stack head data/corpus.build/<series>/ [--json]` prints the directory's last published file;
`stack ls data/corpus.build/<series>/` lists every one, with its run. A series head is not build-aware: it may come
from an `incomplete` or `failed` build (`docs/OPERATE.md`, "`incomplete` builds"), so a consumer who needs a verified
build goes through the build record. A document's text is the blob `blobs/<content.current_text_sha256>` (markdown;
flat blobs, D360), downloaded on first read from an S3 store.

Store layout (`docs/OPERATE.md`, "The store, jobs and upload"): `builds/<head>.<plan>/build.json.zst` build records,
with the build's run records under `builds/<head>.<plan>/runs/`; `runs/` standalone run records; `data/` immutable
parquet files `data/<family>/<series>/<utc>_<run_id>[_<comment>].{full,part}.parquet` (a file without a series:
`data/<family>/<utc>_<run_id>[_<comment>].full.parquet`; `data/overrides/<series>/` holds the human corrections,
`docs/howto/overrides.md`); `blobs/<sha256>` (flat, D360); `releases/<tag>/`; `cache/` (LLM responses). A file is
published only when an `ok` run record lists it; other files under `data/` are garbage. An S3 store is read through
its local mirror (`v4/mirror/<bucket>/<prefix>/`): `stack status`, `stack ls` or `stack head` bring the record jsons
down, and a data file
arrives when a command reads it or when fetched by key (`docs/howto/s3-store.md`, "Read it from a laptop"); the
queries below then run from the mirror directory.

## The entry query (duckdb)

```sql
-- from the store root; build records are zstd-compressed JSON
SET VARIABLE b = (SELECT files FROM read_json('builds/*.std-v3/build.json.zst')
                  WHERE status = 'ok' ORDER BY finished_at DESC LIMIT 1);
SELECT doc_id, metadata.title, annotations.screen.payload.verdict, content.current_text_sha256
FROM read_parquet(getvariable('b').corpus.path)
WHERE annotations.screen.status = 'ok';
```

Replace `std-v3` with the plan's name: a build's directory is `builds/<head>.<plan>/`, named by its plan, not by its
series. `status = 'ok'` skips `incomplete` and `failed` builds.

## The helper macros

`excluded_reason(d)`, `has_text(d)`, `passed_triage(d)`, `acquire_pending(d)`, `acquire_failed(d)`, `ann_ok(a, name)`,
`ann_verdict(a, name)` and `ann_pinned(a, name, digest)` are duckdb macros defined in `stack/query.py`, not
built-in functions. Plain duckdb fails on them (`CatalogException`). Either register them from Python:

```python
import duckdb
from stack.query import register_macros
con = register_macros(duckdb.connect())
```

or write them to a SQL file once and load it in any duckdb client (the website builder, the `duckdb` CLI; `uv sync`
installs only the Python package, so without the CLI see `docs/howto/inspect.md`):

```bash
uv run --no-sync python -c "from stack.query import MACROS; print('\n'.join(f'CREATE OR REPLACE MACRO {n}({p}) AS ({b});' for n, (p, b) in MACROS.items()))" > macros.sql
```

`excluded_reason(d)` says why a row of `files.corpus_everything` is not in a default build; it is NULL exactly when
the row passes `has_text`. Tokens and their meaning: `docs/howto/why-missing.md`.

## Annotation records

One screen call yields several **annotation records**, each its own typed field `annotations.<record>` and its own
file: `screen` (the call-level summary: verdict, buckets, rationale, quotes, tiers) and, depending on the prompt pin,
`stack_relevance` and `sr_relevance` (per-question verdict, confidence, quotes; SR adds `category`, `agenda`),
`text_usability` (`screen-v11` and later) and `doc_class` (`screen-v12`). Registered workflows and records, in
registry order: `triage`, `screen`, `affiliation`, `doc_class`, `stack_relevance`, `sr_relevance`, `text_usability`,
`model_roles`, `model_regexp_matches` (a record of model_roles), `technique_roles`, `technique_staged`,
`affiliation_orgs` (reads `affiliation`), `translation` (`stack/schemas/payloads/registry.py`; `model_mentions`, D266,
merged into `model_roles` by D591: the matcher's hits are the record `annotations.model_regexp_matches`). A field is NULL when the
build did not fold it (`fold.build.annotations_folded` in the plan).

Every record carries common fields (`status`, `error_class`, `config_digest`, `decided_at`, …) and a `payload`. A
reader who needs one prompt version pins it by `config_digest`; the plan-pinned version is the producer's config
digest under the build's plan:

```python
from stack.annotations import annotation, pinned_digest
digest = pinned_digest("dev-v2-m4", "stack_relevance")         # raises if the plan's screen pin does not produce it
rec = annotation(row, "stack_relevance", config_digest=digest)   # typed field, else other[]; None if absent
```

```sql
SELECT doc_id, ann_verdict(annotations, 'stack_relevance') AS stack, ann_verdict(annotations, 'sr_relevance') AS sr
FROM corpus WHERE ann_pinned(annotations, 'stack_relevance', $digest) IS NOT NULL;
```

`ann_pinned` and `annotation(..., config_digest=)` match that exact config digest. When a stage **accepts** earlier
configs (D758, `docs/CONCEPTS.md` "Accepted configs") a build's rows of those configs are current but keep their own
`config_digest` (the config that judged them), so `ann_pinned` on the plan's digest returns only the rows judged under
it. To take every current row of a kind, read the stage's selection too: in a store, the stage run's record
(`selection`: `target` and `accepted[].config`); in a dataset release, `release.json` `stages.<stage>` (`target.config`,
`accepted[].config`, each with its `config_id` and `documents`, and the `selection_digest`), e.g.
`... WHERE annotations.<kind>.metadata.config_digest IN (<target>, <accepted>...)`. A release's FORMAT.md says per
accepted config how many documents it kept.

`name` must be a string literal. `stack.annotations.ann_expr("stack_relevance", "payload", "verdict")` gives the
same as a polars expression; polars is not a dependency of the pipeline, so install it yourself to use it.

A build written before a format change has a corpus of an older format version (`stack.family_version` in its
footer): read raw by duckdb it shows its own version, so a typed field
added later is absent from its struct and `ann_verdict(annotations, '<new record>')` is a binder error there (`Could
not find key …`). Read `data/` files through `stack.store.parquet.read_family(path)` instead, which returns
`(table, header)`, the table at the current version (an Arrow table duckdb queries directly: `tbl, _ = read_family(path);
con.register("corpus", tbl)`), or use a release; the next build writes current files.

## Text quality

Every current text has an LLM-free quality verdict from acquire's `quality` substage: `ok`; `flagged` (the text is
used, `quality.flags` say why); `failed` (the snapshot is `failed_permanent[quality_<reason>]`, its text digest is kept, and
the document's `current_text_status` reads `convert_failed`). Thresholds: `stack/conversion/quality.py` `THRESHOLDS`; a plan can override them on the acquire line
(`options: {quality: {fail: …, thresholds: {…}}}`); a changed gate re-judges stored metrics on the next `acquire`
without re-converting.

```sql
SELECT doc_id, content.current_text_quality AS bucket,
       list_filter(content.snapshots, s -> s.snapshot_id = content.current_snapshot_id)[1].quality.flags AS flags
FROM corpus WHERE content.current_text_quality IN ('flagged', 'failed')
```

## Releases and the website query

`stack release --in data/corpus.build/<series>/[<file>] --config public-v1 [--tag <tag>]` (alias of `stack fold
release`; a directory means its last published file) releases a `fold.build` corpus that an `ok` build record names
(an `incomplete` one only with `--allow-incomplete`, one no build names only with `--allow-unbuilt`, one only a
`failed` or `cancelled` build names never: D494) and writes `releases/<tag>/` (default tag `<config name>-<YYYYMMDD>`; an existing tag is refused): `corpus.parquet`
(the build's rows that pass the config's `filter` (public-v1: `screen_relevant`, D705), after the licence rules, `column_drops` removed, one snapshot per row
under `snapshots: current`), `release.json` (`build_id, tag, config_digest, config, dropped: {reason: n}, corpus_file_id,
run_id, row_count, schema_digest`, and since D647 `release_format: 2, file_id, audience, filter, filter_checkable, source,
rows_in, filtered_out, classes, content`, since D705 `build_status`: `ok`, `incomplete` or `unbuilt` and what stopped
short) and `blobs/<sha256>` for the bytes the config ships (none in `public-v1`).
Rules: `configs/releases/public-v1.yaml`, `stack/fold/release.py`; `stack verify` checks them (V10), `stack verify
--release DIR` one directory. A release written before D647 (no `release_format: 2`, `text/<digest>.md`) is superseded,
not converted: V10 lists it as a warning without validating it, `--release DIR` reports it; re-release from a build.

The site's per-document shards come from one query over a release (`tests/test_fold_build.py` runs it).
Since D690 the store and the release share one vocabulary (D647 introduced it in the release): a blob reference is a
`BLOB` struct named by its role (`raw`, `text`), a flat string reference ends in `_sha256` (the current text is
`content.current_text_sha256`; the shard key is `text_sha256`), and a `*_digest` is a fingerprint only. The release
side differs in two ways: its `BLOB` is the pipeline's `{sha256, bytes, media_type}` plus `inline` (nullable
`large_binary`), and every field is nullable, for withholding (a withheld snapshot keeps `raw` with `sha256` NULL and
loses `text`; a withheld in-band entry loses both). A release's `text.bytes` is the corpus's recorded size; the convert
pin is `content.convert_pin` and `content.snapshots[].convert_pin`. `annotations.affiliation` is NULL in a build that did not fold it; a withheld text has `content.current_text_status
= 'withheld'` and no blob reference (`current_text_sha256` NULL); the site's alias map is `identity.former_doc_ids`.

```sql
-- CREATE VIEW corpus AS SELECT * FROM read_parquet('releases/<tag>/corpus.parquet');
-- $build_id = release.json's build_id
SELECT doc_id, to_json(struct_pack(
    build_id := $build_id, doc_id := doc_id, former_doc_ids := identity.former_doc_ids,
    title := metadata.title, published_at := metadata.published_at, doc_type := metadata.doc_type,
    verdict := annotations.screen.payload.verdict, buckets := annotations.screen.payload.buckets,
    basis_role := annotations.screen.payload.basis_role, text_sha256 := content.current_text_sha256,
    links := list_transform(list_filter(identity.members, m -> m.is_live),
               m -> struct_pack(source_id := m.source_id, channel := m.channel, url := m.canonical_url)),
    affiliations := annotations.affiliation.payload.items)) AS shard
FROM corpus
WHERE annotations.screen.status = 'ok' AND annotations.screen.payload.verdict = 'relevant' ORDER BY doc_id;
```

DESIGN §2.9 has a researcher and an operator query in the same style (they need the macros above).

### Internal exports

`stack export --in data/corpus.build/<series>/[<file>] --out DIR [--config internal-v1]` (D647; the only local export:
`stack release` and `stack fold release` write the store's `releases/<tag>/` only and refuse `--out`, D695) writes the local directory `DIR` (absent or empty, outside the store root; the tag is its name): no run
record, never pushed, checked before it appears; `stack verify --release DIR` checks one received from elsewhere. `internal-v1` keeps the screen-relevant rows (`ann_verdict` `relevant`; borderline out), ships
the current text of every licence class but `shadow` as `DIR/blobs/<sha256>`, keeps abstracts, excerpts uncapped,
`run_id`s, `evidence_id`s and the screen reasoning, and drops `inband` (its bodies are the `inband`-kind snapshots)
and the `members_digest` hashes of `identity` and `content`. Knobs (a copy of the YAML under another name): `filter` (SQL over the build's row),
`snapshots: all`, `content.select: none|text|all` (`all` adds the raw bytes: ~16 GB for the relevant rows, refused by the
default `max_total_bytes` of 4 GiB), `content.place: inline` (bytes into `content.snapshots[].text.inline` /
`.raw.inline`, `large_binary`, beside their `sha256`, `bytes` and `media_type`; a current text that no kept
snapshot carries still goes to `DIR/blobs/`, so an inline export may have a `blobs/` too), `content.missing: pointer`
(a blob neither local nor remote is listed in `missing-blobs.txt` instead of refusing), `content.provider_records: true`
(with `inband` kept: the `provider_record` bodies, not content per D522). The current text is the current snapshot's:

```sql
-- the macros come with stack.query.register_macros(con)
SELECT d.doc_id, current_snapshot(d).raw.media_type, d.content.current_text_sha256, current_snapshot(d).text.bytes,
       current_text(d) AS text            -- NULL unless the export placed text inline
FROM read_parquet('DIR/corpus.parquet') d;
-- current_snapshot(d) = list_filter(d.content.snapshots, lambda s: s.snapshot_id = d.content.current_snapshot_id)[1]
-- current_text(d)     = decode(current_snapshot(d).text.inline)
```

The parquet footer lists the reference paths (`stack.blob_refs`) and the layout (`stack.blob_layout = blobs/<sha256>`).
In Python, `stack.store.blobs.resolve(row, sha256, stores=[DIR, <mirror>])` returns the bytes from the row's `inline`,
else `DIR/blobs/<sha256>`, else the next store (a mirror downloads on read), checked against `sha256`; `None` when no
store has them. `release.json` records provenance (`source`, `build_id`), never where a store is.
