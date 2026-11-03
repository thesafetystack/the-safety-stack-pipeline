# Design: one `documents` family through the phases, blocks, states, fold, partial versions

**Status: design record.** The design of the file-based ("v4") pipeline, written before the implementation
(2026-10-12). The code implements it with the deltas below, and production runs that code. Where this document and
the code disagree, the code wins, with `docs/DECISIONS.md` for what changed and why; a **Stale** note at the top of a
section marks a known gap. Audience: the maintainers and developers of the pipeline; operators read
`docs/OPERATE.md`, `docs/CONSUME.md` and `docs/howto/`.

The design ("v4f") replaces an earlier draft's twelve families ("v4e") with one `documents` family carried
copy-on-write through `gather → triage → acquire` (six blocks, four states, optional part files) and makes corpus,
build and release one `fold`. Markers in the text: B1, B2, B3 are the design's three branches (chain semantics;
versions; fold and consumers); a `(C<n>)`, `(E<n>)` or `F<n>` citation names a rule in the design notes, which are
not in this repository, and the sentence it annotates states the rule; "ran" means checked on a mock or probe at the
time, the rest is argued; v3 `file:line` citations are to the earlier pipeline (at its commit 9312c8c), which is not
in this repository. How the design was made (its branches, critics and review contract):
`docs/repo-history/superseded-records.md`.

**Deltas (checked 2026-10-14).** The code implements this design with the changes below; where a section disagrees,
the code and `docs/DECISIONS.md` win. Lines that are only history are left as written.
- Chains: `stack/store/chain.py` resolves chains from the run records' `outputs` (D345, below); footers are
  verification only. D22 dropped `input_rule` and `compact_after`; compaction runs at build start by
  `versions.compaction`. Priors are explicit inputs, the series directories below; there is no prior build.
  `corpus`, `<wf>`, `<wf>_evidence` and `triage_evidence` files are always full.
- Formats (D295): each family's format versions and the steps between them live in `stack/schemas/upcasters.py`;
  every file carries `stack.family_version`, every reader reads it upcast to the current version, and a chain may
  mix versions (§7.2's C1 amended; a format change no longer forces a full file, amending D43).
- Records (D344): build records are `builds/<head>.<plan>/build.json.zst`, run records
  `builds/<build>/runs/<head>.<stage>.json.zst` (standalone: `runs/<head>.<stage>.json.zst`), `<head>` =
  `<utc>_<id>[_<comment>]`; they are the store's index: chains resolve from the `ok` records' `outputs` minus
  `excludes` (D345). Build statuses `ok`, `incomplete` (a capped stage; resumed by the next build,
  D265), `failed`, `cancelled`. Annotate runs record their `prompt_pin`, which X6 checks against (D279).
- Annotations: *annotation records* (one screen call writes `screen` plus `stack_relevance`, `sr_relevance`,
  `text_usability`, `doc_class`; D257); chained annotators (registry `reads`, basis kind `chained`, D235; no
  shipped entry uses it since D591); `model_mentions`, `model_roles`, `technique_roles` ported (D266-D268);
  `model_mentions` merged into `model_roles` (one stage: matcher, then one judge call; D591).
- Acquire: text quality verdicts (`SNAPSHOT.quality`, D204); `ROLE.lane` replaces `content.lane`; structured text
  roles `xml`/`html` (D276); the convert pin guard (D250); the read-only `archive` step of D241 is removed again
  (D329; its documents keep their `via = 'archive'` snapshots);
  terminal fetch states rechecked after 90 days (D279).
- Identity: duplicate detectors in `stack/identity.py` (key adders, cross-group keys, matcher edges
  `match:<name>`; D251-D253); URL rules in `stack/urls.py`.
- Stores (D320-D366; operator view `docs/OPERATE.md`, "The store, jobs and upload"): a store is a
  directory, or `s3://<bucket>/<prefix>` worked through a local mirror bound by `.stack-mirror.json` (D321); several
  stores per bucket, one prefix each. Layout: `builds/<head>.<plan>/` (build record, log, marker; its runs under
  `runs/`), standalone `runs/<head>.<stage>.*`, `data/<family dir>/<series>/` (below), flat `blobs/<sha256>` (D360),
  `releases/<tag>/`, local-only `cache/`. Every build and run is a **job** with a record, a zstd log and a `-running.json` marker, ticked every 30 s
  and PUT when it uploads (D352-D357); liveness is the marker's age (dead after 5 min), and an uploading job's start
  recovers final records an earlier crash left local (D353). Upload is per run at publish, so S3 holds every finished
  run. **No locks** (D355, D356): one writer per series; a fork from two writers is reported by the reader's
  `ForkError` (V11) and repaired with `chain orphan <path>`. **No ledgers** (D328): each run's spend is in its record, capped
  per run. **No archive lane** (D329), **no data gc** (D356: `stack gc` is removed), no `store.json`.
  Overrides are a family, `overrides`, written by `stack overrides add` and pinned by their readers (D336-D338,
  §6).
- Series (D372-D375; operator view `docs/OPERATE.md`, "Data files: series and chains" and "Runs stand
  alone"): data files are `data/<family dir>/<series>/<utc>_<run>[_<comment>].{full,part}.parquet` (or
  `data/<family dir>/<utc>_<run>[_<comment>].full.parquet` without a series, read only by exact path). A chain is
  the published files of one series directory in name order (published: an `ok` record lists it, no `excludes`
  removes it); a part's base is the directory's previous file, a full has that base when it extends the series and
  none when it starts or restarts it (`--restart`). Every output records `after`, the directory's last file when its
  run pinned; publishing refuses `head_moved` and `name_order`, and a reader checks `after` against the predecessor
  on its walk (`ForkError`). Data and release PUTs are create-only (`If-None-Match: *`); record PUTs are not. Every
  input is an explicit path (`stack run`/`acquire reopen`/`release`/`verify --in`, `stack fold --input … --out`), and
  a build only passes paths between its lines (`series:`, `in:`, `out:` in the plan; the plans in the tree use
  `series: <plan name>`; per-stage kinds in `versions:` are refused, a line's `out: {kind: part}` replaces them,
  D378). A run's name stamp sorts after its inputs and each destination directory's last file (D381). This replaces: `stack latest` (§2.1; now `stack head <dir>`), `chain_id` as a selector, prior builds,
  `claim_base` (§3.3, §7.6, §7.9; now the publish rule), "the file no one names" as the head (§7.3; now the
  directory's last published file), every file naming a base (§7.2, §7.5; a full without a base is legal), and the header and record keys
  `version_kind`/`base_file_id` (§2.6, §7.2; now `kind`/`base`, plus `after`, `series`, `comment`).
- App. A was patched to the code's schemas on 2026-10-13 and its A.6, A.7 and App. B's S7, X1, X2, X5, X23 to the series
  layout on 2026-10-14; `stack/schemas/` is the source of truth.

## 0. One-page map

> **Stale (checked 2026-10-14):** "Six families" omits `overrides`, a seventh family kind (written by `stack overrides add`). Current: `stack/schemas/families/` (`overrides.py`), D336.

**The chain** (plan `std-v2`):

```
collect.<channel>, collect.<provider>-lookup  →  gather  →  triage  →  acquire (repeat ≤ 4)
  →  fold.corpus  →  annotate.<wf> …  →  fold.build  →  verify  →  build record ok
fold.release: human only.   fold.compact: at build start by versions.compaction, or by an operator.
```

**Six families** (key; writer): `sources` (`source_id`; `collect.*`), `coverage` (window key; `collect.*`),
`documents` (`doc_id`; `gather`, `triage`, `acquire`, `fold.compact`), `corpus` (`doc_id`, the `documents` schema;
`fold.*`), `<wf>` (`doc_id, item_key`; `annotate.<wf>`), optional `<wf>_evidence` (`evidence_id`; incl. `triage`).
Stores: `builds/`, `runs/`, `data/`, `blobs/` (flat `blobs/<sha256>`, D360), `releases/`, `cache/` (D320;
`docs/OPERATE.md`).

**Six blocks** (owner): the flat state block (every writer); `identity`, `metadata`, `inband` (`gather`); `content`
(`acquire`); `annotations` (`.triage` by `triage`, the rest by `fold.build`). **Four states:** `identified`,
`triaged`, `acquired`, `retired`; masking is `identity.is_masked`. **A consumer opens** `files.corpus` only (§2.1).
**Checks:** 10 S + 12 V + 25 X = **47** (Appendix B).

**Read first.** v3 developer: §11, §9, then §2.3-§2.5. Future developer: §0, §2, §3, Appendix A. Operator: §7,
Appendix B. Website builder: §2.1, §2.9, §3.2 (release).

## 1. Principle

A stage is a run with explicit input files; only finished runs reach `data/`; a build is a verified set of finished
files; every choice is made once by a writer in a finished run. Changed from v4e: one `documents` row per document,
its blocks owned by phases and its `state` saying which blocks are valid, and every consumer file a fold of that chain.
Full and part files are defined once, in §7.1.

## 2. Consumer interface

### 2.1 Entry: `stack latest`

> **Stale (checked 2026-10-14):** `stack latest` is removed; the entry is the newest `ok` build record of the plan (the query below still holds) or `stack head data/corpus.build/<series>/`, which is not build-aware.
> Current: `docs/CONSUME.md`, "What to open" and "The entry query", D375.

`stack latest std-v2 [--json]` prints the newest `ok` build record: `files.corpus` (the `fold.build` output);
`files.corpus_input` (its `fold.corpus` file); `files.corpus_everything` when `fold.diagnostic: true`. No alias. Text is at `blobs/<content.current_text_sha256>` (flat, D360).

```sql
SET VARIABLE b = (SELECT files FROM read_json('builds/*.std-v2/build.json.zst')
                  WHERE status = 'ok' ORDER BY finished_at DESC LIMIT 1);
SELECT doc_id, metadata.title, annotations.screen.payload.verdict, content.current_text_sha256
FROM read_parquet(getvariable('b').corpus.path)
WHERE annotations.screen.status = 'ok';
```

The helper macros of §2.8-§2.9 (`excluded_reason`, `has_text`, `ann_verdict`, …) are not built into duckdb: register
them with `stack.query.register_macros(con)` or load the SQL that `stack.query.MACROS` holds (`docs/CONSUME.md`).

### 2.2 Naming table

| Kind | Rule | Examples |
|---|---|---|
| Ids | `<entity>_id`; derived 16 hex, minted 12 Crockford (`stack/core/ids.py:18-31`); `file_id = <run_id>.<family>`; `snapshot_id = <run_id>.<seq>` | `doc_id` = anchor's `source_id` |
| Item key | `item_key` is `''` (never NULL) at document grain, in the key of every `<wf>` file (`db/0001_init.sql:145`, verified) | `affiliation` byline index |
| Digests | `<what>_digest`, 64-hex sha256; always a fingerprint, never a blob reference (D690) | `basis_digest`, `fields_digest`, `members_digest` |
| Blob references | `BLOB {sha256, bytes, media_type}` (all non-null; `bytes` recorded when the blob is written) named by its role; a flat string reference `<what>_sha256` (D690) | `content.snapshots[].raw`, `.text`, `inband.entries[].text`, `content.current_text_sha256` |
| Pointers | `current_<what>` on the row that resolves it | `content.current_snapshot_id`, `current_text_sha256`, `current_revision` |
| Times | `<event>_at`, UTC; booleans `is_<condition>`; lists plural | `provider_updated_at`, `is_masked` |
| File names | `data/<family dir>/<series>/<utc>_<run_id>[_<comment>].{full,part}.parquet`, or `data/<family dir>/<utc>_<run_id>[_<comment>].full.parquet` without a series; `<utc>` = `YYYYMMDDTHHMMSSZ`, one stamp per run, after its inputs' and its destination directories' last files (D381); names order a directory's files (D372) | `data/documents/weekly/20261014T080102Z_r3rpts3mehemj.part.parquet` |
| File header (`stack.*`) | parquet key-value metadata; v4e's keys plus: `stack.kind ∈ {full, part}`; `stack.base` (the base file's name in its directory; empty for a full without one); `stack.after` (the directory's last file when the run pinned); `stack.series`; `stack.comment`; `stack.chain_id` (the family directory); `stack.schema_digest` (its declared format version's); `stack.family_version` (D295); `stack.ids_digest`; `stack.blocks_written`; `stack.fold_config`; `stack.filter`; `stack.annotations_folded`; `stack.release_config` | Appendix A.6 |
| Stages | `phase` in prose, `stage` in records; `fold.<config>` the one spelling; `triage` is a stage, not `annotate.triage` | `fold.corpus`, `fold.build` |

### 2.3 Families

> **Stale (checked 2026-10-14):** the table omits the `overrides` family, and there is no `--prior` flag: earlier outputs are read as `--in` series directories. Current: `stack/schemas/families/`, `docs/OPERATE.md` "Runs stand alone", D336, D373.

| Family | Key | One row means | Writer | Who reads |
|---|---|---|---|---|
| `sources` | `source_id` (in a chain `(source_id, run_id)`) | one provider's view of one item seen by one run, or a miss | `collect.*`, `collect.v3_import` | `gather`; auditors (the chain is the observation history) |
| `coverage` | `(channel, query_digest, window_start_at, window_end_at)` | one query window or lookup batch | `collect.*` | `collect.*`, verify |
| `documents` | `doc_id` | one document as its last writer left it | `gather`, `triage`, `acquire`, `fold.compact` | chain phases, `fold.corpus`, verify |
| `corpus` | `doc_id` | one document selected by a fold filter | `fold.corpus`, `fold.build`, `fold.release` | consumers, `annotate.<wf>` |
| `<wf>` | `(doc_id, item_key)` | the selected result of `<wf>` per document or item | `annotate.<wf>` | `fold.build`, named workflows |
| `<wf>_evidence` | `evidence_id` | one invocation | `annotate.<wf>`, `triage` | `--prior`, verify |

Evidence is a capability: `evidence: true` for anything not recomputable; a pure workflow writes only `<wf>` (V-evidence).

### 2.4 Block ownership

| Block | Owner | Columns (Appendix A) | Changes when |
|---|---|---|---|
| flat state block | every writer | `doc_id, state, run_id, created_at, updated_at` | `created_at` once, by `gather`; stamps restamped **only where an owned block changed** (C6) |
| `identity` | `gather` | anchor, members, keys, `former_doc_ids`, `merged_into`, masking, `members_digest`, stamps | membership, overrides, merge, split |
| `metadata` | `gather` | merged fields, `field_sources[]`, `fields_digest`, `sensitivity[]`, `is_withdrawn`, stamps | a member's stable projection, a `fields` override |
| `inband` | `gather` | `entries[]` per `(source_id, content_role, digest)`, stamps | a new in-band body |
| `content` | `acquire` | `snapshots[]`, `roles[]`, `current_*`, `revisions[]`, `members_digest`, `same_raw_doc_ids[]`, stamps | fetch, conversion, pointer move, carry |
| `annotations` | `triage` (`.triage`); `fold.build` (other typed fields, `other[]`) | one typed `ANN<P>` per registered workflow, `other LS(ANN<JSON>)` | `triage` re-decides; in `documents` only `triage` is non-NULL |

A full `gather` carries unchanged rows byte for byte. X15 `block_ownership` compares every block outside
`stack.blocks_written` null-safely with the base chain view's (plain `=` passes a write into a NULL block, B1 probe, ran).

### 2.5 States

| State | Required blocks and digest agreements | Set by | Means to a consumer |
|---|---|---|---|
| `identified` | `identity`, `metadata`, `inband`; `content`, `annotations.triage` may be carried, possibly stale | `gather` | merged; nothing decided on current fields |
| `triaged` | + `annotations.triage` with `basis_kind = 'fields'` and `basis_digest = metadata.fields_digest` | `triage` | gate decided on current fields |
| `acquired` | + `content` with `content.members_digest = identity.members_digest`; `current_text_status` set; `current_text_status = 'ok' ⇔ current_text_sha256` resolves | `acquire` | acquisition ran over current members; text may be pending, failed or absent |
| `retired` | `identity.merged_into` set (merge) or NULL (split); other blocks frozen | `gather` | tombstone; redirect via survivors' `former_doc_ids` |

Transitions: forward by the owning phase; `→ identified` when `gather` changes `members_digest` or `fields_digest`
(carried blocks stay, `acquire.carry` reuses them); `→ retired` by `gather` on a merge or split; `retired →
identified` on `separate`; otherwise monotone (X19). Masking (`is_masked`, `mask_reason ∈ {operator,
review_required}`) is orthogonal and keeps acquisition progress. `members_digest` covers all members, so liveness
never demotes `acquired` (C18). **Derived macros** in `stack/query.py`, not states (dissent §10.3): `acquire_pending()`
= `acquired` with `convert_pending` or a role due later; `acquire_failed()` = no role can still succeed and
`current_text_status <> 'ok'`.

### 2.6 Vocabularies

| Column | Values |
|---|---|
| `status` (role, snapshot, `ANN`, run outputs; frozen) | `ok, empty, partial, failed_transient, failed_permanent, quarantined, skipped, not_attempted` |
| `error_class` transient | `rate_limit, timeout, network, http_5xx, provider_auth, forbidden, crashed` |
| permanent | 21 classes (`stack/schemas/vocab.py` `ERROR_CLASSES`): `dead, blocked, paywalled, no_fetchable_url, parse_error, too_short, too_large, corrupted, truncated, schema_invalid, query_invalid, provider_plan, config_missing, license_gated, code_bug` and the quality gate's `quality_{stub, too_short, abstract_only, references_only, garbled, markup}` |
| quarantined | `cap_reached, operator` |
| skipped | `review_required, stale_basis, no_text, masked, not_relevant, undecided, lapsed_scope, no_basis, withdrawn, gated, gate_failed, not_needed, not_requested` |
| not_attempted | `halted, budget, limit, unanswered` (`unanswered`: `llm_mode: batch`, the run's batch gave no answer; the next run asks again) |
| `state` | `identified, triaged, acquired, retired` (`documents`, `corpus`); `observed, missing, retracted` (`sources`) |
| `content_role` | `full_text, xml, html, archive, post, page, readme, abstract, fields`; pointer rank `full_text < post < page < readme < abstract < fields`; never text: `xml`, `html`, `archive`, except `xml`/`html` as structured text under the acquire option `structured` (D276); `fields` = a rendered structured record. Not content: the record role `provider_record` (`RECORD_ROLES`, D522), the source's record of the item as received, raw only, never wanted, never a snapshot, never text |
| `snapshots.kind` / `via` | `raw, inband` (a text is a field of its raw snapshot, D9) / `live, headless, wayback, manual, api, inband, convert, archive` |
| `current_text_status` | `ok, convert_pending, convert_failed, no_raw`; `withheld` in release files only |
| `current_text_media` | `md` (the default fold filter requires it), `txt`, `html` |
| `lane` | `arxiv, oa, http, inband` |
| `edge_kind` | `anchor, key:<kind>, match:<matcher>, human:merge` (matchers: `stack/identity.py`) |
| `basis_kind` | `fields, text` |
| `origin` | `computed, human, ai_check, import, rule` on `ANN`; `provider, human, import` on `sources.metadata` |
| `version_kind` [now: header and record key `kind`, D372] | `full, part` |
| `fold_config` | `corpus, build, release, compact` |
| `license_class` (on `MEMBER`) | `open, public_web, restricted, api_tos, api_abstract, shadow` |
| `sensitivity` | `pii, legal` |
| `withdrawn_reason` | channel-specific: `deleted, withdrawn, dead, closed` |
| `miss_means` (`channels.yaml`) | `withdrawn, unknown` |
| `triage.payload.decision` | `fetch, skip, pending` |
| run record `status` | `running, ok, failed, cancelled` |

`ok` = a decided result with a payload; `empty` = examined, nothing found where the payload is a set.

### 2.7 Disambiguation

| Term or field | Means here (not) |
|---|---|
| phase / stage / run | a chain step, in prose / its name in records / one execution (`run_id`) |
| `version` / `revision` / `schema_version` / workflow version | file level only: newest full file plus later part files / a document's edition (arXiv v2, page edit): `current_revision`, `revisions[]`, `snapshots[].revision` / the header key / `ANN.version`, the workflow contract version (v3 `workflow_version`) |
| full file / part file / version / compaction | §7.1; compaction = `fold.compact`; never "partial version" |
| base / prior / input | the chain predecessor a file names / an earlier output of the same stage / any file read |
| head / chain view / latest | the chain's last file / the union-and-select result / builds and chain heads only |
| content / text / body / blob / inband / `provider_record` | the block / Markdown a `text` BLOB names / what a member delivered in-band / bytes under `blobs/` / the block of in-band entries / the in-band entry holding the source's record of the item as received (a raw blob; not content, D522; replaces `metadata.record_ref`, D524) |
| source / member / document / observation | provider item (`source_id`) / a source in `identity.members[]` / `doc_id` / one `sources` row |
| status / state / decision | outcome token of a role, snapshot, `ANN`, run / the row's block invariant / triage's `fetch, skip, pending`; no row `status` |
| current / canonical | the pointer rule's choice, not "newest" / unused alone; "canonical identity" = `doc_id` |
| `created_at` / `updated_at` / `provider_updated_at` / `observed_at` / `captured_at` / `as_of` / `decided_at` / `published_at` | row minted / last writer of row or block / provider's date / observation seen / blob obtained / time the content represents (archive time, else `Last-Modified`, else `captured_at`) / annotation made / item's own date |
| masked / withdrawn / retired / dead / retracted | `identity.is_masked`, orthogonal to state / `MEMBER.withdrawn_at`; `metadata.is_withdrawn` = all members / tombstone / role `error_class` after a 404 / `sources` state for a removed record |
| live | `MEMBER.is_live` (not masked or split off) / capture `via <> 'wayback'` / row not `retired`; never "not withdrawn" |
| record | not a `sources` row (an observation) / the `provider_record` in-band entry (D522) / `record_format ∈ {record, miss}` / run and build records |
| `fields_digest` vs `members_digest` | `H(title, abstract, authors[].name, published_at, doc_type, inband_excerpt_digest)`, triage basis / `H(sorted member ids)`, all members, acquisition basis |
| complete row | all columns serialised, permitted NULL blocks included |
| `corpus` / `corpus_input` / `corpus_everything` | family and the build's file / the `fold.corpus` file of a build / optional diagnostic fold, filter `everything` |
| `channel_group` / `provider_version` / `license_class` | identity group (`paper, forum, url, social, record`) / lookup provider schema version / member licence, read by release |
| ANN common fields | the fixed annotation fields; never "base" |

### 2.8 What a consumer never does

Union files, follow a chain, open a `.part` file, join `documents`, read a `<wf>` file, pick the newest row per id,
rank snapshots, remap identity (`former_doc_ids`; no alias rows, C24), apply licence logic, recompute staleness (C25),
or reconcile builds. "Why not in the build" is `excluded_reason()` over `corpus_everything` (precedence
`is_masked`, `state` (triage only for a row not yet acquired), `current_text_status`, `current_text_media`; screen
never excludes, D230; `stack/query.py`); no reason column.

### 2.9 Three queries

B3's forms (ran under B3's names), corrected per C15, C16, C26; argued in this form.

**Researcher.**

```sql
SELECT annotations.screen.status, annotations.screen.error_class, annotations.screen.payload.verdict,
       annotations.screen.payload.basis_role, count(*) FROM corpus GROUP BY ALL;
SELECT doc_id, excluded_reason(d) FROM corpus_everything d WHERE doc_id = ?;
```

**Operator.**

```sql
SELECT state, content.current_text_status, identity.is_masked, count(*) FROM corpus_everything GROUP BY ALL;
SELECT doc_id, r.content_role, r.status, r.error_class, r.attempts, r.retry_after
FROM corpus_everything, unnest(content.roles) AS t(r) WHERE r.status <> 'ok';
SELECT doc_id FROM corpus WHERE annotations.screen.error_class = 'stale_basis';
```

**Website builder** (on the release file; shards written from Python):

```sql
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

One row per `doc_id`; `annotations.affiliation` is NULL, not missing, in a build that did not fold it. The site's
alias map is `identity.former_doc_ids`.

## 3. Stage contracts

### 3.1 Contract table

> **Stale (checked 2026-10-14):** the command line and the head/chain/prior reads predate the series layout: a run takes repeatable `--in <series dir or file>`, `--out`, `--part|--full`, `--restart`, `--only`, and `<wf>` files are always full.
> Current: `docs/OPERATE.md` "Runs stand alone", `configs/plans/README.md` "What a build line reads and writes", D14, D373.

`stack run <stage> --input <family>=<file>… [--filter] [--prior] [--only]`. Caps frozen: fetch 4 per role, convert 3
per snapshot, annotate 3; backoff: due again at once after a first failure, then `1h·2^(attempts-1)` capped at 7 d; terminal fetch states are
rechecked after 90 d (`stack/fetchcycle.py` `backoff`, `TERMINAL_RECHECK`; D221, D279).

| Phase | Reads (union?) | Writes (default) | Owns | Carries | Scope / filter |
|---|---|---|---|---|---|
| `collect.<channel>`, `collect.<provider>-lookup` | own `sources` chain (union, to avoid re-minting, C32); lookups: `documents` head keys | `sources` part; `coverage`; in-band blobs | the observation | nothing | windows / keys not done; optional exclusion of `skip` under X16 |
| `gather` | all `sources` chains (latest row per `source_id`, ranked across chains, C30); `documents` head; overrides; `channels.yaml` | `documents` **full** | `identity`, `metadata`, `inband`, `created_at`, tombstones | `content`, `annotations`; on a merge the retired row's `content` | all sources |
| `triage` | `documents` head; `gate:`; `triage_evidence` prior | `documents` **part**; `triage_evidence` | `annotations.triage` | the rest | unmasked, not `retired`, and `identified` or triage basis/config stale |
| `acquire` (≤ 4) | `documents` head; blobs | `documents` **part**; blobs | `content` | the rest | triage `fetch`, or `skipped` with `no_basis`/`gate_failed`; ∪ roles due; ∪ rows with `inband.entries[]`; ∪ `content.members_digest IS DISTINCT FROM identity.members_digest` |
| `fold.corpus` | `documents` chain | `corpus` full | nothing | the row | `fold.corpus.filter` (`has_text`) |
| `annotate.<wf>` | the build's `fold.corpus` file (no union); named `<wf>` files (D235: registry `reads` = basis, line `reads:` = scope only); evidence prior | `<wf>` full (part optional); `<wf>_evidence`; LLM cache | `<wf>` | n/a | plan filter |
| `fold.build` | the build's `fold.corpus` file (never the head); the build's `<wf>` files | `corpus` full | `annotations.<wf>`, `other[]` | the rest | `fold.build.filter`, after the join |
| `fold.release` | a build's `corpus` | `releases/<tag>/` | nothing | per config | minus `row_drops` |
| `fold.compact` | `documents` chain | `documents` full, base = superseded head | nothing | every row and stamp | none |

**The triage gate.** `gate:` holds per consumer `sql:` clauses (over the gate view: the row plus the anchor's
`inband_text_sha256` and `channel_group`, `stack/gate.py`, X14; `inband_text_digest` before D690: the rename moved
every plan's gate digest, so triage re-decides each row once, with no LLM call) and `question:` clauses (prompt pin, `fetch_on:`
verdicts). A `rule:` clause (track S, S5) names a triage source rule of `stack/rules.py` (channels, URL prefixes,
authors, title keywords, doc types; one place in code) and is a `sql:` clause whose SQL is the rule's compiled
predicate, carried in the gate as `rule_sql` so every gate digest covers it (a rule-list edit re-scopes triage).
Basis `fields`; `origin = 'rule'` when `sql:` alone decided. First match wins:

| # | Condition | `decision` | `status[error_class]` |
|---|---|---|---|
| 1 | some `sql:` clause matches, or some question's selected `ok` verdict is in its `fetch_on` | `fetch` | `ok` |
| 2 | every clause conclusively negative | `skip` | `ok` |
| 3 | no clause matched and every question lacks a basis | `pending` | `skipped[no_basis]` (v3's arm 2, `db/0002_views.sql:90-91`) |
| 4 | no positive clause; no question can still resolve and at least one is terminal | `pending` | `skipped[gate_failed]` |
| 5 | no positive clause; some in-scope question is `not_attempted` or `failed_transient` | `pending` | that question's status (first in plan order) |
| 7 | checked before 6: arm 6's case, every question row a re-derived fill, the row outside the question scope because its primary role is `ok` (or, whatever its status, still points at a held raw snapshot, D704), and it has a **standing** (`fetch` keeps `wanted_by` ∩ the gate's consumers, non-empty; `origin` carried) | the standing's `fetch`/`skip` | `ok` |
| 6 | otherwise: every remaining question outside the question scope with no carried answer | `pending` | `skipped[not_needed]` |

A row's **standing** (arm 7, T5.5) is its newest decided triage annotation (`ok`, `fetch`/`skip`): the current one, else
the newest for its `doc_id` in the pinned `documents` chain's earlier files, so already-acquired documents keep their
triage standing when a later gate does not ask about them; a `pending`, `skipped[not_needed]` row with a primary role
`ok` and a standing is in triage scope (the repair), X14 recomputes the stored decision as its own standing.

`pending` is no permission to fetch; `acquire` fetches `no_basis` and `gate_failed` once. Questions are asked only
for rows not `fetch` by `sql:` whose primary role is not `ok`. `wanted_by` = consumers with a positive clause. A
`gate:` edit reruns `triage --only select`, no calls.

### 3.2 The fold contract

`stack fold <config> --input documents=<head>|corpus=<file> [--filter] [--join <wf>=<file>…] [--release <config>]`
[now: `documents=<series dir or file>`, plus `--out <dir>` (required for `corpus`, `build`), `--restart`, `--comment`, `--tag`; D379],
one `stack/fold/` package, five steps in this order (B3's filter-before-join culled, Astra future developer):

1. **union** the `documents` chain if the input is one (§7.3);
2. **join** each `<wf>` (an annotation record, A.4, is joined like any registered workflow; `stack build` takes
   its file from the producer's run and refuses a plan folding a record the pinned producer prompt does not
   produce) on `doc_id` into typed `annotations.<wf>` or `other[]`; item grain into `payload.items[]`;
   recompute `is_basis_current`; **mapping M**: an `ANN` whose `basis_digest` differs from the row's current basis
   (`current_text_sha256` for `text`, `fields_digest` for `fields`) becomes `skipped[stale_basis]`, payload kept
   (C15; consumer F1 ran: 3 of 20 shards carried a stale `ok`);
3. **filter**, after the join, so a build filter may read `annotations.screen`;
4. **release rules** if releasing;
5. **write** one full file with the fold header keys.

| Config | Input | Filter (default) | Join | Release | Output |
|---|---|---|---|---|---|
| `corpus` | `documents` chain | `has_text` = `state = 'acquired' AND NOT identity.is_masked AND content.current_text_status = 'ok' AND content.current_text_media = 'md'`; `passed_triage` = `state <> 'retired' AND NOT identity.is_masked AND annotations.triage.status = 'ok' AND annotations.triage.payload.decision = 'fetch'`; `everything` = `true` (includes masked and retired); any SQL predicate over the row | none | none | `corpus` (`files.corpus_input`; `files.corpus_everything` when diagnostic) |
| `build` | the build's `fold.corpus` file | plan `fold.build.filter`, default `has_text`; a consumer inclusion is a plan clause such as `annotations.screen.payload.verdict = 'relevant'` | plan `annotations_folded` | none | `corpus` (`files.corpus`) |
| `release` | a build's `corpus` | the build's rows | the build's block | release config | `releases/<tag>/` |
| `compact` | `documents` chain | none | none | none | `documents` full, base = superseded head |

**Release config** (`configs/releases/<name>.yaml`; resolved and embedded in `releases/<tag>/release.json`):
`licence_rules` per `license_class` (`rows: keep|drop`, `text: ship|pointer|withhold`, `abstract: keep|drop`,
`excerpt_cap_chars`), read from the **member that supplied** the field or snapshot (C18); registry `text_bearing` caps
(C17); `other[]` dropped by default; `metadata.sensitivity` (C21); `column_drops` (default `inband`, `run_id`s,
`evidence_id`s, `screen.payload.reasoning`, `text_usability.payload.note`); `row_drops` (default `shadow`, masked, `retired`). Withheld text:
`current_text_status = 'withheld'`, `current_text_sha256` NULL, a snapshot keeps `raw` with `sha256` NULL and loses
`text`, an in-band entry loses both (C25). Defaults (v3
`configs/policies.yaml:95-117`, now in v3's `configs/policies.yaml`; unenforced in v3): `open`, `public_web` keep, text `pointer`, abstract keep;
`restricted`, `api_tos`, `api_abstract` keep, text `withhold`, abstract drop; `shadow` dropped. Build and release
differ by config, not code.
Since D647 a release config also has `audience` (advisory; `internal` needs `stack export --out` unless it ships no bytes), `filter`
(named or SQL over the input rows, before the rules; `screen_relevant` for internal exports), `snapshots: current|all`
(default `current`: only the snapshot `current_snapshot_id` names) and `content {select: none|text|all, place:
blobs|inline, max_total_bytes, missing: fail|pointer, provider_records}`; bytes ship per element by its own supplier's
class, into `blobs/<sha256>` (replacing `text/<digest>.md`, whose releases are superseded, not converted) or the release schema's `inline` (`large_binary`), after a
pre-flight of availability and size. The release schema uses the pipeline's names (D690; D647 introduced them in the release only): the release
`BLOB` is the pipeline `BLOB` plus `inline`, every field nullable, at `content.snapshots[].raw` / `.text` and
`inband.entries[].raw` / `.text`; flat `content.current_text_sha256`; `content.convert_pin`,
`content.snapshots[].convert_pin`; a `*_digest` is a fingerprint. A release's `text.bytes` is the corpus's recorded size. `stack export --out DIR` writes a local export directory (no run record, never pushed; default config
`internal-v1`); `stack release` writes only the store's `releases/<tag>/` (D695). Both shipped configs also drop the `members_digest`s;
no config may drop the references the writer fills or the fields the licence is read from (`KEPT_FIELDS`). Checks: `validate_release`
(V10, the writer over its stage for both destinations, `stack verify --release DIR`; a release before D647 is a V10 warning, not
validated; it reads no symlink, and a bundle's `filter` runs without file access). Consumer view: `docs/CONSUME.md` "Releases and the website query".

### 3.3 `--only`, scope and `--prior` under part files

> **Stale (checked 2026-10-14):** the head pin, `--prior`, `claim_base` and a build-start `head_descends` are gone: inputs are explicit `--in` paths pinned in `in_files`, the publish rule (`head_moved`, `name_order`) replaces `claim_base`, and X23 runs per file in verify; `--only` (D32) remains.
> Current: `docs/OPERATE.md` "Runs stand alone" and "Data files: series and chains", D372, D373, D375.

`stack run` pins the chain head in the run record (`inputs.documents`); a non-head `--prior` is refused and a `corpus`
is never a base (C2). Before dispatch and at build start, `head_descends` (X23, C3); at publish, `claim_base` (§7.6).
`--only` skips the work of unnamed substages; pure substages (gather's `union`, `merge`, `carry`; acquire's `carry`, `inband`, `quality`, `point`; triage's and annotate's
`select`) always run. Scope = due
chain-view keys ∪ filtered keys; unreached keys are `not_attempted`; planned stops finish `ok` with `scope_complete =
false`.

### 3.4 Build order

As the §0 chain, with `annotate.screen` then `annotate.affiliation`; `acquire` repeats until `scope_complete`
(max 4), so a `documents` chain holds at most 6 files per build.

### 3.5 The plan file

> **Stale (checked 2026-10-14):** `stack.plan.load_plan` now refuses this sample (per-stage kinds in `versions:`), and a build refuses a line without a series; plans set `series:` and `out: {kind: part}` on collector, triage and acquire lines.
> Current: `configs/plans/std-v3.yaml`, `configs/plans/README.md`, D378.

Updated 2026-10-13 to a sample `stack.plan.load_plan` accepts (the original showed `input_rule` and
`compact_after`, refused since D22). Field reference: `configs/plans/README.md`; real plans: `configs/plans/`.

```yaml
plan: std-v2
schema_version: 2
prompts:
  triage.safety-relevance-v3: {name: safety-relevance-v3, model: openai/gpt-6-luna}
  annotate.screen: {name: screen-v12, model: openai/gpt-6-luna, adjudicate_model: openai/gpt-6-luna}
  annotate.affiliation: {name: affiliation-v1, role: extract_main}
gate:
  screen:
    - {rule: frontier_lab_posts}
    - {question: safety_relevance, prompt: triage.safety-relevance-v3, fetch_on: [relevant, borderline]}
    - {sql: "metadata.abstract IS NULL AND inband_text_sha256 IS NULL AND channel_group = 'url'"}
  no_basis_fetches: true
acquire: {convert: {config: pymupdf4llm-v1}, wants: {arxiv: [full_text], openalex: [full_text, xml],
          lesswrong: [post], blogs: [page], x: [post]}, recheck: {default: weekly}}
versions: {gather: full, triage: part, acquire: part,
           compaction: {max_partials: 12, max_partial_rows_ratio: 1.0}}
fold:
  corpus: {filter: has_text}
  build: {filter: has_text, annotations_folded: [screen, stack_relevance, sr_relevance, affiliation]}
  release: {config: configs/releases/public-v1.yaml}
  diagnostic: false                        # true: files.corpus_everything
recovery: {dead_run_charge: false}       # D1
stages:
  - {stage: collect.openalex, prior: [sources, coverage]}
  - {stage: collect.openalex-lookup, prior: [sources, coverage], keys: [doi, pmid, arxiv], miss_cap: 3}
  - {stage: gather}
  - {stage: triage, prior: [triage_evidence], budget: {max_usd: 20}}
  - {stage: acquire, budget: {max_minutes: 90}, repeat: {until: scope_complete, max_runs: 4}}
  - {stage: fold.corpus}
  - {stage: annotate.screen, prior: [screen, screen_evidence], budget: {max_usd: 40}}
  - {stage: annotate.affiliation, prior: [affiliation, affiliation_evidence],
     filter: "metadata.doc_type IN ('paper', 'preprint') AND content.current_role = 'full_text'"}
  - {stage: fold.build}
require_complete: [sources, coverage, documents, screen, screen_evidence]
```

`configs/llm.yaml` stays the LLM config; the cache key is `H([model, rendered_digest, basis_digest])`.
`channels.yaml` gains per channel: `channel_group`, non-merging key kinds (`related:`, `relay:`, the X context keys),
`miss_means`, and the stable projection for `record_digest` (C23). The payload registry
(`stack/schemas/payloads/registry.py`) gains `grain`, `text_bearing`, `version` per workflow.

## 4. Boundaries

| Boundary | Kept, merged or new | Why |
|---|---|---|
| `collect` ‖ `gather` | kept | `gather` needs every channel; reruns at zero network |
| `gather` ‖ `triage` ‖ `acquire` | kept, now blocks of one row | one budget kind per run; `acquire` repeats alone |
| `triage` ‖ `screen` | kept apart | different questions on different bases (`fields`, `text`) |
| fetch + convert + revisions | merged in `acquire` | `acquire --only convert`: zero network |
| phase ‖ fold (new) | new cut | only folds write `corpus`; a `corpus` is never a base |
| selection ‖ inclusion (new) | new cut | `annotate.<wf>` selects; inclusion is a plan fold filter |
| build ‖ release (new) | a config, not code | one `stack/fold/` package; release = build's rows + release config |

## 5. The row

### 5.1 `documents`, block by block

Owners and change triggers in §2.4; exact columns in Appendix A.3. The flat block has no row `status` and no row
attempts (retry state per role, conversion attempts per snapshot, C8); a schema-invalid row has no row status: the
S-checks refuse the file. `metadata.abstract` is inline and priced (C36). Every struct block carries `run_id` and
`updated_at`.

### 5.2 `sources` and the shared field vocabulary

Columns in A.5; `state ∈ {observed, missing, retracted}`. **Identical structs**: `INBAND`, and `METADATA` minus the
side-specific fields (`origin`, `provider_version` on `sources`; `field_sources`, `fields_digest` on
`documents`): a "shared field vocabulary", not shared blocks. `record_digest` is over a per-channel stable projection
(counters excluded, C23). One chain per collector, part files; **`sources` compaction is operator-only and off by
default**, so the retained part files are the observation history; a `source_id` may sit in several chains (C30).

### 5.3 The merge over members

Per document `gather` writes `identity` from the members; `metadata` by `human fields override > provider rank > newest
observed_at` (a year-only `published_at`, 1 January at midnight, ranks after every other date, D644), with
`field_sources[]`, v3's `empty_is_absent` and author union (`stack/read.py:205`, `configs/policies.yaml:53`);
`inband.entries[]` = the union of members' entries. **Copy** for X and pages (one member, byte for byte); **aggregate**
for papers.

### 5.4 Bodies and in-band content

`collect` mints in-band bodies to `blobs/` (flat, D360); `gather` lists them in `inband.entries[]`; `acquire` also makes each an
`inband` snapshot (C20), so edit history survives `sources` compaction. An entry may have `raw` NULL and
`text` set (C33). In-band roles are minted whatever the gate said.

### 5.5 The pointer rule

Candidates: `snapshots[]` with `kind IN ('raw','inband')` of members that are live (not masked, not split off);
withdrawn members stay candidates. Order: role rank, `via <> 'wayback'` first, `as_of DESC`, `captured_at DESC`,
`snapshot_id`. The first is current, then its text:

| Current entry | `current_text_sha256` | `current_text_status` |
|---|---|---|
| none | NULL | `no_raw` |
| `inband` | its `text.sha256` | `ok`; `convert_pending` if not converted in the same run (V-content flags it) |
| `raw`, conversion at the plan pin `ok` | its `text.sha256` | `ok` |
| `raw`, conversion `failed_*` or `quarantined` | NULL | `convert_failed` |
| `raw`, no conversion yet or `not_attempted` | NULL | `convert_pending` |

Never consulted: older occurrences' text, other configs, masked members, never-text roles. Role before currency: an
abstract never displaces a full text (v3 guards it at mint time, `stack/collect/inband.py:144-151`). A newer raw
occurrence whose conversion failed gives `convert_failed`, never an older text.

### 5.6 Channel walk-through

B1 §5.3 corrected by C18-C23 and the channels critic's table.

| Channel | Identity | In-band at first contact | `acquire` fetches | Retry | Edit | Deletion |
|---|---|---|---|---|---|---|
| arXiv | paper; bare id (`arxiv_sweep/main.py:215-224`); DOI bridge | title, authors, abstract | `full_text` of the current revision | role backoff, cap 4 | v2 = new `raw` snapshot, `revision = 'v2'`, pointer moves | `withdrawn_at`, `metadata.is_withdrawn`; state kept |
| OpenAlex | paper; DOI else `W…` | rebuilt abstract, `license_class` per member | OA PDF; `xml` (never text) | a 429 halts the run, no row charge | new `sources` row only if the stable projection moves | miss = `unknown`, never withdrawal |
| LW/AF crossposts | forum, `fm_post`: one document, two members (runtime u) | body / `htmlBody` | none; http if no body | cap 4 on fallback | new body = new `inband` snapshot | `withdrawn_at` on that member; the other stays current |
| Pages, Wayback | url, canonical URL | HTML if fetched at collect | live, headless, wayback | 304 or equal digest mints nothing | new `raw` snapshot ordered by `as_of` (C22) | 404: role `dead`, pointer on last good; archived-only: newest capture |
| X posts | `social`, **one post per document**; `conversation_id`, `reply_to_id`, `quote_of`, `repost_of` non-merging; repost collapses; quote post own document | post JSON, one entry | none; links → url sources (`relay:`) | n/a; `convert_failed` on render | new body = new snapshot; threads are an extraction view | miss with `miss_means: withdrawn` → `withdrawn_at`, text kept; release drops under `api_tos` |
| Incidents, OSINT | `record`, registry id | record → role `fields`; raw as the `provider_record` entry | none; report URLs separate (`related:`) | n/a | each import = new snapshot | posting closed = dated event |
| Forums (Reddit, HN) | forum, submission id | title, body, top comments | later pages | per page | growing thread = new snapshot; triage basis unchanged | `withdrawn_at`, release caps 0 |
| Code repositories | url, repo URL | metadata, README | `readme` at `(url, path, commit_sha, selector)`; archive = role `archive` | cap 4 | snapshot on README digest change; `revision` = tag else sha | 404: `dead`, text kept |
| Leaks | outlet article (url); raw only `via = 'manual'` | article page | `page` | as pages | corrections = snapshots | `shadow` + `sensitivity`; release drops the row |

Surprises (B1): (a) first contact usually carries the whole text, so `acquire` is mostly a converter; (b) an edit is a
re-observation first and a `content` change second, so the pointer rule, not `sources` order, sets the current
revision; (c) deletion is never a row deletion.

## 6. Identity

Once per build in `gather`, after the lookups, on keys only; acquisition never feeds identity. `doc_id` = the anchor's
`source_id` (earliest observed, masked included). Groups in `configs/channels.yaml`: **paper** (`doi, arxiv, openalex,
s2, pmid, zenodo`; arXiv-DOI bridge `stack/scholarly_records.py:129, 196-197`), **forum** (`fm_post`), **url**,
**social** (`x_post`), **record**; edges stay within a group (v3 matched globally, `stack/core/writer.py:928-931`).
Overrides: the `overrides` family (targets `identity`: `merge`, `separate`, `mask`; `fields`; `<wf>`: `row`, `field`,
`quarantine`; `manual_sources`), added by `stack overrides add`, pinned as an input by its readers; what wins:
`docs/howto/overrides.md` (D336-D338).
**Detectors** (D251-D254): `stack/identity.py` declares them in one place. Key adders give a source extra
keys from its own record (a LessWrong linkpost's target URL, an OpenAlex arXiv location, a feed guid, URL alternates
of `stack/urls.py`) at collect time and in `stack migrate rekey`; a forum source's `url` key joins the url group.
Matchers join sources of one class (paper, post) by normalised title plus first-author surname within a year
(papers) or author/channel within 7 days (posts): edge `match:<name>`. A `separate` removes key and match edges alike.

**Merge:** survivor's `former_doc_ids` grows; the absorbed id is `retired` with `merged_into`; `gather` carries the
retired row's `content` onto the survivor, `identified` until `acquire` regroups. **Split:** old id `retired`,
`merged_into` NULL; parts list it in `former_doc_ids`. **Retry state:** role entries keyed `(source_id,
content_role)`, so a merge unions without summing attempts and a split gives each part its members' entries. **Id
continuity** (C32): v3 `source_id`s kept; collectors look up `(channel, external_id)` before minting. **Duplicates**
(C29): equal `raw.sha256` listed in `content.same_raw_doc_ids[]` for a human `merge` (v3 merged automatically,
`workflows/fetch/http/main.py:767-792`).

## 7. Versions, run records, fold, build, release, verify, recovery

### 7.1 Full copy-on-write vs part files

Defined once here. A **full file** holds the complete chain view; a **part file** only the rows whose owned block
changed, each row complete, never a delta; a **version** is the newest full file plus the ordered part files after it.
Full stays valid for any phase.

### 7.2 Files, headers, the base chain

> **Stale (checked 2026-10-14):** a chain is the published files of one series directory in name order, not a family's files linked by `base_file_id`; the header keys are `kind`, `base` (a name in the directory), `after`, `series`, `comment`, and a full may have no base.
> Current: `docs/OPERATE.md` "Data files: series and chains", `stack/store/layout.py` `HEADER_KEYS`, D372.

A chain (`stack.chain_id`) is the files of one family. **Every file names exactly one base**: a part file the head it
extends, a full file the head it supersedes, a chain's first file `none`; a base is a same-chain file, never a
`corpus`. Header: `version_kind`, `base_file_id`, `chain_id`, `ids_digest`, `schema_digest`, `family_version`. **Every file's
schema digest is its declared format version's** (C1 as amended by D295; was "one schema digest per chain", which
forced a full file on every bump): a chain may mix versions of its family (`stack/schemas/upcasters.py`), and the
reader reads each file through `read_family`, upcast to the current version; duckdb only windows over the key
columns, which never change across versions (versions F1, future developer F2 ran: duckdb over two schemas silently
dropped new fields, which is why rows never come back through duckdb). A file newer than the code, of an unknown
format or behind an `Incompatible` step is refused. A full file drops an id only by
tombstone (X22); omission from a part file means unchanged.

### 7.3 The reader

> **Stale (checked 2026-10-14):** the reader resolves from run records, not headers: the head is the directory's last published file, the walk back to the newest full checks each file's `after` against its predecessor (`ForkError`, `MissedFileError`), and nothing is gc-able; the union SQL still holds.
> Current: `stack/store/chain.py` (module docstring, `_walk`, `read_view`), D345, D372.

`stack/store/chain.py` reads headers only, finds the head (the file no one names), **walks back** to the newest full file
(C4), checks linearity from there, and unions
by chain position, never by file name or time:

```sql
SELECT * EXCLUDE (filename, pos) FROM (
  SELECT d.*, c.pos FROM read_parquet($paths, filename := true) d JOIN chain c ON d.filename = c.path)
QUALIFY row_number() OVER (PARTITION BY doc_id ORDER BY pos DESC) = 1;
```

A full file's base is a fork key, not a required file, so older history is gc-able (versions F4 ran). B2 mock (ran):
by name 500 of 3,000 rows wrong, by chain none. Who unions: chain phases, `fold.corpus`, `fold.compact`, verify;
never consumers, `annotate.<wf>`, `fold.build`, `fold.release`.

### 7.4 Compaction as a fold

`fold.compact`: union-and-select written full, no filter or join, base = the superseded head; rows keep `created_at`,
row and block stamps; the compaction's run id is in the header only; tombstones survive. Rule: `max_partials: 12` or
part rows above `max_partial_rows_ratio` × full rows. B2 mock (ran): C1 equals the union, stamps kept, chain `[C1, S4]`
afterwards. Rarely needed under full gather.

### 7.5 Linearity

> **Stale (checked 2026-10-14):** files no longer each name a base: linearity is each file's `after` equal to its predecessor in name order, from the newest full on, and a full without a base is legal. Current: `stack/store/chain.py` `_walk`, V11 in `stack/checks/chain.py`, D372.

As the brief states it: **within one file an id appears at most once; every file names exactly one base; no two files
claim the same base.** Plus: bases exist from the newest full file on; the head reaches a full file; `ids_digest` and
`schema_digest` recompute. `V-chain-linear` runs before publish and at build; the fork check counts full files (C2).

### 7.6 Failure cases

> **Stale (checked 2026-10-14):** `claim_base` and base-naming headers are gone: concurrent writers meet the publish rule (`failed[head_moved]`, `failed[name_order]`) and the reader's `after` check (`ForkError`, `MissedFileError`); a refused output stays as `data/<family>/<series>/.<name>.tmp`.
> Current: `docs/OPERATE.md` "Two writers, forks and their repair" and "What a crash leaves, and why it is safe", D372.

| Case | Detected by | Recovery |
|---|---|---|
| missed part file | `MissedFileError` naming the gap | restore, or `stack chain orphan` and re-run from the last good head; never skip |
| fork | `ForkError` naming both files | orphan one; no adopt |
| two concurrent runs | `claim_base(expected_head)` against a refreshed index before publish → `HeadMovedError` (B2 mock ran); no lock (D355): two writers can both pass it, so the fork check is the backstop (`ForkError`, V11, X23) | refused output stays as `data/.<name>.tmp` (garbage); re-run. Discipline: one writing container per store |
| a run that died | its marker goes stale (dead after 5 min); nothing a record names is published (a file renamed before the final record is garbage, X1 warns) | re-run; earlier phases stay valid |
| a lost tail part file | `head_descends` (versions F3 ran) | restore, or re-run the phase |
| a late part file on a superseded head | fork with the full file on the same base (versions F2 ran: B3's NULL base lost 300 rows) | orphan it; re-run |

### 7.7 Sizes

Order of magnitude, `07-db-facts` (DB not current), abstracts priced (C36; versions F5-F8), superseding E13.

| Item | Size |
|---|---|
| `documents` row, compressed | ~1.0-1.5 kB; ~0.5 kB without abstract |
| `sources` full | ≈ 60 MB (245k × ~260 B) |
| full `documents` file | ≈ 230-330 MB |
| `corpus` (182k rows) | ≈ 200-270 MB |
| per build, defaults | ≈ 0.3 + 0.015 + 4 × 0.005 + 0.45 ≈ 0.8 GB |
| per year, weekly | ≈ 40 GB, all kept (no data gc, D356); all-full ≈ 100 GB |
| text blobs | ≈ 0.4 GB once |
| raw blobs | 0.7 GB; ≈ 25.6 GB if the 58k gated documents were fetched (hypothetical) |
| touched per sweep | 1-6%; a page recheck part file is the whole recheck set (304s stamp `last_attempt_at`) |

B3's 8 GB/yr and 3 GB are over 2x low (50k-row corpus assumed).

### 7.8 Recommended default per phase

> **Stale (checked 2026-10-14):** `<wf>` files are always full (no part option), and a file's kind is set per run or plan line (`--part`, `out: {kind: part}`), not per phase. Current: `docs/OPERATE.md` "Runs stand alone" (Outputs), D14, D378.

| Phase | Default | Evidence |
|---|---|---|
| `collect.*` | part, never compacted | the chain is the observation history |
| `gather` | **full** | a legal base; chains ≤ 6 files; Astra versions (no abstracts): all-full 48.9 GB/yr, full gather 17.7, part gather 13.1, corpora ~10.7; C36: part gather saves ≈ 15 GB/yr at the cost of tombstones and longer chains |
| `triage` | part | only rows reset by a digest change |
| `acquire` (≤ 4) | part | a 1,988-row retry ≈ 1 MB against a 230-330 MB full file |
| `fold.*` | full | consumers never union |
| `annotate.<wf>` | full, part optional | prior-based selection |
| `fold.compact` | by rule | rare under full gather |

B2 mock ratio 0.573 (understated: every row touched in S1). Dissent: B2's part gather; settled by the weekly
touched-row rate (§10.2).

### 7.9 Run records, build record, verify, recovery

> **Stale (checked 2026-10-14):** the verb list (`latest`, no `head`), `claim_base` in the publish step and the `--prior` ancestry of dead-run charging no longer hold (`dead_run_charge: true` is refused).
> Current: `stack --help`; `docs/OPERATE.md` "Jobs: record, log, marker and tick", "Recovery", "`stack store push` and `--check`"; D1, D372, D375.

Run records (`…/<head>.<stage>.json.zst`, D344) gain `version` and `fold`; the build record gains `version_chain`,
`files.corpus_input`, `files.corpus_everything?` (A.7). Verbs: `run`, `build`, `verify`, `ls`, `status`, `latest`,
`diff`, `release`, `fold`, `chain`, `eval`, `migrate`, `acquire`, `overrides`, `store` (`stack --help`).

**Jobs and upload** (D352-D358). A build or run writes its `running` record, log and marker at start (PUT before any
work when it uploads), rewrites and re-PUTs them every 30 s, publishes (`claim_base`, rename, data PUTs), then
writes the final record locally and PUTs json and log and deletes the marker. A job whose ticks fail for 2.5 min
cancels itself (`cancelled[tick_failed]`) before publishing. A dead job (marker older than 5 min) is never
finalised; its last json is its state. At the start of every uploading job, after the index refresh and before the
pins (D353), recovery pushes each local `upload: true` final record whose json S3 lacks or whose marker S3 still
holds, in publish order (data, json, log, marker DELETE); `stack store push <job>` does the same for a finished
no-upload job, refusing one whose inputs are not on S3 or whose base moved, and `stack store push --check` lists
every local record S3 lacks or holds with another status.

**Recovery without a journal** (D1 open): a parent death publishes nothing;
convert runs in an RLIMIT child (`stack/conversion/worker.py`) charged `failed_transient[crashed]`;
default `dead_run_charge: false` with the `repeated_inflight` warning (X11); the switch charges dead runs' inflight
keys along `--prior` ancestry. A dead run cannot leave a part file.

### 7.10 What ran

- **B2 partials mock** the v4 design mock `v4f-B2-partials`
  (pyarrow 24.0.0, duckdb 1.5.6): order by chain vs name, compaction, the three errors, ratio 0.573.
- **B3 fold mock** the v4 design mock `v4f-B3-fold-annotations`:
  five fold steps; `has_text` 2 of 6, `passed_triage` 3 of 6, `everything` 6 of 6; typed and JSON annotations;
  `withheld`; the three queries under B3's names.
- **Critics' probes** (`review/`): versions F1-F4, future developer F2, operator F1, consumer F1, F2, F4, F6, channels
  F1, F2, F5, F6, semantics F6, the B1 ownership probe; cited where their rules land.

## 8. Growth

**A new channel:** a collector with its own `sources` chain and a `channels.yaml` entry; no schema change unless a new
role. **A new consumer:** a `gate:` clause, stage lines, a `fold.build.filter` clause. **A new annotation workflow:** a
registry entry makes it typed: a new format version of `documents`/`corpus` (an `ADDITIVE` step) and version 1 of its
own families, read upcast with no full write (§7.2, D295); before it has an entry it folds into `other[]` and is
dropped from releases.

## 9. Census check and v3 import

| v3 datum or behaviour (file:line, verified by) | v4f | Lost or at risk |
|---|---|---|
| provider record history, append-only per `(source, workflow, version)` (`0001:124-134`; `identity.py:89-93`; v3 developer) | `sources` chains, part files, never compacted by default; import one observation per v3 `metadata` row | none |
| `metadata_canon` live view (`0004:6-28`; original `0002:31-36`), `EffectiveMeta` (`read.py:205`) | `gather.merge`, `field_sources[]`, `empty_is_absent`, author union | none designed; argued |
| `fetch_log`, one row per attempt (`0001:236-243`) | `content.roles[]` T2, run counters | per-attempt `detail` (accepted loss) |
| `snapshots`, `seq`, `snapshots_latest` (newest `seq` even without text, `0002:21`) | `content.snapshots[]`, pointer rule | none; the pointer differs by design |
| `convert_state` | `snapshots[].convert_attempts` (cap 3), `text_status`, `convert_pin` | a blob shared by two documents converts twice (accepted) |
| receipts and annotations (`types/payloads.py:90-105`) | `<wf>_evidence` with original keys (`ann_id`, `snapshot_id`, `workflow_version`, `n_records, outcome, attempts`) | none |
| `duplicate_of`, `mark_duplicate` (`writer.py:2016`); content dedup (`fetch/http/main.py:767-792`) | `gather.union`; `same_raw_doc_ids[]` for a human merge; `duplicate_info.method ∈ {content, election}` → an `identity` override | automatic content merge becomes human (C29) |
| masks (`0001:197-204`), reviews (`0001:208-216`) | `identity.is_masked`, overrides; `origin = 'human'` | none |
| `corpus` view (`0006:2-30`), `corpus_dropped` | `fold.corpus` `has_text`; `excluded_reason()`; v3 membership kept as a comparison set | per-row reason column |
| in-band mint (`inband.py:102-160`, rule `:144-151`; `store_inband_text` → `Writer.import_snapshot`, `inband.py:105, 153`; `collect-inband@1`, `:158`) | `collect` mints, `gather` lists, `acquire` snapshots | none |
| writer bookkeeping (`writer.py:834, 849`); advisory lock (`writer.py:664-675`, verified) | run records; `claim_base` [now: the publish rule, D372], no lock (D355) | concurrent writers on one store: one-writer discipline, forks reported |
| `0007_affiliation_history.sql`, `model_roles` | `other[]` JSON at import | **unverified**: absent from 9312c8c |

**v3 import** (one-off, C34; the first full `documents` file is the import):

| v3 source | Through | Into | Accepted loss |
|---|---|---|---|
| `metadata` rows | raw table, not `metadata_canon` | `collect.v3_import` → `sources` part, one row per row, key `v3_meta_id = meta_id` (D524), `origin`, `run_id` | none |
| `sources`, `duplicate_of`, masks | `sources_live` | `gather` → `documents.identity`; v3 `source_id` kept | none |
| `snapshots`, `fetch_state`, `convert_state` | `snapshots_latest` for the pointer check | `content.snapshots[]`; `fetch_state` split per role by lane; `roles[]` | `fetch_log` detail |
| `fetch_log` `dead`/`no_fetchable_url` | latest per source | role `skipped`/`failed_permanent[dead]` | per-attempt rows |
| annotations, receipts | `annotations_canon` for selection | `<wf>_evidence` (all), `<wf>` (selected); non-current-snapshot verdicts in evidence only; `empty` receipts = done | `n_records` per unit |
| `model_mentions`, `model_roles`, `md_quality` | raw | `other[]` JSON | typed access until registered |
| `corpus` view | `0006:2-30` | a comparison set in the import run record | none |

## 10. Open questions and dissent

> **Note (checked 2026-10-14):** §10 is the design-process record of 2026-10-12 (open questions, dissent, counts, decisions and choices as then posed); the outcomes are rows of `docs/DECISIONS.md`.

### 10.1 What ran, what is argued

Ran: §7.10 only. Everything else is argued.

### 10.2 Open, ordered by what bites first

1. **Gather full vs part** (measure the weekly touched-row rate).
2. **D1** `dead_run_charge`.
3. **`O_EXCL` + rename** on a network filesystem (measure).
4. **Abstracts** inline (~+200 MB per full file) vs an `inband` entry.
5. **LW/AF shared id** at runtime (one DB count).
6. **Item grain**: `payload.items[]` vs a list of `ANN` (B2).

### 10.3 Dissent

| Joint | Positions | Standing |
|---|---|---|
| `acquire_pending`/`acquire_failed` as states | B1, Sol semantics, Sol channels, Astra future developer: states | derived macros; a state flipping per retry multiplies part rows; an edit with conversion pending would contradict `acquired = valid text` (Sol operator F2) |
| gather default | B2: part after bootstrap | full (Astra versions numbers) |
| incidents and OSINT | Sol channels: a record path outside `documents` | rendered `documents` rows, role `fields` |
| "why excluded" | Astra consumer: a per-row column | a query over an optional diagnostic fold |
| `inband` block | Sol full-set: the brief said five blocks | six |
| X unit | channels critic: self-thread documents | one post per document (producers report) |
| dead-run charging (from v4e) | v3 developer, operator, Sol: R3's charging rule; Astra: telemetry only | default off, D1 |
| boundary criterion (from v4e) | Astra: merge where substage reruns keep cost and failure isolation | Note 2's criterion kept |

### 10.4 Counts v4e → v4f

Families 12 → 6; blocks 0 → 6; states 0 → 4 (+3 for `sources`); checks 7 S + 10 V + 16 X = 33 → 10 S + 12 V + 25 X =
**47**; verbs 9 → 11; plan steps 7 + verify + record → 9 + verify + record (three folds count).

### 10.5 The eight decisions of 2026-10-12

| Decision | Honoured in |
|---|---|
| 1. one `documents` family, state, invariants | §2.3, §2.5, App. B |
| 2. blocks, written by owner, rest carried | §2.4, X15 (`inband` added, §10.3) |
| 3. `sources` separate, shared field vocabulary, merge over members | §5.2, §5.3 |
| 4. `corpus` = `documents` schema by fold; prior always `documents` | §3.1, §3.2, §3.3, C2 base rule |
| 5. annotations beside, folded at build; common fields + payload; triage in the block | §3.2, A.4 |
| 6. fold one operation; build ≈ release | §3.2 |
| 7. names `sources`, `documents`, `corpus`; no `metadata` family | §2.3 |
| 8. minimal families and stores | §0, §2.3 |

### 10.6 Discussion record

Full table in `comparison.md` §7.

| Item | Answer | Section |
|---|---|---|
| 1. post-acquire gather → corpus; content in documents; specific name; `sources`; next sweep from prev corpus | `fold.corpus`; `content` block; `documents`; `sources` = collectors' family; next sweep reads the `documents` head (retries in `roles[]`) | §3.1, §3.2, §2.3 |
| 2. prev documents, not corpus; minimal outputs; same family; fold filters | enforced by head pin and base rule; six families; `has_text`, `passed_triage`, `everything` | §3.3, §2.3, §3.2 |
| 3. annotations beside; per-state invariants; sources alike; generic fold | `<wf>` folded at build; state table, checks; shared field vocabulary; one `stack/fold/` package | §2.5, §5.2, §3.2 |
| 4. fixed fields + payload, JSON if unknown; build ≈ release | `ANN<P>`, `ANN<JSON>`; release = build + config | A.4, §3.2 |
| partial versions and clarification | part files, base chain, position reader, compaction as fold, linearity, failures, sizes, defaults; complete rows; stamps per row and block (C6) | §7, §2.4 |

### 10.7 Decisions left to the maintainers

(1) `inband` block; (2) `error_class`, `is_basis_current` in the ANN common fields; (3) gather full vs part; (4)
incidents as documents vs records; (5) abstracts inline vs blob, and `keep` only for `open` in releases; (6) "why
excluded" as a query; (7) X one post per document; (8) D1.

### 10.8 Choices made in writing

1. C8 drops row `status`; E2-E4, E7 filters rewritten with `NOT identity.is_masked`.
2. `sources` keeps T1 `status, error_class` beside `state`.
3. 47 checks, not E19's 43 (C2, C3, C8, C29 add four); `evidence_declared` folded into V-evidence.
4. Roles `readme`, `archive` added; `readme` ranks between `page` and `abstract`.
5. `as_of` in the pointer order before `captured_at`.
6. The two macro definitions are mine.
7. Verbs 11 (`fold`, `chain`).
8. Sizes from C36, superseding E12/E13.
9. About 500 words over 9,000 (`wc -w` counts ~1,250 table pipes): C14 self-containment and the five required tables.

## 11. Glossary

### 11a. v3 → v4

Kind: T table, V view, F function, W workflow, C column or concept. `0001` = `db/0001_init.sql`, `0002` =
`db/0002_views.sql`, wy = `configs/workflows.yaml`; v = verified in the code copy, c = census, u = unverified.

| v3 | Kind | file:line | v4f place |
|---|---|---|---|
| `sources` | T | `0001:23` v | `sources` (observations), `identity.members[]`; ids kept |
| `metadata` | T | `0001:124-134` v | `sources.metadata` (+ the `v3_meta_id` key, D524); not a family |
| `metadata_canon`, `EffectiveMeta` | V, C | `0004:6-28`; `read.py:205` v | `gather.merge`, `metadata`, `field_sources[]` |
| `snapshots`, `snapshots_latest` | T, V | `0001:89-123`; `0002:21-27` v | `content.snapshots[]`, `current_*` |
| `convert_state`, `fetch_state` | C | `0001:117`, `0001:36-39` v | `snapshots[].convert_attempts, text_status`; `content.roles[]` T2 |
| `fetch_log` | T | `0001:236-243` v | dropped: `roles[].last_outcome`, run counters |
| `annotations`, receipts | T, C | `0001:138`; `types/payloads.py:90-105` v | `<wf>_evidence` |
| `annotations_canon`, `annotations_live` | V | `0002:38-43, 116` v | `<wf>`; `annotations.<wf>` at `fold.build` |
| `pending_units()` | F | `0002:130-141` v | the `diff` substage |
| `aggregates*` | T, V | `0001:172` v | dropped: a fold plus a query |
| `masks*`, `reviews` | T, V | `0001:197-204, 208-216` v | `identity.is_masked`, overrides; `origin = 'human'` |
| `coverage` | T | `0001:220` v | `coverage` family |
| `llm_calls` | T | `0001:248` v | `cache/llm-responses/`, evidence rows |
| `corpus` | V | `0006:2-30` v | `fold.corpus` `has_text` (v3's referent kept) |
| `corpus_dropped` | V | `0002:85-97` v | `excluded_reason()` over `corpus_everything` |
| `duplicate_of`, `mark_duplicate` | C, F | `0001:47`; `writer.py:2016` v | `gather.union`; `same_raw_doc_ids[]` |
| `external_id`, `hard_key` | C | `writer.py:925-940` v | `external_id`, `keys[]` per group |
| `JournalRequest` | C | `provider_transport.py:251` v | `cache/provider-responses/` |
| `collect.*` | W | wy:23-127 v | `collect.<channel>` writing `sources` |
| `fetch.*` (`metadata`, `abstract`, `history`, `http/oa/arxiv`) | W | wy:157-222 v | lookups; `acquire` substages, lanes |
| `convert.docling` | W | wy:229 v | `acquire.convert` |
| `annotate.screen@3` | W | wy:277-286 v | gate → `triage`; verdict → `annotate.screen` |
| `affiliation`, `card_dates` | W | wy:267-347, 527 v | `annotate.affiliation`; `collect.card-dates-lookup` |
| `v1_import`, `recall_check` | W | c | collectors (`recall_check`: `coverage` only) |
| `md_quality`, `model_mentions`, `model_roles` | W | c | `other[]` JSON until registered (u) |
| `workflow_version` | C | c | `ANN.version`, `config_digest` |
| advisory lock | C | `writer.py:664-675` v | `claim_base` per chain [now: the publish rule per series directory, D372]; no lock (D355) |

### 11b. v4 terms

Terms defined in §2.7 (phase, stage, run, full and part file, version, base, head, chain view, observation, member,
canonical identity, current revision and text, `corpus_input`) are not repeated. **block** a struct column owned by
one phase, or the flat state block; **state** §2.5; **compaction** `fold.compact`; **fold** §3.2;
**union-and-select** §7.3; **tombstone** a `retired` row; **in-band content** a body delivered at first contact;
**ANN common fields** A.4; **`other[]`** unregistered annotations as JSON; **release config** §3.2; **work key** `H([source_id, basis_digest, config_digest,
item_key])`; **accepted configs** a plan line's `target` and `accepted:` list (D758, `docs/CONCEPTS.md` "Accepted configs"): decided
answers of the listed earlier configs stand for the target's, labelled with their own config; selection
(`stack/annotate/select.py`), the ask loops and triage's carried answers (`stack/gate.py`) consult it, X6 and X8 replay
the run's recorded `selection`; the **selection digest** names a stage's target and list; **pin** a prompt, model or converter digest fixed by the plan [now: the convert pin also hashes the installed converter tool versions, `workflows/acquire/convert.py` "The pin"]; **lane** `acquire`'s fetch route; **via**
how a snapshot was obtained; **origin** who produced a value; **tier** T0, T1, T2 (A.1); **`scope_complete`** no
`not_attempted` row in scope [now: per stage; in `acquire` a role with no lane stays `not_attempted` without making the
scope incomplete, while a stop, a halt or a fetch that did not run does, `workflows/acquire/main.py` docstring].

## 12. Code layout

> **Stale (checked 2026-10-14):** `stack/store/chain.py` has `resolve`, `read_view`, `stage_file`, `claim`, `publish`, `write_file`, `orphan` (no `write_full`, `write_part`, `claim_base`); the verb list names `latest`, not `head`; files such as `stack/store/parquet.py`, `stack/checks/rows.py` are unlisted.
> Current: the tree and `stack --help`, D372, D375.
>
> **Stale (checked 2026-11-02):** the tree has grown since 2026-10-13 (e.g. `stack/fold/dataset.py`, the annotate stages `technique_staged`, `translation`, `affiliation_orgs`, `tools/scheduled_run.py`), and the verbs include `head`, `stop` and `show`. The files this section names as gone or retired (`cli/latest.py`, `core/db.py`, `stack/cli/{gc,blobs,archive}.py`) are absent.

Delta from v4e §12 (E20). v3 paths keep their names where the job survives; `db/`, `core/db.py`, `core/writer.py`
retire.

| Path | Fate |
|---|---|
| `stack/schemas/{metadata,docs,content,text,triage,triage_evidence,screen_corpus}.py`, `workflows/annotate/screen/corpus.py`, the alias in `cli/latest.py` | gone |
| `stack/store/chain.py` | new: resolve (from run records, D345), union-and-select, `write_full`, `write_part`, `claim_base`, `publish`, `orphan` |
| `stack/fold/` (`__init__`, `configs`, `release`, `stage`) | new: the five fold steps, four configs (`configs.py`), the release writer, the stage adapter |
| `stack/schemas/blocks.py`, `stack/schemas/families/{sources,coverage,documents,wf,evidence}.py` | new (`corpus` resolves to the `documents` schema in `families/__init__.py`) |
| `stack/schemas/payloads/registry.py`, `payloads/{triage,screen,affiliation}.py` | new |
| `stack/states.py`, `stack/checks/{chain,blocks}.py`, `stack/query.py` macros | new |
| `stack/cli/{fold,chain}.py` | new verbs |
| `workflows/triage/main.py`, `workflows/gather/carry.py`, `workflows/collect/v3_import/main.py` | new |
| `configs/releases/public-v1.yaml`, `configs/plans/std-v2.yaml` | new |

```
stack/
  store/chain.py  fold/{__init__,configs,release,stage}.py  states.py  query.py
  schemas/blocks.py  schemas/families/{sources,coverage,documents,wf,evidence}.py
  schemas/payloads/{registry,triage,screen,affiliation}.py
  checks/{schema,family,verify,chain,blocks}.py
  cli/{fold,chain}.py
workflows/
  collect/v3_import/main.py  gather/carry.py  triage/main.py
configs/
  releases/public-v1.yaml  plans/std-v2.yaml
```

Implemented since, beyond this delta (tree as of 2026-10-13): `stack/{stage_runner,plan,runs,
build,migrate,urls,gate,rules,annotations,overrides}.py`; `stack/{annotate,collect,conversion,evals,llm,lookup}/`;
`stack/cli/` (verbs `run, fold, verify, latest, status, ls, release, build, diff, chain, eval, migrate, acquire,
overrides, store`); the store (D320-D366): `stack/store/{layout,remote,index,job,blobs,backend,s3config}.py`
(names, the mirror and its remote, the record index and liveness, jobs with ticks, upload, recovery and push, flat
blobs), `stack/cli/store.py` (`store push`); removed with it: `stack/cli/gc.py`, `stack/cli/blobs.py`,
`stack/cli/archive.py`, `stack/v3archive.py`;
`stack/schemas/payloads/{doc_class,relevance,usability}.py`; `stack/checks/{annotate,build_checks,content,digests,
fold_checks,lookup,triage}.py`; `workflows/acquire/`, `workflows/gather/{anchor,merge,normalise,overrides,union}.py`,
`workflows/annotate/{screen,affiliation}/`, the collectors under `workflows/collect/`; `configs/plans/{dev-v2,
dev-v2-m4,links-scratch}.yaml`. v3 leftovers were removed (`README.md`, "What is not in this repository").

## Appendix A. Schemas

Patched to the code on 2026-10-13 (T-DOCS, from the pyarrow schemas in `stack/schemas/` and real files); where they
differ later, `stack/schemas/` wins.

**A.1 Notation and tiers.** `S` string, `B` bool, `I` int32, `L` int64, `D` float64, `TS` timestamp(us, UTC),
`LS(t)` list, `ST(…)` struct; `!` non-nullable (S2; pyarrow does not enforce it on write). Tiers: **T0** `status!
S, run_id! S`; **T1** T0 + `error_class S, error S`; **T2** T1 + `attempts! I, first_attempt_at TS, last_attempt_at
TS, retry_after TS`. **EVI** (evidence common columns) = `evidence_id! S, work_digest! S, source_id! S, basis_kind! S,
basis_digest! S, config_digest! S, item_key! S, doc_id! S, origin! S, decided_at! TS, supersedes S, is_cache_hit! B,
template_digest S, model_digest S, prompt_name S, rendered_digest S, model S, response_digest S, cost_usd D, tokens_in
I, tokens_out I, latency_ms I, result_digest S`. **SEL** (selection common columns of a `<wf>` file) = `doc_id! S,
item_key! S, evidence_id S, source_id S, override_ids! LS(S), override_stale_ids! LS(S), result_digest S`.

**A.2 Structs.** `AUTHOR = (name! S, orcid S, affiliation_raw S)`; `KEY = (kind! S, value! S)`; `KEYSRC = KEY +
source_id! S`. `MEMBER = (source_id! S, channel! S, key_kind! S, key_value! S, canonical_url S, title S, published_at
TS, doc_type S, license_class S, is_live! B, edge_kind! S, edge_to_source_id S, override_id S, first_observed_at! TS,
last_observed_at! TS, observation_count! I, withdrawn_at TS, withdrawn_reason S)` (`inband_digests` dropped, D690:
derivable from the entries). `BLOB = (sha256! S, bytes! L, media_type! S)`, a blob reference named by its role (D690;
`bytes` is the blob's size, recorded when it is written).
`INBAND_ENTRY = (source_id! S, content_role! S, raw BLOB, text BLOB, captured_at! TS)`; `text.media_type` is the
entry's media type for a text-only entry, `text/markdown` when both are set (the X collector).
`SNAPSHOT = (snapshot_id! S, source_id! S, kind! S, content_role! S, via! S, url S, final_url S, http_status I,
etag S, last_modified S, captured_at! TS, as_of TS, revision S, raw! BLOB, text BLOB, text_status S,
text_error_class S, convert_attempts! I, convert_pin S, quality QUALITY)` (no `converted_from`: a text is a field of
its raw snapshot, D9; `text.media_type` is the raw's when the text is the raw bytes and those are `text/*`, an in-band
abstract or post, else `text/markdown`). `ROLE = (content_role! S, source_id S,
status! S, error_class S, error S, attempts! I, first_attempt_at TS, last_attempt_at TS, last_outcome S, retry_after
TS, tried_urls! LS(S), last_source_id S, etag S, last_modified S, validator_url S, snapshot_id S, lane S, rule S)` (`rule`: `arxiv_pdf` when the arXiv PDF rule set the status, NULL for
a lane's outcome; D690). `REVISION =
(revision! S, published_at TS, title S)`; `QUOTE = (text! S, offset I)`; `AFFIL = (surface! S, org_slugs! LS(S))`;
`QUESTION = (question! S, status! S, error_class S, verdict S, confidence D, evidence_id S, config_digest! S)`.
`QUALITY = (version! I, bucket! S, flags! LS(S), fails! LS(S), gate_digest! S, converter S, tier S, metrics ST(…))`, the
text quality verdict (D204; `bucket` ∈ `ok, flagged, failed`; `tier` the ladder tier of the converter stamp
`<tool>@<version>+tier<k>`, NULL without one (`inband`), which the D661 garbled-PDF retry reads (≠ `3`), D690; the 31 metric fields are in `stack/schemas/structs.py`).

**A.3 Blocks of `documents` (= `corpus`).** Column order is file order.

| Block | Columns |
|---|---|
| state (flat) | `doc_id! S, state! S, run_id! S, created_at! TS, updated_at! TS` |
| `identity` | `ST(anchor_source_id! S, members! LS(MEMBER), member_ids! LS(S), keys! LS(KEYSRC), channels! LS(S), former_doc_ids! LS(S), merged_into S, is_masked! B, mask_reason S, override_ids! LS(S), members_digest! S, run_id! S, updated_at! TS)` |
| `metadata` | `ST(title S, abstract S, authors LS(AUTHOR), published_at TS, provider_updated_at TS, doc_type S, is_withdrawn! B, sensitivity! LS(S), field_sources! LS(ST(field! S, source_id! S, origin! S)), fields_digest! S, run_id! S, updated_at! TS)` |
| `inband` | `ST(entries! LS(INBAND_ENTRY), run_id! S, updated_at! TS)` |
| `content` | `ST(convert_pin S, snapshots! LS(SNAPSHOT), roles! LS(ROLE), current_snapshot_id S, current_source_id S, current_role S, current_text_sha256 S, current_text_status! S, current_text_media S, current_text_quality S, current_revision S, revisions LS(REVISION), members_digest! S, same_raw_doc_ids! LS(S), run_id! S, updated_at! TS)`; NULL until `acquire` |
| `annotations` | `ST(triage ANN<TRIAGE>, screen ANN<SCREEN>, affiliation ANN<AFFILIATION>, doc_class ANN<DOC_CLASS>, stack_relevance ANN<RELEVANCE>, sr_relevance ANN<SR_RELEVANCE>, text_usability ANN<USABILITY>, model_roles ANN<MODEL_ROLES>, model_regexp_matches ANN<MODEL_REGEXP_MATCHES>, technique_roles ANN<TECHNIQUE_ROLES>, other! LS(ANN<JSON>))`, one typed field per registry entry in registry order; all typed fields always present, NULL when not folded |

**A.4 Annotations.** **ANN common fields** (exact; frozen after adoption): `workflow! S, version! I, status! S,
error_class S, basis_kind! S, basis_digest S, config_digest! S, decided_at! TS, evidence_id S, origin! S,
is_basis_current B`. `ANN<P> = ST(ANN common fields…, payload P)`. `ANN<JSON> = ST(ANN common fields…, payload
ST(item_key! S, payload_json! S))`, identified by `workflow` and `version`. Payloads: `TRIAGE = ST(decision! S,
wanted_by! LS(S), questions LS(QUESTION))`; `SCREEN = ST(verdict S, basis_role S, buckets LS(S), quotes LS(QUOTE),
off_topic_reason S, confidence D, reasoning S, adjudication_tier I, tier1_verdict S, tier1_model S)` (v3
`payloads.py:127-150`); `AFFILIATION = ST(items LS(ST(item_key! S, author_name S, author_surface S, byline_index I,
affiliations LS(AFFIL), orcid S, resolution S)))`; `DOC_CLASS = ST(doc_type S, self_published B, primary_lang S)`;
`MODEL_ROLES`, `TECHNIQUE_ROLES`: `stack/schemas/payloads/{model_roles,technique_roles}.py` (D266-D268;
`MODEL_MENTIONS` merged into model_roles, D591: its hits are the record `MODEL_REGEXP_MATCHES`, `model_regexp_matches`).
**Registry entry**: `{workflow, version, payload_schema, grain ∈ {document, item}, text_bearing: [paths], evidence: B,
basis_kind ∈ {fields, text, chained}, produced_by, reads}` (`produced_by` defaults to `workflow`; `reads` names earlier
entries whose results the ask consumes, non-empty exactly when `basis_kind` is `chained`, D235); e.g. screen: `text_bearing: [quotes[].text,
reasoning]`. **Annotation records** (track S, S1): an entry whose `produced_by` names another workflow is a record
that the producer's stage writes from the same invocation: document grain, the producer's `basis_kind`, its own
typed `annotations.<record>` field and `<record>` family (SEL + ANN common + payload flat + T2, key `(doc_id,
item_key)`), no evidence family of its own. The producer's `<wf>_evidence` = EVI + producer payload + one nullable
struct column per record it may produce (registry order) + T2; its `result_digest` covers the payload plus the
non-NULL record columns. Which records a run writes is the pin's (`Workflow.records(pin)`); a record row is
selected from the producer's selected evidence row (status, T2, digests, `evidence_id`, `decided_at`, `origin` as
the producer's row) and then gets its own overrides (target `<record>`, `docs/howto/overrides.md`). Registered records of `screen`, in registry order: `doc_class` `DOC_CLASS` (`screen-v12`; before round 5 a workflow
of its own, D257),
`stack_relevance` `RELEVANCE = ST(verdict S, confidence D, quotes LS(QUOTE), prompt_name S, model S, basis_role S,
adjudication_tier I, tier1_verdict S)`, `sr_relevance` `SR_RELEVANCE` (the same plus `category S, agenda S` after
`quotes`), both `text_bearing: [quotes[].text]`; `text_usability` `USABILITY = ST(verdict S ∈ {usable, degraded,
unusable}, issues LS(S), note S, prompt_name S, model S, basis_role S, adjudication_tier I)`, `text_bearing:
[note]`. `screen-v10` produces `stack_relevance`, `sr_relevance`; `screen-v11` adds `text_usability`, `screen-v12` `doc_class`;
the single-question pins none. A reader pins a
record's version by `config_digest` (the producer's config digest under the plan: `stack/annotations.py`
`pinned_digest`; duckdb `ann_ok`, `ann_verdict`, `ann_pinned`).

**A.5 Families.** [now also `overrides`: `stack/schemas/families/overrides.py`, D336]

| Family | Columns |
|---|---|
| `documents`, `corpus` | A.3 |
| `sources` | `source_id! S, state! S, record_digest! S, record_format! S, observed_at! TS` + T1 + `identity ST(source_id! S, channel! S, external_id S, canonical_url S, keys! LS(KEY))`, `metadata ST(title S, abstract S, authors LS(AUTHOR), published_at TS, provider_updated_at TS, doc_type S, is_withdrawn B, sensitivity LS(S), license_class S, origin! S, provider_version I, lookup_of_source_ids LS(S), miss_count I, query_digest S, matched_by LS(ST(query_id! S, query_digest! S, label S)))`, `inband INBAND` (`matched_by`: every query of the run that returned the source, sorted; `query_digest` = the first's; round 4 P; `inband.entries[]` may hold one `provider_record` entry, D522; format v3 dropped `metadata.record_ref`, D524) |
| `coverage` | `channel! S, query_digest! S, window_start_at! TS, window_end_at! TS, query! S, seen I, kept I, is_exhausted! B, next_cursor S, gate_digest S` + T2 |
| `<wf>` | SEL + ANN common fields as flat columns + payload fields as flat columns (item grain: one row per item) + T2 |
| `<wf>_evidence` | EVI + payload columns + one nullable `ST(<record payload>)` per annotation record of `<wf>` (A.4) + T2; `triage_evidence`: EVI + `question! S, verdict S, confidence D, reasoning S` + T2 |

**A.6 `stack.*` header** (S7: every key required): `schema_version, family, key, stage, writer, run_id, file_id,
plan, inputs, prior, only, config_digest, scope_complete, row_count, finished_at, code_digest, schema_digest,
family_version, chain_id, ids_digest, blocks_written, series, comment, kind, base, after`; on `<wf>`: `basis_kind`; on fold
outputs: `fold_config, filter, annotations_folded, release_config` (NULL where not releasing). `schema_version = "2"`:
the header carries `family_version` (the family's format version, D295); a legacy `"1"` file has none and is mapped by
its `schema_digest`.

**A.7 Run and build records.**

```
run:   {run_id, stage, writer, plan, status, error_class?, error?, started_at, heartbeat_at, finished_at?,
        code_digest, config_digest, only, inputs: {family: file_id}, prior, in_files: [{path, file_digest}],
        filter?, scope_complete, inflight, charged_from_dead_runs?, substages: {name: {ran, scope_complete}},
        refresh_pending, counts, version: {rows_touched},
        fold?: {config, filter, joined: {wf: file_id}, annotations_folded, release_config},
        outputs: {family: {file_id, path, file_digest, row_count, schema_digest, family_version,
                           chain_id, series, kind, base, after, comment}},
        prompt_pin?: {workflow, registry_version, plan_pin, prompt_name, template_digests, models, produced},
        build_id, upload, comment, note?, excludes: [file_id],
        spend: {usd_micro: {provider/model: int}, reserved_open_usd_micro, batch_ids, abandoned_batch_ids}}
       # annotate runs carry prompt_pin (D279); outputs carry the series fields of their files (D372; chain_id is
       # the family directory, base the base file's name, after the directory's last file at pin time);
       # in_files pins each input version's newest file; spend: D328, D364; no pid, host, writes (D356)
build: {build_id, plan, series, note?, plan_digest, gate_digest, status: ok|incomplete|failed|cancelled,
        started_at, finished_at, inputs_pinned_at, version_chain: [file_id], compacted_from?,
        pass?, first_pass_build_id?,
        files: {family | family/<family dir>: {file_id, path, file_digest, run_id},
                corpus: {…, schema_digest, family_version, fold}, corpus_input: {…}, corpus_everything?: {…}},
        runs: [run_id], verify: {status, checks: [{code, name, level, status, error_class, n, items}], pairs},
        require_complete, counts: {run_id: {stage, status, rows, error_class, scope_complete}}, warnings,
        overrides?, incomplete?: [{stage, run_id, reasons}], error_class?, error?, upload, comment}
       # files: this build's own outputs, the last per key; version_chain: the walk of its last documents file
       # (D373); no prior build
release.json: {build_id, tag, config_digest, config, dropped: {reason: n}, corpus_file_id, run_id, row_count,
               schema_digest,
               # D647 (a release without release_format 2 is superseded, not read):
               release_format: 2, file_id: <run_id>.release, audience, filter (resolved SQL), filter_checkable,
               source: {file_id, path, family_version}, rows_in, filtered_out, classes: {licence_class: rows},
               content: {snapshots, select, place, provider_records, missing_policy, max_total_bytes, elements,
                         inline_count, inline_bytes, blobs_count, blobs_bytes, records, missing}}
```

## Appendix B. Checks

S: the writer refuses the file; V: per family and row; X: verify over a build's set (X11 warn, all else hard). Cited by
name elsewhere; failing examples argued unless marked.

| # | Check | Rule | Failing example |
|---|---|---|---|
| S1 | `schema_equal` | schema = registry | `attempts` int64 |
| S2 | `nonnull` | no NULL in `!` | NULL `current_text_status` in a `content` block |
| S3 | `key_unique` | key unique, non-NULL | `doc_id` twice in a part file |
| S4 | `status_pairing` | `error_class` NULL for `ok`/`empty`, required and listed for `skipped`, `failed_*`, `quarantined` | `ok` with `dead` |
| S5 | `nested_unique` | roles per `(source_id, content_role)`, snapshots, members, evidence unique | a role twice |
| S6 | `id_formats` | 64-hex digests, `sha256`, `*_sha256` and `convert_pin`; 16-hex derived, 12-char minted | `raw.sha256 = 'abc'` |
| S7 | `kv_complete` | every A.6 key; `kind` is `full` or `part`, a part has a `base`, a `base` equals `after`, an always-full family's file is full; digests, row count match | a part file without a base |
| S8 | `state_blocks` | required blocks of §2.5 non-NULL | `triaged` without `annotations.triage` |
| S9 | `digest_agreements` | `triaged`: basis = `fields_digest`; `acquired`: `members_digest` equal | `acquired` after a member joined |
| S10 | `payload_typed` | a workflow typed or in `other[]`, not both | `screen` in both |
| V1 | `V-sources` | miss ⇒ `record_format = 'miss'`, `empty`; `origin` listed; in-band roles in `INBAND_ROLES`, a `provider_record` raw-only (D524) | a miss with `ok` |
| V2 | `V-coverage` | exhausted ⇒ no cursor | exhausted with a cursor |
| V3 | `V-state` | NULLs only where §2.5 allows; masked rows keep state | `retired` with no `merged_into`, not a split |
| V4 | `V-digests` | `fields_digest`, `members_digest`, `ids_digest` recompute | stale `fields_digest` |
| V5 | `V-annotations` | common fields complete, paired; `payload_json` parses; only `triage` in `documents` | `screen` in `documents` |
| V6 | `V-content` | pointers resolve; `ok ⇔ digest`; no wayback over a live candidate; roles per `(source_id, content_role)`; in-band converted same run | `convert_failed` with a digest |
| V7 | `V-wf` | `evidence_id` resolves; `basis_kind` constant | `ok` without evidence |
| V8 | `V-evidence` | digests recompute; evidence iff `evidence: true` | a pure workflow writing evidence |
| V9 | `V-fold` | rows satisfy the recorded filter; `annotations_folded` = non-NULL typed set; basis mismatch ⇒ `skipped[stale_basis]` | stale `ok` verdict (consumer F1 ran) |
| V10 | `V-release` | licence by supplying member; `text_bearing` caps; `other[]` dropped; withheld ⇒ no blob `sha256` anywhere; a release before D647: warn, not validated | 400-char quote (consumer F2 ran) |
| V11 | `V-chain-linear` | §7.5 [now: every series directory resolves, `after` walk, D372], plus every file's `schema_digest` its declared format version's, readable at the current one (D295) | two files on one base (B2 mock ran) |
| V12 | `V-compaction-equal` | compact = union of the replaced chain, stamps included | restamped `run_id` (B2 mock ran) |
| X1 | `finished_files` | over all `ok` records: every file one names exists locally or on S3 and has the record's `file_digest` (a download checked too); a file anywhere under `data/` (any family or series directory) that no `ok` record names is a warn-level finding (garbage; D346, D364) | an `ok` record naming a lost file |
| X2 | `lineage_equality` | each build run's pins against the store: every `in_files` path is listed by an `ok` record, not excluded, with its digest, and every pinned chained file is published; for the series directories the build wrote, a pin is on the walk of the build's last file there and a later run of the build pinned an earlier run's output or a descendant (D380); `annotate.<wf>`, `fold.build` read the build's `fold.corpus` file | `fold.build` on a newer head |
| X3 | `expected_scope` | due ∪ filtered = keys; `require_complete` | lost split-off component |
| X4 | `cardinalities` | `doc_id` once per file; a live `source_id` in one live document | a source in two documents |
| X5 | `blob_resolution` | the blob references of the build's `documents` file (without a build: the one `documents` directory `verify --in` selects) and of every `corpus` file resolve in `blobs/`, each with its recorded `bytes` equal to the store's listed size (raw and text, snapshots and in-band entries; D690); on an S3 store against one LIST of `blobs/`, so a blob only in the mirror counts as missing (D362) | text under another config |
| X6 | `policy_pins` | `template_digest` = plan pin | a dev prompt |
| X7 | `basis_currency` | `is_basis_current` recomputes | stale text marked current |
| X8 | `selection_recompute` | `select` reproduces `<wf>` | hand-edited `screen` row |
| X9 | `fold_recompute` | fold output recomputes from inputs and config | a dropped row |
| X10 | `ordering_pinned` | `version_chain` = header walk | order by file name |
| X11 | `dead_runs_seen` (warn) | `repeated_inflight`; dead = `failed[crashed]` or `running` with a stale marker (D346) | one key on two dead records |
| X12 | `state_propagation` | masked, `retired` never pass `has_text`, `passed_triage` | masked row in `corpus` |
| X13 | `gate_obeyed` | a fetched role has `fetch`, `no_basis` or `gate_failed` | fetched `skip` row |
| X14 | `gate_union` | `decision`, `wanted_by` recompute | `wanted_by` without a positive clause |
| X15 | `block_ownership` | blocks outside `blocks_written` `IS NOT DISTINCT FROM` the base view's | triage part changing `content` (B1 probe ran) |
| X16 | `lookup_gate_current` | lookup exclusion only on matching `gate_digest` | old exclusion after a new consumer |
| X17 | `created_preserved` | `created_at` constant; `updated_at` non-decreasing; untouched block `run_id` unchanged | `created_at` reset by gather |
| X18 | `tombstone_closure` | retired id no live row's id; `merged_into` exists or NULL (split); a retired anchor may be a survivor's member | `merged_into` dangling |
| X19 | `state_monotone` | forward only, except digest change and `separate` | `acquired → triaged` |
| X20 | `triage_current` | unmasked live rows triaged on the current `fields_digest`, or `identified` | `triaged` on an old basis |
| X21 | `carry_regrouped` | after the last acquire, `content.members_digest = identity.members_digest` | survivor missing retired snapshots |
| X22 | `full_covers_base` | a full file holds every base-view id except those written `retired` | stale-head full file drops 300 rows (operator F1 ran) |
| X23 | `head_descends` | per file: every selected file with a base names a published file of its own directory, so it descends from its base's version (no comparison with an earlier build; D375) | a file whose base was orphaned (a lost tail) |
| X24 | `attempts_monotone` | role `attempts` and `convert_attempts` never decrease except on `ok` or a changed basis/config | attempts reset by a stale-head file |
| X25 | `same_raw_reported` | equal `raw.sha256` documents list each other in `same_raw_doc_ids` | unreported duplicate |

Count: 10 S + 12 V + 25 X = **47** (46 hard, X11 warn; X1's garbage finding is warn-level too).
