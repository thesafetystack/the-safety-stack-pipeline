# Plans

A plan is one YAML file here: the stages a build runs, their pins, caps and options. It is loaded and validated by
`stack/plan.py` (pydantic, unknown keys refused); `uv run --no-sync python -c "from stack.plan import load_plan;
load_plan('configs/plans/<plan>.yaml')"` checks one. A plan names a **series**: its lines read and write
`data/<family>/<series>/` directories, and a build continues from each directory's head, whichever build or run
wrote it. Most plans in the tree set `series: <plan name>` (std-v3 reads and writes `data/<family>/std-v3/`, D378);
the production plan's series is `batch-import`.
Plans that share a series (e.g. a collect-only and an acquire-only plan, or dated plans sharing a `weekly` series)
extend the same directories; nothing is selected by plan name (`docs/OPERATE.md`, "Data files: series and chains").
The series is document identity (ids are minted per series): a build into a series with no `documents` while
another series of the store has some is refused unless `--new-series` (D491), so name the long-lived series in the
plan and keep it (the full fetch: `batch-import`; the e2e basis: `main`).

## The plans in the tree

| Plan | Use |
|---|---|
| `batch-2026-10a.yaml` | **Production**: the plan the weekly cycle builds (`docs/DEPLOY-railway.md`), series `batch-import`. Its first revision was the full fetch of 2026-10 from 2025-10-01; later revisions backfilled to 2015 (the forums to 2006) and added the link, manual and feed collectors, `annotate.technique_staged`, `model_roles`, `affiliation_orgs` and `translation`. The plan's header lists every revision with its D-number; how to run a full fetch or backfill with it: `docs/howto/full-run.md`. |
| `std-v3.yaml` | The earlier full-run plan (backfill from 2025-10-01, weekly runs), superseded by `batch-2026-10a`; kept for its stored runs and tests. |
| `std-v2.yaml` | The production shape before `std-v3`'s full-run changes; `std-v3` is std-v2 plus those. |
| `dev-v2-m4.yaml` | The dev store's real plan: small collector windows, `max_rows` caps, dev budgets. |
| `ann-scratch.yaml` | A copy of `dev-v2-m4` (screen-v11, no worker counts; the header says what moved on since) plus the `model_roles` (model mentions and roles in one stage, D591) and `technique_roles` lines, for the annotators' first real runs. |
| `links-scratch.yaml` | The first link-collection plan: `collect.feeds`, `collect.manual`, `collect.links` (all three now run in production; the newsletters' link harvest is still off, `docs/FUTURE.md`). |
| `dev-v2.yaml` | The synthetic test worlds' plan, pinned by test fixtures; a sql-only gate. Not for real stores. |

What each plan is and what loads it. Every file here is validated by `tests/test_plan_files.py` and
`tools/check_configs.py`; the last column names what else reads the file.

| Plan | Kind | Loaded by |
|---|---|---|
| `batch-2026-10a` | production (series `batch-import`) | the weekly cycle (`STACK_SCHED_PLAN`); `tests/test_plan_files.py` |
| `std-v3` | production before `batch-2026-10a`, superseded by it | `tests/test_cli.py`, `tests/test_plan_files.py` |
| `std-v2` | production before `std-v3`, superseded by it | `tests/test_stage_runner.py`, `tests/test_affiliation_stage.py` |
| `dev-v2-m4` | dev plan (the dev store) | `tests/test_screen_sr_schema.py`; the example in `stack/annotations.py` |
| `ann-scratch` | scratch plan (annotator real runs) | `tests/test_annotate_inputs.py` |
| `links-scratch` | scratch plan (link collection) | `tests/test_collect_manual.py` |
| `dev-v2` | test fixture | the synthetic worlds and fakes (`tests/synth/world.py`, `tests/fakes/annotate.py`); 35 files under `tests/` name it |

## Fields

```yaml
plan: std-v3                     # the plan's name; build records key on it
series: std-v3                   # the default series of every line's in/out; `stack build --series` overrides it
schema_version: 2
prompts:                         # per LLM stage: template name and model (or a llm.yaml role)
  triage.<question prompt>: {name: …, model: …}
  annotate.<wf>: {name: …, model: … | role: …, …}      # screen also: adjudicate_model, tier2, tier1_chars, tier2_chars
gate:                            # triage: per consumer, clauses that make a document worth fetching
  screen:
    - {rule: <source rule in stack/rules.py>}
    - {question: <name>, prompt: <prompts key>, fetch_on: [relevant, borderline]}
    - {sql: "<predicate over the documents row>"}
  no_basis_fetches: true
acquire:
  convert: {config: pymupdf4llm-v1, expect_pin: <hex prefix, optional guard>}
  wants: {<channel>: [<content_role>…]}  # which roles to fetch per channel
  recheck: {default: weekly}     # also article_in_press (default 14d), hosts: {<host>: <period>} (D701)
versions:                        # compaction only: a per-stage kind (`gather: full`) is refused (D378; a line's out: {kind: part})
  compaction: {max_partials: 12, max_partial_rows_ratio: 1.0}   # when fold.compact runs on documents/<series>/ at build start
fold:
  corpus: {filter: has_text}     # a named filter (has_text, passed_triage, everything) or SQL over the row
  build: {filter: has_text, annotations_folded: [screen, …]}
  release: {config: configs/releases/public-v1.yaml}
  diagnostic: false              # true: also write corpus.everything/<series>/ (files.corpus_everything; every row, for excluded_reason)
recovery: {dead_run_charge: false}
stages: […]                      # below
require_complete: [sources, coverage, documents, …]   # families whose writers must finish their scope, else the build is `incomplete`
```

Refused: `input_rule` and `versions.compact_after` (dropped by D22; DESIGN §3.5 still showed them),
`recovery.dead_run_charge: true` (not implemented), `recovery.heartbeat_timeout` (removed by T-STORE C2: a job's
liveness is its marker's age, `docs/OPERATE.md`).

## Stage lines

```yaml
- {stage: annotate.screen, prior: [screen, screen_evidence], budget: {max_usd: 40},
   options: {screen_usd_per_1k: 3.0, workers: 16}}
- {stage: triage, out: {kind: part}, budget: {max_usd: 20}}
# seeds exp-2 from legacy's answers: fulls without a base; a second run into the non-empty exp-2 dirs is a
# restart (refused without `restart: true`)
- {stage: annotate.screen, in: [data/screen/legacy/, data/screen_evidence/legacy/],
   out: {paths: [data/screen/exp-2/, data/screen_evidence/exp-2/], comment: try-v13}}
```

| Key | Meaning |
|---|---|
| `stage` | the stage id (`collect.openalex`, `gather`, `annotate.<wf>`, `fold.corpus`, …) |
| `prior` | families the stage reads as priors, from the line's series directories, to carry earlier answers (collectors: `[sources, coverage]`, also their default when omitted; annotate default `[<wf>, <wf>_evidence]`) |
| `in` | store paths (`data/<family>/<series>/` or an exact file) replacing the line's default inputs of the same family directory (below); not part of the line's config digest |
| `out` | `{paths, series, kind, comment, restart}`: `--out` destinations (`data/<family>/[<series>/]`, one per family), the line's series (default the plan's), `kind: part` (default full), the outputs' comment (`[a-z0-9-]+`) and `restart: true` for a full without a base into a non-empty directory; not part of the line's config digest |
| `budget` | `max_usd` (LLM spend per run), `max_minutes` (wall time per run); a run stopped by it ends scope-incomplete |
| `repeat` | `{until: scope_complete, max_runs: N}`: run the line again within the build until its scope is complete; not after a run that ended `halted` with fewer than 10 items ok (`stack/build.py` `halt_ends_repeat`) |
| `filter` | SQL over the row narrowing the stage's scope (annotate lines may use the macros, `ann_verdict(…)`) |
| `reads` | `annotate.<wf>` only: annotations joined for `filter` (scope reads, not part of the basis; D235) |
| `keys` | lookups only: which key kinds to look up |
| `miss_cap` | consecutive misses before a source counts as gone (`miss_means` of the channel) |
| `bump_on` | refused when not empty (D437): v3's list of config versions that force a re-run, read by nothing in v4; the field stays in the line digest until the next plan-digest bump removes it |
| `target`, `accepted` | `annotate.<wf>` (a workflow that keeps evidence) and `triage` only, together (D758; `docs/OPERATE.md` "Keeping answers across a config change"): `target` the config digest the line's pin gives (triage: `{question: digest}`; refused at build start when the pin gives another), `accepted` the earlier configs whose decided answers count, `[{config, name, decision, note?, question?}]` in preference order. In the plan digest, not in the line's config digest. |
| `require_complete` | `false`: the line's scope-incomplete run does not make the build `incomplete` (recorded under `incomplete_allowed`, with a warning; retried next build); a failed run still fails the build. Default `true`; not part of the line's config digest. std-v3 sets it on `collect.s2-lookup` only (keyless S2 stops at its first 429). |
| `options` | stage options, read by the stage (`c.option(…)` in collectors). Every stage module declares the keys it reads (`OPTIONS`); any other key is refused before the run record exists and at the build's start (`stage_refused`; D434, D472; `stack/stage_runner.py` `check_options`). The values are not schema-checked. Part of the line's config digest, except: on triage `workers`, `llm_mode` and the `batch_*` options are left out; an `annotate.<wf>` digest is its prompt pin's and takes no option. |

Options in use, by stage: collectors (`from`, `window_days`, `max_records`, `subwindow_days`, `max_usd`, …: the
collector's docstring; `collect.s2-lookup`: `only_missing_abstract` (default `true`, D520); `collect.openalex` and
`collect.openalex-lookup`: `select` (default none: full works, D525)), `triage` (`workers`, `max_rows`, `llm_mode`,
`batch_*`; `title_basis: true` asks a row with no abstract and no excerpt on its title and metadata with the
question's title variant instead of `skipped[no_basis]`, and makes the build pass the `sources` chains to triage,
D527, `workflows/triage/ask.py`), `acquire` (`max_rows`, `fetch_workers`, `reopen`; `order: doc_id | newest_first`;
`convert_max_rows`, `convert_max_minutes`, D391-D392 (absent: no convert row cap, and convert's own clock of
`budget.max_minutes` after the fetch, so a run can take up to 2 x `budget.max_minutes`, D483); `convert_workers`,
`convert_threads`, `convert_timeout_s`, `allow_degraded_convert`; `structured`, `quality`; `arxiv_pdf: always |
on_miss | never` (D480); the lanes' `http_timeout_s`, `max_bytes`, `wayback`, `headless`, `oa_route`,
`oa_target_task`, `oa_playwright`. The set and each key's reader: `workflows/acquire/main.py` `OPTIONS`; the
clocks: `docs/OPERATE.md` "`incomplete` builds and resuming"; `structured` and `arxiv_pdf`: `docs/OPERATE.md` "The
convert pin and the venv"), `annotate.*`
(`max_rows`, `workers`, `llm_mode`, `batch_*`; screen: `screen_usd_per_1k`, `tier2_max_usd`, `tier2_budget_share`,
`tier2_max_share`). `llm_mode: batch` (default `sync`) asks through OpenRouter's batch API at half price, with
`batch_wait_minutes` (time cap; default none, hard stop 26 h), `batch_min_requests` (100: a smaller pass is asked
sync), `batch_max_requests` (2000) and `batch_max_mb` (40) per batch: `docs/howto/concurrency.md`, "Batch mode".

## What a build line reads and writes

`S` is the line's series (`out.series`, else the plan's `series:`); `<f>/S/` is `data/<f>/S/`, its head at the
run's start. A line's `in:` replaces the defaults of the same family directory; outputs go to the directory of the
run's input of that family unless `out.paths` names another (`stack.build.stage_inputs`, `docs/OPERATE.md`, "Runs
stand alone").

| Stage | `--in` | Writes |
|---|---|---|
| `collect.<p>` | `sources.<p>/S/`, `coverage.<p>/S/` (the line's `prior:`; empty on a first run); `collect.manual` also `overrides/S/` when it has a file | the same directories |
| `collect.<p>-lookup` (and the other collectors that read documents) | as `collect.<p>`, plus `documents/S/`; not run, with a warning, while `documents/S/` is empty | `sources.<p>/S/`, `coverage.<p>/S/` |
| `gather` | every `sources.*/S/` with a file; `documents/S/` and `overrides/S/` when they have a file | `documents/S/` (full) |
| `triage` | `documents/S/`, `triage_evidence/S/` | `documents/S/`, `triage_evidence/S/` |
| `acquire` (and any other stage) | `documents/S/` (when it has a file) | `documents/S/` (acquire) |
| `fold.compact` | `documents/S/` | `documents/S/` (full, base the head) |
| `fold.corpus` | `documents/S/` | `corpus/S/`; with `fold.diagnostic` a second run, filter `everything`, into `corpus.everything/S/` |
| `annotate.<wf>` | this build's `fold.corpus` file (exact path), this build's file of every joined read, `overrides/S/` when it has a file, `<f>/S/` per family of the line's `prior:` | `<wf>/S/`, `<wf>_evidence/S/` |
| `fold.build` | this build's `fold.corpus` file and its `annotate.<wf>` output per `annotations_folded` workflow | `corpus.build/S/` |

The standard plans set `out: {kind: part}` on the collector, triage and acquire lines; without it those stages write
the full file their part makes. A missing this-build input (annotate before `fold.corpus`, a joined read whose
producer wrote nothing) fails the build `missing_input`.

## Which commands read which plan

`stack build <plan>` takes it as its argument (a name here or a path). `run`, `fold`, `release`, the `eval` verbs
`triage`/`screen`/`affiliation`/`model_roles`/`technique_roles`/`audit-prompts`, `acquire reopen` (except
`--dry-run`) and `diff` with `latest` use `--plan`/`STACK_PLAN`; there is no default. `status`, `head`, `ls`, `chain orphan`, `store push`, `overrides add` and
`verify` work without one.
