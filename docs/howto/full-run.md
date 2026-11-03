# A full fetch or a backfill (`batch-2026-10a`)

The production plan `batch-2026-10a` (series `batch-import`) is built every week by the scheduled cycle on Railway
(`docs/DEPLOY-railway.md`). Each build continues the series from the heads the build before it published (D278,
D491), so a week's builds ask only what is new or due. The cycle does not start a new series, move a collector's
floor back or add overrides to the store; this page is how to do those by hand: the first full fetch into a new
series or store, a backfill below the current floor, and the operator steps a plan revision asks for. The build
order below is the one the cycle runs. The plan's header lists its revisions: the full fetch of 2026-10 from
2025-10-01, then backfills to 2024, 2020 and 2015 (the forums to 2006).

## Before you start

1. **Venv.** `uv sync --frozen --group convert-pdf --group release` in the checkout that runs it (pymupdf4llm
   1.28.0, pymupdf 1.28.0, trafilatura 2.2.0). The acquire line pins `expect_pin: 625b4004…` (pymupdf4llm-v1 with
   structured text); a missing tool or another version ends the run `failed[environment]` instead of converting at
   another pin.
2. **Store**: an S3 store, `s3://<bucket>/<prefix>` (`docs/howto/s3-store.md`), not `v4/store-dev`. On a disposable
   machine, follow `docs/howto/container-lifecycle.md` (pull, run, watch, discard, what to do when it dies). On the
   store a cycle builds, pause the cycle first (`python tools/scheduled_run.py pause --note <why>`, then `resume`;
   `docs/DEPLOY-railway.md`): a series has one writer at a time (`docs/howto/concurrency.md`).
3. **Series.** The series is document identity (`docs/OPERATE.md`): every later build must use the same one, or it
   re-mints every id and re-pays every call. The first build into a series with no `documents` beside one that has
   them is refused without `--new-series`; `--dry-run` shows the verdict on its `new_series` line. Later builds run
   without the flag.
4. **Environment.** `env | grep -E '^STACK_[A-Z_]*(_LIMIT|_FRACTION|_MAX_QUERIES)='` prints nothing: a build or a
   collector run refuses to start while one is set (D553; `--allow-env-limits` only for a deliberately capped run).
   `STACK_MAILTO` and the keys are read from `STACK_ENV_FILE` when unset.
5. **Spend.** Each run's spend is in its run record (`spend`), capped per run by its line: triage $20, screen $40,
   affiliation $50, technique_staged $350, model_roles $230, translation $2, collect.links $0.50, and OpenAlex's
   metered API $3 per collect run and $1 per lookup run. An LLM line without `budget.max_usd` gets `configs/llm.yaml`
   `budget.per_run_usd` ($50). Each pass of `--passes N` is a build with fresh per-run caps. Outside a cycle (whose
   `STACK_SCHED_MAX_USD` caps its jobs) nothing caps spend across runs or passes: the providers' own credit limits
   are the only bound, so set them to the intended ceiling before launching. A total so far: `docs/OPERATE.md`.

## The build order

The lookups (`collect.openalex-lookup`, `collect.s2-lookup`, `collect.card-dates-lookup`) and `collect.links` read
the series' documents, which only a gather writes, and a plan lists gather once; so the first build stops after
gather and the later builds run every line, with the lookups' abstracts gathered before triage. Three slow
collectors run as standalone runs, not in the builds (D554 for S2; the cycle treats the arXiv sweep the same way):
`collect.semantic_scholar` and `collect.arxiv_sweep` before build 1 (or beside it: build 1 writes neither of their
directories), `collect.s2-lookup` between build 1 and the next. The cycle runs exactly this (`tools/scheduled_run.py`,
docstring and `steps_all`), with one-pass builds after build 1.

`S=s3://<bucket>/<prefix>` and `P=configs/plans/batch-2026-10a.yaml` below; every command runs detached (tmux, or
`systemd-run --user` as in `docs/howto/container-lifecycle.md` §2) as `STACK_ENV_FILE=.env uv run --no-sync stack
--store $S …`. SIGTERM or `stack stop` drains, a second SIGTERM or `stack stop --now` cancels (`docs/OPERATE.md`).
Each build or run prints its record path; the build record's `incomplete` lists `{stage, run_id, reasons}`, the CLI
repeats them on stderr (`incomplete: <stage> run <id> (<reasons>)`, exit 3), and `stack --store $S show runs
<build_id>` tabulates the runs. Watch with `stack --store $S status` from any machine with the S3 settings (live and
dead jobs, spend so far), and `df` on the running one. `--new-series` below is for the first runs into a new series
only (a collector run or build that would mint ids into a series with no documents while another series has some
is refused without it, D491); continuing `batch-import` needs none.

0. **Dry run.** `stack --store $S build $P --dry-run <build 1's flags>` shows `env_limits -`, the `new_series`
   verdict, the three standalone collectors `skipped`, and no `preflight` error.
1. **S2 collect and the arXiv sweep**, standalone:

   ```bash
   stack --store $S --plan $P run collect.semantic_scholar --series batch-import [--new-series] \
     --in data/sources.semantic_scholar/batch-import/ --in data/coverage.semantic_scholar/batch-import/
   stack --store $S --plan $P run collect.arxiv_sweep --series batch-import [--new-series] \
     --in data/sources.arxiv_sweep/batch-import/ --in data/coverage.arxiv_sweep/batch-import/
   ```

   Each reads the plan's line (S2 from 2015-01-01 with `max_records` 250000 since revision 12, D800). Check each
   record: `scope_complete` true; else the reasons (S2: `windows_failed`, a page still 429'd after six waits;
   `halted`, a `Retry-After` above 60 s; `limit`) and run the same command again (only due queries are asked). The
   cycle drains the arXiv sweep after 45 min (`STACK_SCHED_ARXIV_MAX_MIN`); what it did not reach stays due.
2. **Build 1** (collectors and gather):

   ```bash
   stack --store $S build $P [--new-series] --passes 1 --skip triage --skip acquire --skip 'fold.*' --skip 'annotate.*' \
     --skip collect.semantic_scholar --skip collect.s2-lookup --skip collect.arxiv_sweep
   ```

   The lookups are skipped with a warning while the series has no documents.
3. **After build 1 and the S2 run have ended**: review build 1's `incomplete[].reasons` (below) and run the S2
   lookup:

   ```bash
   stack --store $S --plan $P run collect.s2-lookup --series batch-import \
     --in data/sources.s2-lookup/batch-import/ --in data/coverage.s2-lookup/batch-import/ \
     --in data/documents/batch-import/
   ```

   It asks only documents without a usable abstract (D520); a run that ends short is acceptable (enrichment): the next
   run asks the keys it left. Wait for the S2 collect run first: both share S2's one request per 2.5 s.
4. **Build 2** (everything else):

   ```bash
   stack --store $S build $P --passes N --skip collect.semantic_scholar --skip collect.s2-lookup --skip collect.arxiv_sweep
   ```

   Its gather folds in the standalone runs' rows published before it starts; anything they publish later is
   gathered by the next build. `--passes` stops early on `failed`, `converged` (an `ok` pass that made no call and
   changed no row) or `no progress`.
5. **Release** once a build ends `ok`: `docs/howto/dataset-release.md`. Then `resume` the cycle, which continues the
   series in the same order every week.

## Reading `incomplete`

A stage stopped by its cap (acquire: 45 min of fetch plus up to 45 min of convert per run, D483, up to 6 runs per
build; the LLM lines' budgets; OpenAlex's $3 per collect run) ends `ok` but scope-incomplete; the build still runs
every line and ends `incomplete` (exit 3). The next pass or build takes it as its prior, carries the `ok` rows and
asks only the rest. `stack head data/corpus.build/batch-import/` names the series' last corpus file, which can be an
`incomplete` build's; the export refuses a corpus only an `incomplete` build names unless given `--allow-incomplete`.
`collect.s2-lookup` is the exception (`require_complete: false` on its line): it waits a 429 out up to six times (5 s
doubling to 60 s, D521, D550) and stops at a batch still 429'd after that; the build still ends `ok` (the record's
`incomplete_allowed` and a warning say so), and the next build retries the keys it left.

Expected while a full fetch or a backfill runs: acquire `time` (its clock); `collect.cards` `limit` until the
deepseek/qwen history sweep has caught up (25 repos a run, D555); annotate `budget` until the corpus annotations have
caught up. To act on: `limit` on a search collector (raise its `max_records`, or for forums narrow a broad query with
`query_from`, D552), `windows_failed` (rerun or let the next pass retry), `halted` and `held` (a provider asked for a
longer pause), `budget` on triage or screen (a spend cap). arXiv's 429/503 is waited out (60, 120, 240 s, D584); only
the fourth in a row gives up for the run: on Atom (`arxiv.atom_limited` 1) the papers whose version is unknown are
answered without a request (`arxiv.limited_roles`; due next run; `html` roles still try the unversioned HTML URL,
D585), on arxiv.org itself the lane halts (`halted.arxiv`, reason `halted`). `stack show runs` lists these counts by
default (`docs/OPERATE.md`).

OpenAlex resumes each query 60 days of publication dates below its watermark (`lookback_days`, D582; the plan cannot
ask by creation date); a work it indexes later than that is missed until a re-sweep from the floor,
`STACK_OPENALEX_FULL=1 stack run collect.openalex` with this plan's line. The cycle runs that re-sweep weekly
(`docs/DEPLOY-railway.md` has its time and cost). A `halted` OpenAlex run whose record's `error` starts
`provider_plan:` asked for a filter the account's plan lacks (D583): fix the filter, a retry fails the same way.

## Moving the floor back (a backfill)

A backfill is a plan revision: change `from` on the search collector lines (arxiv_sweep, openalex,
semantic_scholar, forums, af_graphql, lw_graphql). Each search query's coverage key leaves the floor out, so its `ok`
windows stand and only the gap below the old floor is swept (D272). Then run the build order above without
`--new-series`. The revision moves the plan digest, which the cycle checks: set `STACK_SCHED_EXPECT_PLAN_DIGEST` in
the same step as the deploy (`docs/DEPLOY-railway.md`).

- **AF** lists a hole in bounded 180-day spans and halves a span its GraphQL refuses (one listing reaches ~2,200
  posts, the server's skip limit; D645).
- **Forums** sends a hole only its upper bound and the karma floor (D642: its search drops a two-sided date range but
  honours either bound alone), so each query re-pages its karma-gated history before the old floor once and cuts it
  at the new one; give the forums line a `max_records` the largest query stays under (a capped pass claims nothing
  and stays due). ForumMagnum's search answers at most 10,000 hits per request (page 100 of 100 is an HTTP 400): a
  query over that in a hole or a tail ends `partial`, reason `limit` (`server_page_cap`); probe the counts first
  (`hitsPerPage: 1`, read `nbHits`) and start such a query later with `query_from` (D552). A forums `from` at or
  below the channels' `from_date` (2020-01-01) sends a query's first pass no lower date bound (the whole index).
- **A change to a collector's query** (a new concept group, a changed filter) gives the affected queries new coverage
  keys: each re-sweeps from the floor once (the plan header gives the sizes of the revisions so far).

## A plan revision's operator steps

Some revisions add prepared overrides to the store before their first build (the plan's header says which):
`stack --store $S overrides add <file> --out data/overrides/batch-import/`, `--dry-run` first, for each file
(`docs/howto/overrides.md`). The files in the tree: `configs/overrides/manual_sources.yaml` and
`lineage_origins.yaml` (revision 5; D658, D664), `china_coverage.yaml` and `data_quality_2026_10.yaml` (revision 11;
D768, D772, D774). The manual sources in the first three are URLs and ids of the works; the other overrides
(`lineage_origins.yaml`'s screen and relevance verdicts, all of `data_quality_2026_10.yaml`) name documents of series
`batch-import` by their ids, so they apply only to that series.

The estimates made before the 2026-10 full fetch, the standalone S2 collect's expected times and the open choices of
that time: `docs/repo-history/superseded-records.md`.
