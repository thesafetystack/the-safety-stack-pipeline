# The docs: map, references, words

Start with the root `README.md`, then `CONCEPTS.md`, `SETUP.md`, `OPERATE.md` and `CONSUME.md`; `howto/inspect.md`
shows a build from the inside. "v4" is the working name of the pipeline's file-based design (a store of immutable
parquet files and blobs, no database), which replaced v3; v1, v3 and their code are not in this repository.

## Docs

| Doc | What it holds |
|---|---|
| `CONCEPTS.md` | The concepts the other docs assume: store, families, series and chains, runs and builds, plans and stages, the `documents` row, identity, roles and snapshots, pins and digests, folds, verify. |
| `SETUP.md` | Install, `.env` and keys, spend caps, stores, the offline route, a first real build, tests. |
| `OPERATE.md` | Running builds and stages: plans, overrides and passes, `incomplete` builds, standalone runs, the store, jobs and upload, spend, verify, the convert pin, failed fetches, family format changes, keeping answers across a config change, re-keying. |
| `CONSUME.md` | Reading a build or a release: the files, the entry query, the duckdb macros, annotation records, text quality. |
| `DEPLOY-railway.md` | The production cycle on Railway: what a cycle does, the image, the services and variables, release gating, operating it (`tools/scheduled_run.py`). |
| `FUTURE.md` | Postponed work: what is built but not in a production plan, what is not to be built, what is not decided, and other open items. |
| `DESIGN.md` | The design: families, blocks, states, stage contracts, folds, identity, checks (App. B), schemas (App. A). Written before the implementation; its "Stale" notes say where the code moved on, and where they disagree the code wins. Read §0 (one-page map), §2 (consumer interface), §3 (stage contracts) and App. A with their "Stale" notes; a v3 developer starts with §11 (v3 → v4 glossary) and §9. |
| `DECISIONS.md` | Every decision taken while implementing, one row each (`D<n>`), append-only: a later row amends or supersedes an earlier one. A row marked "default taken" is in force until a later row overrides it. D801 on; D0-D800 are in `repo-history/decisions-D1-D800.md`. |

**How-tos** (`howto/`): `inspect.md` (what a build fetched), `why-missing.md` (why a document is not in a build),
`full-run.md` (the full fetch), `s3-store.md`, `container-lifecycle.md`, `concurrency.md`, `overrides.md`,
`duplicates-and-urls.md`, `add-collector.md`, `add-feed.md`, `add-annotator.md`, `change-prompt-pin.md`,
`change-family-format.md`, `dataset-release.md`.

**Design notes** (`design/`), each the record of one piece of work; code comments cite their sections:

| Note | Subject |
|---|---|
| `catalogue-envelope.md` | One structure for the taxonomy files (techniques 1.1.0, models 1.1.0, orgs 0.2.x). |
| `collection-yield.md` | Which queries, authors and feeds flood the pipeline; the backfill cuts of 2026-10 (`configs/channels.yaml` cites it). |
| `license-classes.md` | The channels' licence classes (D8). |
| `link-crawling.md` | Outbound links of linking documents as candidate sources (`collect.links`). |
| `openalex-author-ids.md` | OpenAlex author ids for the researcher roster: an open worksheet (the ambiguous and not-found names are undecided), written by `tools/resolve_openalex_authors.py`. |
| `short-field-translation.md` | English translations of non-English titles and abstracts (D755). |
| `technique-quality-2026-10.md` | Technique extraction quality, October 2026: what was tried, measured and decided. |
| `url-normalisation.md` | The URL module (`stack/urls.py`) and its rules. |

**Scope notes** (`scope/`): `shallow-review.md` (the Shallow Review's scope, which the `shallow_review_relevance`
screen question asks about) and `sr-2025-repo-criteria.md` (the screening criteria of the Shallow Review 2025
repository, as implemented there).

**Context** (`repo-history/`): `pre-repo-history.md` (how the pipeline was developed before this repository, and
who did the work), `decisions-D1-D800.md` (the decisions up to D800, as written) and `superseded-records.md` (passages moved out of the current docs: estimates, development-store
results, the design's provenance, resolved items).

Elsewhere: `workflows/README.md` (stages and prompts), `configs/README.md` and `configs/plans/README.md`,
`taxonomy/README.md`, `evals/README.md`, `tools/README.md`, `spec/README.md`.

Gitignored, local to the machine that ran them: `v4/store-dev/` (the CLI's default store), other `v4/store-*/`
copies, `v4/evals-store/` (eval runs), `v4/mirror/` (local mirrors of `s3://` stores). S3 stores are implemented
with a local mirror, upload at publish and recovery (`OPERATE.md`, "The store, jobs and upload"; `howto/s3-store.md`);
there is no data garbage collection (D356) and no lock: one writer per store is a discipline.

## References used across the docs

- `D<n>`: a row of `DECISIONS.md` (from D801) or of `repo-history/decisions-D1-D800.md` (D0-D800); `D<n> (X)` names
  the line of work that took it.
- `X6`, `V11`, `S7`, …: checks of DESIGN Appendix B, as `stack verify` prints them.
- `(C<n>)`, `(E<n>)`, `F<n>` in `DESIGN.md`: rules of the design review's contract (DESIGN header), which is not in
  this repository; the sentence they annotate states the rule. B1-B3 are the design's three branches.
- Older rows and code comments also carry labels of plans, task boards and reviews that are not in this repository
  (milestones `M1`-`M5`, tasks `T<n.m>`, rounds, track letters, review findings `R<n>-<m>`); the sentence they
  annotate states the rule. `C#` in `DECISIONS.md` is a different list (census contradictions).
- People named in the docs: Tomáš Gavenčiak (maintainer), Dan (the v3 developer), Jake (author of the technique
  catalogue `taxonomy/taxonomy-v0.8.yaml`).

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
