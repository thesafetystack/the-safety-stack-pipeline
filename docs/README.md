# The docs: map, references, words

v4 is the only supported code in this repository and the production pipeline: its cycle runs weekly on Railway
since 2026-10-31 (`DEPLOY-railway.md`). "v4" is the working label of the file-based design (a store of immutable
parquet files and blobs, no database) that replaced v3.

## Docs

Start with `CONCEPTS.md` after the root `README.md`; then `SETUP.md`, `OPERATE.md` and `CONSUME.md`.

| Doc | What it holds |
|---|---|
| `CONCEPTS.md` | The concepts the other docs assume: store, families, series and chains, runs and builds, plans and stages, the `documents` row, identity, roles and snapshots, pins and digests, folds, verify. |
| `SETUP.md` | Install, `.env` and keys, spend caps, stores, the offline route, a first real build, tests. |
| `OPERATE.md` | Commands; builds, overrides and passes; `incomplete` builds; standalone runs (`--in`, `--out`); the store, jobs and upload (the S3 mirror, data files in series directories and the publish rule, ticks and liveness, logs, recovery, `store push`, two writers and their repair, spend, crash leftovers, `stack status`; inspecting a document and a build with `stack show`); verify; the convert pin; failed fetches; family format changes; keeping answers across a config change (accepted configs); re-keying. |
| `CONSUME.md` | What a consumer opens; the entry query; the duckdb macros; annotation records; text quality; releases and the site query. |
| `howto/` | `inspect.md` (what a build fetched), `why-missing.md`, `full-run.md`, `s3-store.md`, `container-lifecycle.md`, `concurrency.md`, `overrides.md`, `duplicates-and-urls.md`, `add-collector.md`, `add-feed.md`, `add-annotator.md`, `change-prompt-pin.md`, `change-family-format.md`, `dataset-release.md`. |
| `DEPLOY-railway.md` | The scheduled production cycle on Railway (deployed; cycles since 2026-10-31, D780-D786): what a cycle does, the image, the services and variables, release gating, operating it (`tools/scheduled_run.py`). |
| `FUTURE.md` | Postponed work: what is built but not in a production plan, and what is not to be built. |
| `DESIGN.md` | The design (v4f): families, blocks, states, stage contracts, folds, identity, checks (App. B), schemas (App. A). Written before the implementation; its "Deltas" block and per-subsection "Stale" notes say where the code moved on, and where they disagree the code wins. |
| `DECISIONS.md` | Every decision taken while implementing, one row each, append-only: a later row amends or supersedes an earlier one. A row marked "default taken" is in force until a later row overrides it. |

Removed material (the M1-M5 plan, whose §9 is now `FUTURE.md`; implementation boards; design mocks; `HISTORY.md`):
root `README.md`, "Removed, and where it lives"; it is kept in the archived pre-publication repository.

Gitignored, local to the machine that ran them: `v4/store-dev/` (the dev store), the other `v4/store-*/` copies,
`v4/evals-store/` (eval runs), `v4/mirror/` (the local mirrors of `s3://` stores). Each run's spend is in its run record
(`SETUP.md` §3).

## The S3 store: implemented and not

Implemented (D320-D366; the series layout, D372-D375; `OPERATE.md`, "The store, jobs and
upload"): `s3://<bucket>/<prefix>` stores with a local mirror, records as the index, data files in series directories
`data/<family>/<series>/` with one publish rule and create-only data PUTs, runs that stand alone on explicit `--in`
paths, jobs with ticks, markers and logs, upload per run at publish, `--no-upload`,
recovery at an uploading job's start, `stack store push` and `--check`, self-cancel on failing ticks, flat blobs in
the store's prefix, spend in run records with per-run caps, `stack status` over the index. Not implemented: data gc
(none; D356), the arXiv PDF background lane, and any lock (one writer per store is a discipline). The
one-off migration of existing stores to the new layout is a script outside this repository. The S3 parts are tested
against an in-memory fake (`tests/fakes/s3.py`); runs against a real bucket are in the stores' run records.

Reading order for a newcomer: the root `README.md`, `CONCEPTS.md`, `SETUP.md`, `OPERATE.md`, `CONSUME.md`,
`howto/inspect.md`; for the design behind them, DESIGN §0 (one-page map), §2 (consumer interface), §3 (stage contracts)
and App. A, read with their "Stale" notes; a v3 developer starts with DESIGN §11 (v3 → v4 glossary) and §9.

## References used across the docs

- `D<n>`: a row of `DECISIONS.md`; `D<n> (X)` names the track that took it.
- `X6`, `V11`, `S7`, …: checks of DESIGN Appendix B, as `stack verify` prints them.
- Other labels in older rows and code comments (milestones `M1`-`M5` and tasks `T<n.m>` of the M1-M5 plan, `PLAN §n`,
  rounds 2-5, track letters and names such as `A` or `T-S3`, review findings such as `R<n>-<m>`, design-review rules
  `C<n>`, `E<n>`, `F<n>`) name plans, boards and reviews that are not in this repository; the sentence they annotate
  states the rule. `C#` in `DECISIONS.md` is a different list (census contradictions).
- People: Tomáš Gavenčiak (owner; decides the "default taken, for Tomáš" rows), Dan (v3 developer),
  Jake (author of the technique catalogue `taxonomy/taxonomy-v0.8.yaml`).

## Words that mean several things

| Word | Meanings |
|---|---|
| tier | converter tier 1-3 (docling / pymupdf4llm / pymupdf); screen tier 1 / tier 2 (the first call / the adjudication call on a borderline); schema tiers T0-T2 of DESIGN App. A (status column groups) |
| skipped | a record status (`skipped[no_basis]`, `skipped[no_text]`); a triage decision (`skip`); `stack build --skip`; a verify result (`skipped[no_builds]`) |
| corpus | the family name; a `fold.corpus` file (`files.corpus_input`, what annotate stages read); the `fold.build` file (`files.corpus`, what a consumer opens) |
| workflow, annotator | an annotate stage `annotate.<wf>` and its registry entry are a "workflow" in code; "annotator" in prose; `workflows/` is also the directory of every stage |
| record | an annotation record (a typed result one annotate call writes beside its workflow's, e.g. `stack_relevance` of `screen`); a run record (`builds/<build>/runs/<head>.<stage>.json.zst`, standalone `runs/<head>.<stage>.json.zst`); a build record |
| build | `stack build`; a build record (`builds/<head>.<plan>/build.json.zst`); `fold.build`, the stage that writes `files.corpus`; `build_id` |
| prior | an earlier version a stage reads to carry its answers, usually the head of the series directory it writes to (an `--in` of an output family, the plan line's `prior:`); there is no prior build |
| pin | a plan's choice of prompt and model for a stage (`prompts:`); the convert pin (config + installed tool versions); a stage pinning its input heads at start |
| lane | an acquire fetch route (`arxiv`, `oa`, `http`; `inband` for bodies that came with the listing); `via = 'archive'` snapshots came from the removed v3/v1 archive step (D329), not a lane |
| SR | the Shallow Review (`docs/scope/shallow-review.md`), whose scope the `sr_relevance` record screens for |
| chain, head, series | a chain is the published files of one series directory `data/<family>/<series>/` in name order, its head the last of them; a series is a named line of work (`weekly`, `exp-2`) with one directory per family, and it is document identity: ids are minted per series, so a new series re-pays everything (`stack build` refuses one beside a series with `documents` unless `--new-series`, D491); `corpus`, `<wf>`, `<wf>_evidence` and `triage_evidence` files are always full |
| gate | the plan's triage rules (`gate:` clauses: `rule`, `sql`, `question`) that decide which documents are fetched |
