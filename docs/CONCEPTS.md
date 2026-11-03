# Concepts: the words the operator docs assume

What each concept is, how they fit, where each lives in the code, and which doc holds its rules. Words with several
meanings (`tier`, `skipped`, `corpus`, `record`, `pin`, …) are sorted out in `docs/README.md`, "Words that mean several
things". Where this page and the code disagree, the code wins.

## The pipeline in one paragraph

Collectors observe providers and write `sources` and `coverage` rows; `gather` joins sources into documents; `triage`
decides per document whether its text is worth fetching; `acquire` fetches and converts it; `fold.corpus` selects
what the annotators read; each `annotate.<wf>` asks its question per document; `fold.build` joins the answers into
the corpus a consumer opens; `fold.release` turns one such corpus into a release. Every step is a recorded run that
reads explicit input files and publishes new immutable files.

```
collect.*    -> gather -> triage -> acquire -> fold.corpus -> annotate.<wf>  -> fold.build   -> fold.release
sources.<p>     documents (one row per document)   corpus      <wf>             corpus.build    releases/<tag>/
coverage.<p>          triage_evidence                          <wf>_evidence
```

## Store

A **store** holds everything runs read and write: a local directory, or `s3://<bucket>/<prefix>` worked on through a
local **mirror** with the same keys (`$STACK_MIRROR/<bucket>/<prefix>/`, bound by `.stack-mirror.json`), chosen by
`--store` (env `STACK_STORE`). There is no database, lock file or state directory: the records are the index. A job's
(a build's or run's) **head** is `<YYYYMMDDTHHMMZ>_<id>[_<comment>]`, its start minute. Code:
`stack/store/layout.py`, `stack/store/remote.py`. Read next: `docs/SETUP.md` §4, `docs/howto/s3-store.md`.

| Path | Holds |
|---|---|
| `data/<family>/<series>/` | the immutable parquet data files (below) |
| `builds/<head>.<plan>/` | a build's record, log and marker; its runs' records under `runs/` |
| `runs/` | records of standalone runs (`stack run`, `release`, `overrides add`, `chain orphan`, `migrate upcast`) |
| `blobs/<sha256>` | content-addressed bytes, one flat namespace: fetched bodies, converted texts, record payloads, gate JSONs |
| `releases/<tag>/`, `cache/` | releases (`release.json` written last); local-only LLM and provider response caches |

## Families

A **family** is a schema with a key (`FAMILIES` in `stack/schemas/families/`; workflows from the payload registry
`stack/schemas/payloads/registry.py`). Its files live under a **family directory**: the family name, or
`<family>.<suffix>` for per-collector and per-fold copies (`sources.arxiv`, `corpus.build`); the schema family is the
part before the dot (`stack/store/layout.py`, `family_of_dir`).

| Family | Key | Written by | A row is |
|---|---|---|---|
| `sources` (dirs `sources.<p>`) | `source_id` | `collect.*` | one provider record of one source |
| `coverage` (dirs `coverage.<p>`) | channel, query digest, window | `collect.*` | one collected query window |
| `documents` | `doc_id` | `gather`, `triage`, `acquire`, `fold.compact` | one document, carried through the phases |
| `corpus` (dirs `corpus`, `corpus.everything`, `corpus.build`) | `doc_id` | `fold.corpus`, `fold.build` | a selected `documents` row (same schema) |
| `triage_evidence` | `evidence_id` | `triage` | one triage question asked of one document |
| `<wf>`: `screen`, `translation`, `affiliation`, `affiliation_orgs`, `technique_staged`, `model_roles`, `technique_roles` | `doc_id`, `item_key` | `annotate.<wf>` | a workflow's selected answer |
| `<record>`: `doc_class`, `stack_relevance`, `sr_relevance`, `text_usability`; `model_regexp_matches` | `doc_id`, `item_key` | `annotate.screen`; `annotate.model_roles` | an annotation record (below) |
| `<wf>_evidence` | `evidence_id` | `annotate.<wf>` | one invocation of a workflow |
| `overrides` | `override_id` | `overrides.add` | one human correction, or the revoke of one |

## Series and data files

A **series** is a named line of work (`[a-z0-9][a-z0-9-]*`; the production plan `batch-2026-10a` uses `batch-import`,
the other plans in the tree their own name) with one **series directory** per family, `data/<family>/<series>/`. A
data file is
`<utc>_<run_id>[_<comment>].<kind>.parquet`: `<utc>` (`YYYYMMDDTHHMMSSZ`) is the run's start, moved past its inputs
so names sort in time order; `<kind>` is `full` (every row of the version) or `part` (only the rows changed since the
previous file). `corpus`, `<wf>`, `<record>` and evidence files are always full, as is a file directly under
`data/<family>/` (no series; read only by its exact path). The writer stage is in the record and the file's `stack.*`
footer, not in the name. Code: `stack/store/layout.py` (`DataFile`, `tie_guard`).

## Chain, head, version, publish rule

A file is **published** when an `ok` run record (below) lists it (path and digest) and no record's `excludes` (`stack
chain orphan`) takes it out; readers ignore every other file. A series directory's **chain** is its published files
in name order, its **head** the last (`stack head DIR`, `stack ls`). A **version** is the newest full file at or
before a file plus the parts after it; a reader walks back to that full and unions the files by position. Each output
records `base` (the file it extends; empty for a series' first file or a `--restart`) and `after` (the directory's
head when its run pinned its inputs). The **publish rule**, checked for every write into a series directory against a
fresh index: the head must still be the new file's `after` (else `failed[head_moved]`) and the new name must sort
after it (else `failed[name_order]`). There are no locks: two writers that both pass fork the chain, and the next
reader stops with `ForkError` (V11; repair: `docs/OPERATE.md`, "Two writers, forks and their repair"). Code:
`stack/store/chain.py`. Read next: `docs/OPERATE.md`, "Data files: series and chains".

## Stages and phases

A **stage** has the id `<phase>.<name>` or a bare `<phase>` (`collect.arxiv_sweep`, `gather`, `annotate.screen`,
`overrides.add`). Stage `a.b` is `workflows/a/b/main.py`, bare `a` is `workflows/a/main.py`, loaded by path; `fold.*`
goes to `stack/fold/stage.py`. A module exposes `run(ctx)` and optionally `SUBSTAGES` (for `--only`), `OPTIONS` (the
`options` keys it reads; others are refused) and `preflight`. The **phases**, in order, are `PHASE_ORDER` in
`stack/stage_runner.py`: `overrides`, `collect`, `gather`, `triage`, `acquire`, `fold.corpus`, `annotate`,
`fold.build`, `fold.release`, `fold.compact`. Read next: `workflows/README.md`.

## Runs

A **run** is one execution of one stage over explicit inputs: each `--in` is a series directory (its head, resolved
as a version) or an exact file; nothing is selected by plan, build or family. Outputs go to `--out`, else the
directory of the run's input of that family (its **prior**, the version it carries answers from), else
`data/<family>/<--series>/`; they are staged as `.<name>.tmp` and published only at the run's end. The **run record**
(`runs/<head>.<stage>.json.zst`, or under `builds/<build>/runs/` in a build) holds `status` (`running`, `ok`,
`failed`, `cancelled`; the last two with an `error_class`), `scope_complete` (false when a planned stop such as a
budget or `max_rows`, or a drain, left work; a drained run is `ok` with `counts["stopped.drain"]`, D570),
`in_files` (each input's path and digest), `outputs`, `counts`, `config_digest` and
`spend`. Code: `stack/stage_runner.py`, `stack/runs.py`. Read next: `docs/OPERATE.md`, "Runs stand alone".

## Plans and plan lines

A **plan** is a YAML file under `configs/plans/`, validated by `stack/plan.py` (unknown keys refused). Its main keys: `series:`
(every line's default), `stages:` (the lines), `prompts:` (template and model per LLM stage), `gate:` (triage's
clauses), `acquire:` (convert config, wanted roles, rechecks), `versions.compaction`, `fold:` (filters, folded
annotations, release config) and `require_complete:` (families whose writers must finish their scope). A **plan
line** (`StageLine`) names a `stage` and may set `options`, `budget`, `filter`, `repeat`, `prior`, `reads`, `in`,
`out` (series, kind, comment, restart) and `require_complete: false`. The full field list (also `plan`,
`schema_version`, `recovery:`, and the lookup fields `keys`, `miss_cap`): `configs/plans/README.md`.

## Builds

A **build** (`stack build <plan>`) runs the plan's lines one after another in the order listed (the loader only moves
an annotate line after the lines whose answers it reads), each an ordinary run with `--in`/`--out` paths a user could
pass by hand (`--dry-run` prints them). Each line pins its directories' heads at its own start; there is no prior
build, and the next build on the series continues from the heads. A build runs `fold.compact` first when
`documents/<series>/` is due and ends with verify. Its **build record** (`builds/<head>.<plan>/build.json.zst`) holds
`status`, `plan_digest`, `runs`, `verify` and `files` (its own outputs; `files.corpus` is the `fold.build` file).

| Status | Meaning | Exit |
|---|---|---|
| `ok` | every line ran, verify passed | 0 |
| `incomplete` | verify passed; a writer of a `require_complete` family stopped short, or a drain stopped the build (record `stopped`; `stack status`: `incomplete[stopped]`, D573) | 3 |
| `failed[<class>]` | a run failed (`head_moved`, `code_bug`, …), a line was refused (`stage_refused`, `missing_input`), or verify failed (`verify`) | 1 |
| `cancelled[<class>]` | a hard stop (`operator`: Ctrl-C, a second SIGTERM, `stack stop --now`) or its ticks ended it | 1 |

What makes a build `incomplete`, its reasons, resuming: `docs/OPERATE.md`, "`incomplete` builds and resuming"; a first
SIGTERM or `stack stop` **drains** a build (the run in flight publishes what it completed, no further run starts),
a second SIGTERM or `stack stop --now` cancels it: "Stopping, cancelling and resuming". A build into
a series with no `documents` while another series has some is refused unless `--new-series` (D491): `docs/OPERATE.md`,
"A build". Code: `stack/build.py`, `stack/cli/build.py`.

## The `documents` row: blocks and states

A `documents` row is the document as its last writer left it: flat columns (`doc_id`, `state`, `run_id`,
`created_at`, `updated_at`) and five **blocks**, each owned by one phase (`stack/schemas/blocks.py`; X15 checks that
a file changes only the blocks its footer's `blocks_written` names).

| Block | Written by | Holds |
|---|---|---|
| `identity` | `gather` | anchor, members, keys, channels, former ids, `merged_into`, masking |
| `metadata` | `gather` | title, abstract, authors, dates, `fields_digest` |
| `inband` | `gather` | bodies that came with a provider's listing |
| `content` | `acquire` | roles, snapshots, the pointer to the current text |
| `annotations` | `triage` (`.triage`); `fold.build` in a corpus | one typed field per registered workflow, plus `other[]` |

The **states** `identified`, `triaged`, `acquired` move forward only, except that gather resets a row to `identified`
when its members or fields change; `retired` is a tombstone after a merge or split (a `separate` can revive it).
Masking (`identity.is_masked`) is apart from state. Code: `stack/states.py`. Read next: DESIGN §2.4-§2.5.

## Identity

A **`source_id`** (`s` + 12 base32 characters) names one source: a collector looks `(channel, external_id)` up in the
`sources` directory it was given as its prior and mints an id only when absent (`stack/collect/base.py`,
`Collector.source_id`). `gather` joins sources that share a merging key or a matcher edge into one document
(`workflows/gather/union.py`, `stack/identity.py`); the **`doc_id`** is the anchor's `source_id`, the earliest
observed member (`workflows/gather/anchor.py`). Ids are therefore minted per series: a series that starts empty mints
new ids, collects from empty coverage, and has no prior rows or evidence for acquire and annotate (whose work key,
below, includes the `source_id`), so everything is fetched and asked again; hence the series guard
and `--new-series` (D491). The local LLM response cache is keyed by
model, rendered request and basis, not by id (`stack/llm/cache.py`).

## Triage and the gate

The plan's **gate** maps each consumer (e.g. `screen`) to clauses: `rule:` (a rule of `stack/rules.py`), `sql:` (a
predicate over the document row) or `question:` (an LLM question with its `fetch_on` verdicts). `triage` writes the
decision (`fetch`, `skip`, `pending`) into `annotations.triage` and one `triage_evidence` row per question asked;
acquire fetches only for rows triage lets fetch. Every document is asked its questions (D724), but only the
**question scope**'s answers decide (rows no `sql:`/`rule:` clause fetches and whose primary content role is not
`ok` yet); elsewhere the answer is a **signal**: the question row keeps its fill status (`skipped[not_needed]`) and
records the `verdict`, `confidence` and `evidence_id`, so a document's verdict is `questions[].verdict` whatever the
status (`stack.gate.verdict_of`). A new prompt name or model re-asks every document. Code: `stack/gate.py`,
`workflows/triage/main.py`.

## Acquire: roles, snapshots, lanes, the pointer

A **role** (`content.roles[]`, one per `(source_id, content_role)` such as `full_text` or `abstract`; wanted per
channel by the plan's `acquire.wants`) carries its fetch status, attempts and next due time. New bytes mint a
**snapshot** (`content.snapshots[]`, id `<run_id>.<n>`, kind `raw` or `inband`) whose bytes and converted text are
blobs; an equal digest mints nothing. A **lane** is a role's fetch route: `arxiv`, `oa` or `http` (`lane_for` in
`workflows/acquire/lanes/__init__.py`), or `inband` for bodies that came with a listing. The **pointer** ranks the
live members' snapshots by role, then newest, and takes the first one's text if it converted `ok` at the plan's
current convert pin (Pins, below), setting `content.current_*` (`current_text_status` `ok`, `convert_pending`,
`convert_failed` or `no_raw`; `workflows/acquire/point.py`). Acquire's substages run `carry`, `inband`, `history`,
`fetch`, `convert`, `quality`, `point` (`workflows/acquire/main.py`). Failed fetches, rate limits, the quarantine
cap: `docs/OPERATE.md`, "Failed fetches and `acquire reopen`". Its other options (`arxiv_pdf`, which arXiv PDFs
are fetched; convert caps; the run's clock): that module's docstring and `docs/OPERATE.md`, "The convert pin and the venv".

## Annotations and annotation records

An **annotator** (a "workflow" in code) is a stage `annotate.<wf>` plus its registry entry. It reads the build's
`fold.corpus` file, asks per work key (`H([source_id, basis_digest, config_digest, item_key])`), carries prior
answers whose key is unchanged, and writes `<wf>_evidence` (every invocation, cumulative) and `<wf>` (the selected
answer per `(doc_id, item_key)`). An **annotation record** is a typed result a producer's call writes beside its own:
one `screen` call also yields `doc_class`, `stack_relevance`, `sr_relevance` and `text_usability`, each its own
family and one column in the producer's evidence; the prompt pin decides which a run writes (`produced_by` in the
registry). Code: `stack/annotate/stage.py`. Read next: `docs/howto/add-annotator.md`, `docs/CONSUME.md`.

## Digests

| Digest | Covers, and so is moved by | Code |
|---|---|---|
| line digest | the line's canonical JSON without `require_complete`, `in` and `out` | `StageLine.digest` |
| plan digest | the whole validated plan (lines in resolved order, `series`, the gate with each rule's compiled SQL) | `Plan.plan_digest` |
| config digest | what a run's results depend on; in its record, its files' footers and each annotation | `ctx.config_digest` |

A run's config digest is its line digest unless the stage sets its own: `gather` adds its override rows and the provider
rank (with the `published_at` precision rule, D644); `triage` hashes the line (without the options that only steer how
it asks: `workers`, `llm_mode` and the four `batch_*` keys, `UNDIGESTED_OPTIONS` in `workflows/triage/ask.py`), the gate
digest and the asker; `annotate.<wf>` hashes the workflow, its registry version, the `prompts:` pin, prompt name,
template digests and models, not the line, so an annotate line's `budget` or `max_rows` does not move it
(`stack/annotate/stage.py`); folds hash their fold config.

## Pins

The **prompt pin** is a plan's `prompts:` entry (`annotate.<wf>`, or the key a triage question names); an annotate
run resolves it against `configs/llm.yaml` and the stage's templates and records it as `prompt_pin`, which verify's
X6 checks evidence against (`docs/howto/change-prompt-pin.md`). The **convert pin** is a sha256 over the plan's convert
config and the installed versions of its tools; `content.convert_pin` records the pin a block was pointed at, and a
changed pin re-converts every current text (`workflows/acquire/convert.py`, `convert_pin`).

## Accepted configs

Any change of a config digest re-asks every document, also when the change sends the same bytes (a vocabulary file
re-enveloped, D738) or is not retroactive. An annotate line (or the triage line, per gate question) can instead name
its **target** config (the digest its pin gives; a stage and the build refuse to run when the pin gives another) and an
**`accepted:` list** of earlier configs (D758): the decided answers judged under one of them count as current, the first
listed config with a decided answer winning, when the target config has none. Nothing is copied or relabelled: an
accepted row keeps its `evidence_id`, `config_digest`, `decided_at` and `run_id`, so it stays labelled with the config
that judged it. Failures are never accepted and a basis change is never crossed. Each entry carries a readable `name`
(checked against the stored pin: the prompt name and `@<tag>` of the vendored vocabularies it pinned), a decision row
and a note. The list is part of the plan (its digest), never of the config digest, which stays the identity of what
produces an answer; a stage's **selection digest** (over its target and its accepted list) identifies which set of
answers it ships, and is in the run record's `selection`, `release.json` `stages` and `tools/list_configs.py`. Verify
replays the run's recorded selection (X6, X8). How-to: `docs/OPERATE.md` "Keeping answers across a config change".

## Folds

A **fold** reads `documents` or one `corpus` file and writes a selection (`stack/fold/__init__.py`, `configs.py`):

| Fold | Reads | Writes |
|---|---|---|
| `fold.corpus` | a `documents` version | a `corpus` full (filter default `has_text`) that annotators read; with `fold.diagnostic`, also `corpus.everything` |
| `fold.build` | this build's `fold.corpus` file and the `<wf>` files of `annotations_folded` | a `corpus.build` full with the answers under `annotations.<wf>`: what a consumer opens |
| `fold.release` | one `corpus.build` file | `releases/<tag>/` under per-licence rules (`configs/releases/`); run by `stack release`, which accepts the files `docs/OPERATE.md`, "`incomplete` builds and resuming" names |
| `fold.compact` | a `documents` version | a `documents` full whose base is the head it replaces, so readers stop there |

## Verify

`stack verify` runs the 47 checks of DESIGN Appendix B (`CHECK_TABLE`, `stack/checks/__init__.py`): 10 `S` (what a
writer refuses), 12 `V` (per family and row), 25 `X` (over a build's files; X11 warns, the rest are hard), over the
store and the newest finished (`ok` or `incomplete`) build. The pair checks (V12, X13, X15, X17, X19, X22, X24) compare a chain file with its
base. `stack verify` checks a build: every check on the files its runs wrote and the build checks; the structural
checks (footers, records, chains, blobs over the whole store) run at every build's start and finish; `--full` runs
every check on every file.

## Overrides

**Overrides** are human corrections under `data/overrides/<series>/`, added only by `stack overrides add FILE --out
data/overrides/<series>/`; a `revoke` row retires an earlier one. Targets: `identity` and `fields` (read by
`gather`), `manual_sources` (`collect.manual`), `<wf>` (`annotate.<wf>`). A build passes the series' overrides
directory to those stages when it has a file; a by-hand run without it is refused unless `--no-overrides` (D495): `docs/howto/overrides.md`, "Who reads it".

## Spend and per-run caps

Each paid call first reserves against its run's in-memory ledger; the record's `spend` holds settled micro-USD per
`openrouter/<model>`, `x` and `openalex`, open reservations and batch ids. Caps are per run (an LLM line's
`budget.max_usd`, else `configs/llm.yaml` `budget.per_run_usd`; own caps for X and keyed OpenAlex); none spans runs
(`docs/SETUP.md` §3).

## Jobs, ticks and liveness

Every build and run is a **job** with a record (`….json.zst`), a zstd log and a marker (`…-running.json`). The record
is written `running` at the start, rewritten every **tick** (30 s) and replaced by the final record at the end; on an
S3 store each is uploaded, a run's data at its publish. A `running` record's **liveness** is its marker's age: `live`
under 5 minutes, `dead` above, `unknown` without one (`stack/store/index.py`); nothing finalises a dead job. When
ticks keep failing: `docs/OPERATE.md`, "Jobs: record, log, marker and tick". Code: `stack/store/job.py`.

## Where the rules are

| Topic | Doc, section |
|---|---|
| Install, keys, spend caps, stores, first build; tests and their tiers | `docs/SETUP.md` §1-§6; §7 |
| Commands; builds, options, passes; `--in`/`--out` | `docs/OPERATE.md`, "Commands", "A build", "Runs stand alone" |
| `incomplete` builds, resuming, what `stack release` accepts | `docs/OPERATE.md`, "`incomplete` builds and resuming" |
| Store, series, publish rule, jobs, recovery, forks, spend | `docs/OPERATE.md`, "The store, jobs and upload" |
| Verify | `docs/OPERATE.md`, "Verify"; DESIGN Appendix B |
| Convert pin, failed fetches, format changes, URL rules | `docs/OPERATE.md`, sections of those names |
| Plan fields, what a line reads and writes; writing a stage | `configs/plans/README.md`; `workflows/README.md` |
| Prompt pins, overrides, collectors, annotators, a missing document | `docs/howto/` |
| Reading a build or a release | `docs/CONSUME.md` |
| Exporting and publishing a dataset release | `docs/howto/dataset-release.md` |
