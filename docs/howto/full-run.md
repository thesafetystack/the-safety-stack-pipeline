# The full run (`std-v3`, `batch-2026-10a`)

The production plan is `batch-2026-10a` (below; `configs/plans/README.md`); its weekly builds run as the scheduled
cycle on Railway (`docs/DEPLOY-railway.md`; cycles since 2026-10-31), and this page is the manual procedure.
`configs/plans/std-v3.yaml` was the full-run plan before it: the first backfill from 2025-10-01 and the weekly runs
after it, one plan in one series so that each build continues from the heads the one before published (D278, D491). Its header
lists what differs from `std-v2`. Its real runs, and the rehearsal on a 7-day window (track T-REH), were logged in
the 2026-10 run log (not in this repository); the rehearsal's extrapolation to the backfill was in `v4/HISTORY.md` (removed; kept in the archived pre-publication
repository, root `README.md`, "Removed").

The full fetch of 2026-10 runs `configs/plans/batch-2026-10a.yaml` in series `batch-import`: std-v3's lines (forums
included) plus `collect.feeds`, triage `title_basis: true` (D527), acquire `order: newest_first`
and `arxiv_pdf: on_miss` (D480), 256 LLM workers on triage, screen and affiliation (32 until D692), an explicit
affiliation budget and cards `full: true` (D555), and from later revisions `collect.manual`, `collect.links` and the
annotators technique_staged, model_roles, translation and affiliation_orgs (its header lists the differences). It
runs as two builds, because its lookups read the series' documents, which only a gather writes: build 1 collects and gathers; build 2 runs every line, so the lookups' abstracts are gathered before
triage. S2 runs beside the builds, not in them (D554): its collect as a standalone run in parallel with build 1, its
lookup as a standalone run between the builds. The procedure below applies with that plan's name;
"The 2026-10 full fetch" gives the commands.

## Procedure

1. **Venv.** `uv sync --group convert-pdf` in the checkout that runs it (pymupdf4llm 1.28.0, pymupdf 1.28.0,
   trafilatura 2.2.0). The acquire line pins `expect_pin: 625b4004…` (pymupdf4llm-v1 with structured text); a
   missing tool or another version ends the run `failed[environment]` instead of converting at another pin.
2. **Store**: an S3 store of its own, `s3://<bucket>/<prefix>` (`docs/howto/s3-store.md`), not `v4/store-dev`. On a
   disposable machine, follow `docs/howto/container-lifecycle.md` (pull, run, watch, discard, what to do when it dies).
3. **Series.** The series is document identity (`docs/OPERATE.md`, "The series is document identity"): every later
   weekly build must use the same one, or it re-mints every id and re-pays every call. The full fetch's series is
   `batch-import` (in `stack-store/` beside the e2e's `main`; D491). Its first build there needs
   `--new-series` (a build into a series with no `documents` beside one that has them is refused without it);
   `--dry-run` shows the verdict on its `new_series` line. Later builds run without the flag.
4. **Run**, detached (tmux, or `systemd-run --user` as in `docs/howto/container-lifecycle.md` §2; SIGTERM or `stack
   stop` drains, a second SIGTERM or `stack stop --now` cancels: `docs/OPERATE.md`, "Stopping, cancelling and resuming"):

   ```bash
   STACK_ENV_FILE=.env \
     uv run --no-sync stack --store s3://<bucket>/<prefix> --plan configs/plans/std-v3.yaml build std-v3 --passes 6
   ```

   Every run's data and record go up as it finishes. Watch with `stack --store s3://<bucket>/<prefix> status` from
   any machine with the S3 settings (live and dead jobs, spend so far), and `df` on the running one.

   `STACK_MAILTO` is read from `STACK_ENV_FILE` when unset. Each run's spend is in its run record (`spend`), capped per
   run by its line (T-STORE D328); an LLM line without `budget.max_usd`, as std-v3's affiliation line, gets
   `configs/llm.yaml` `budget.per_run_usd` ($50). So one pass can reach $114: triage $20, screen $40, affiliation
   $50, keyed OpenAlex $3 per collect run and $1 per lookup run. Each pass of `--passes N` is a build with fresh
   per-run caps, so N passes can reach N times that ($684 for 6). Nothing caps spend across runs or passes: the
   OpenRouter key's own credit limit is the only bound on the LLM spend, so set it to the intended ceiling before
   launching. A total so far: `docs/OPERATE.md`, "Spend in run records and per-run caps".
5. **Resume.** A stage stopped by its cap (acquire: 90 min of fetch plus up to 90 min of convert per run, D483, up to 4 runs per build; triage $20, screen $40
   and affiliation $50 per build; OpenAlex $3 per collect run) ends `ok` but scope-incomplete; the build still runs
   every line and ends `incomplete` (exit 3). The next pass or build takes it as its prior, carries the `ok` rows
   and asks only the rest. `--passes` stops on `failed`, `converged` or `no progress` (e.g. a run's budget reached).
   `stack head data/corpus.build/std-v3/` names the series' last corpus file, which can be an `incomplete` build's;
   `stack release` refuses a corpus only an `incomplete` build names unless given `--allow-incomplete`
   (`docs/OPERATE.md`, "`incomplete` builds"). `collect.s2-lookup` is the exception (`require_complete: false` on its
   line): it asks only for documents without a usable abstract (D520), waits a 429 out up to six times (5 s doubling to
   60 s, D521, D550) and stops at a batch still 429'd after that; the build still ends `ok` (the record's
   `incomplete_allowed` and a warning say so), and the next pass or build retries the keys it left.
6. **Release** once a build ends `ok`: `stack --store s3://<bucket>/<prefix> --plan configs/plans/std-v3.yaml release
   --in data/corpus.build/std-v3/ --config public-v1` (the directory's last file; `--in` also takes an exact file).
7. **Moving the floor back.** Change `from` on the six search collector lines; only the gap below the old floor is
   swept (D272). `batch-2026-10a` revision 2 moves it to 2024-01-01 (its header): the same two-build order, with no
   `--new-series`; AF lists a hole in bounded 180-day spans and halves a span its GraphQL refuses (one listing
   reaches ~2,200 posts, the server's skip limit; D645, before which an AF hole older than ~2,200 posts back from
   today failed `parse_error` in every build). Forums sends a hole
   only its upper bound and the karma floor (D642: its search drops a two-sided date range but honours either bound
   alone), so each query re-pages its karma-gated history before the old floor once and cuts it at the new one.
   ForumMagnum's search answers at most 10,000 hits per request (page 100 of 100 is an HTTP 400): a query over that
   in a hole or a tail ends `partial`, reason `limit` (`server_page_cap`); probe the counts first (`hitsPerPage: 1`,
   read `nbHits`) and narrow such a query with `query_from` (D552). A forums `from` equal to the channels'
   `from_date` (2020-01-01) sends a first pass no lower date bound (the whole index); revision 3 uses 2020-01-02,
   revision 4 (D646) 2006-01-01 for the three forum collectors (before the channels' `from_date`, so every forum
   first pass is sent no lower bound: at 2006, the whole index either way). Revision 5 (D663) moves no window: its
   collector changes (D653-D655) give every OpenAlex query a new coverage key, so the 147 OpenAlex queries re-sweep
   from 2015-01-01 once (≈ 2,800 requests, ≈ $2), and the 59 S2 group queries in the standalone S2 collect run (≈ 575
   requests, ≈ 25-50 min); its triage prompt (D656) re-asks triage's question scope, the screen stays on screen-v12
   (staged). Before its first build add the prepared overrides: `stack --store $S overrides add
   configs/overrides/manual_sources.yaml --out data/overrides/batch-import/`, then the same for
   `configs/overrides/lineage_origins.yaml` (`--dry-run` first each; D658, D664).

## The 2026-10 full fetch (`batch-2026-10a`)

`S=s3://<bucket>/stack-store` and `P=configs/plans/batch-2026-10a.yaml` below; every command as in step 4 (detached,
`STACK_ENV_FILE=.env uv run --no-sync stack --store $S …`). Each build or run prints its record path; the build
record's `incomplete` lists `{stage, run_id, reasons}`, the CLI repeats them on stderr (`incomplete: <stage> run
<id> (<reasons>)`, exit 3), and `stack --store $S show runs <build_id>` tabulates the runs.

0. **Checks before.** `env | grep -E '^STACK_[A-Z_]*(_LIMIT|_FRACTION|_MAX_QUERIES)='` prints nothing: a build or a
   collector run refuses to start while one is set (D553; `--allow-env-limits` only for a deliberately capped run).
   `stack --store $S build $P --dry-run <build 1's flags>` shows `env_limits -`, `new_series allowed by
   --new-series`, `collect.semantic_scholar` and `collect.s2-lookup` `skipped`, and no `preflight` error.
1. **Build 1** (collectors and gather):

   ```bash
   stack --store $S build $P --new-series --passes 1 --skip triage --skip acquire --skip 'fold.*' --skip 'annotate.*' \
     --skip collect.semantic_scholar --skip collect.s2-lookup
   ```

2. **S2 collect, beside build 1** (a second detached process on the same machine and store mirror):

   ```bash
   stack --store $S --plan $P run collect.semantic_scholar --series batch-import --part --new-series \
     --in data/sources.semantic_scholar/batch-import/ --in data/coverage.semantic_scholar/batch-import/
   ```

   `--new-series` is needed on this first run: it pins no `sources` of its own (the directory is empty) and
   `documents/batch-import/` has no file until build 1's gather publishes, while `main` has documents (D491). A later
   run pins its own `sources` and needs no flag. The run reads the plan's line (from 2025-10-01 in the first revision,
   `max_records` 100000; 250000 since revision 12, D800). It cannot make build 1 fail: build 1 writes neither of its
   directories (`head_moved` is per destination directory), its verify accepts the new files, and a skipped line is no
   `incomplete` entry (D554). Expected time (D550, a virtual-clock test of 62 pages at one request per 2.5 s): ~3 min
   without 429s, ~6 min at 25 %, ~24 min at 49 % (the e2e took 61 min). Check its record: `scope_complete` true; else
   the reasons (`windows_failed`: a page still 429'd after six waits; `halted`: a `Retry-After` above 60 s; `limit`)
   and run the same command again (only due queries are asked).
3. **After build 1 and the S2 run have ended**: review build 1's `incomplete[].reasons` (step 6) and run the S2
   lookup:

   ```bash
   stack --store $S --plan $P run collect.s2-lookup --series batch-import --part \
     --in data/sources.s2-lookup/batch-import/ --in data/coverage.s2-lookup/batch-import/ \
     --in data/documents/batch-import/
   ```

   It asks only documents without a usable abstract (D520); a run that ends short is acceptable (enrichment): the next
   run asks the keys it left. Wait for the S2 collect run first: both share S2's one request per 2.5 s.
4. **Build 2** (everything else):

   ```bash
   stack --store $S build $P --passes N --skip collect.semantic_scholar --skip collect.s2-lookup
   ```

   Its gather folds in the S2 collect and lookup rows published before it starts; anything an S2 run publishes later
   is gathered by the next build.
5. **Weekly builds** continue the series with this plan (no `--new-series`). With S2 at 2.5 s and the backoff, a
   week's S2 collect is minutes; keeping the two `--skip`s and the standalone runs is the operator's choice.
   OpenAlex resumes each query 60 days of publication dates below its watermark (`lookback_days`, D582; our plan
   cannot ask by creation date); a work it indexes later than that is missed until a full re-sweep, so about once a
   month run `STACK_OPENALEX_FULL=1 stack run collect.openalex` (this plan's line; the 2026-10-14 sweep from
   2025-10-01: 845 pages, 39.7 min, $0.73). A `halted` OpenAlex run whose record's `error` starts `provider_plan:`
   asked for a filter the account's plan lacks (D583): fix the filter, a retry fails the same way.
6. **After each pass** (each build of `--passes`), review `incomplete[].reasons`. Expected while the backfill runs:
   acquire `time` (its 90-min clock); `collect.cards` `limit` until the deepseek/qwen history sweep has caught up (25
   repos a run, D555). To act on: `limit` on a search collector (raise its `max_records`, or for forums narrow a broad
   query with `query_from`, D552), `windows_failed` (rerun or let the next pass retry), `halted` and `held` (a
   provider asked for a longer pause), `budget` (a spend cap). arXiv's 429/503 is waited out (60, 120, 240 s, D584);
   only the fourth in a row gives up for the run: on Atom (`arxiv.atom_limited` 1) the papers whose version is unknown
   are answered without a request (`arxiv.limited_roles`; due next run; `html` roles still try the unversioned HTML
   URL, D585), on arxiv.org itself the lane halts (`halted.arxiv`, reason `halted`). `stack show runs` lists these
   counts by default (OPERATE, "show runs").

## What to expect (estimates, D278, not measured end to end)

Estimates for std-v3, made before the first full fetch; the full fetch's measured runs are in the store's run
records.

- Size: ~105-140k documents after gather (OpenAlex alone ~144k group hits since 2025-10-01 before dedup).
- LLM: triage ~$14-18, screen ~$30-90 (capped at $3 per 1,000 documents by `screen_usd_per_1k`), affiliation
  ~$14-45; total ~$60-150 for the whole backfill. There is no $150 cap: the host-wide ledger that held it was
  removed (D328); the caps are the per-run ones of step 4.
- Wall time ~3-5 days: triage ~8 h, acquire ~2-3 days of builds (arXiv's 3 s per request and the sequential oa
  lane), screen 9-15 h, affiliation 4-7 h.
- Disk: local parquet a few GB; the blobs a container fetches stay in its mirror until it is discarded, ~100-150 GB
  for the backfill (an estimate from the S3 store's design; less with the acquire option `arxiv_pdf: on_miss`, which
  std-v3 does not set). In the bucket: ~55-80 GB by the rehearsal's extrapolation; an earlier
  estimate here said ~100-170 GB of PDFs.
- `stack verify` runs in every build; its memory was bounded in round 5 (T-SCALE; RUNLOG (SCALE)).

## Open choices

The decisions marked "default taken, for Tomáš" in D236-D279. Two are superseded in `batch-2026-10a`: D269 kept the
new annotators out of the production plans (model_roles runs there since revision 7, D710, beside technique_staged;
technique_roles stays in `ann-scratch`), and D277 resolved OpenAlex author ids but left them off (`collect.openalex`
sets `author_ids: true` there).
