# The weekly cycle on Railway

**Deployed**: cycles have run on Railway since 2026-10-31 (test cycles on 2026-10-31 and 2026-11-01; the fixes
that followed are D780-D786). Prepared on 2026-10-26; run before the first deploy: the entrypoint's dry run against
the production S3 store (read-only), the dataset exports from a cold mirror, the targeted tests
(`tests/test_scheduled_run.py`, every Hugging Face and email call on fakes). The measurements behind the design are
in design notes that are not in this repository; the design itself is this page and `DESIGN.md`. Decided on
2026-10-26: weekly on Sunday 00:00 UTC, up to 5 builds, spend and wall caps, a 24 GB replica on Railway Pro, publish
on every successful cycle, one summary email per cycle titled by its worst severity, release a build short only on
collectors, clean tagged releases, balance alerts, a Monday watchdog. Amended on 2026-10-28: a build short only on
acquire's retry tail is released too, and an acquire halt ends the cycle's builds (below); and on 2026-10-30: such a
build is also released when a cap or stop comes before the catch-up build, and only while its tail is at most 5% of
the documents acquire covers.

| Piece | Where |
|---|---|
| The cycle | `tools/scheduled_run.py` (`cycle [--dry-run\|--test]`, `watchdog`, `upload`, `update-website`, `pause`/`resume`, `report`, `unlock`; its docstring) |
| The image | `Dockerfile`, `.dockerignore` |
| Railway config | the dashboard (below): Config as Code (`railway.json`/`railway.toml`) is deprecated, new services cannot opt in, and its files stop being read on 2026-12-01 (docs.railway.com/infrastructure-as-code); the IaC reference documents no cron schedule or restart policy |

## What a cycle does

`python tools/scheduled_run.py cycle`, the image's default command, against `STACK_STORE` (an `s3://` store):

1. Refuses to start (exit 4) on a preflight problem (a missing key, including the LLM keys the plan's prompt pins
   need through `configs/llm.yaml`; `HF_TOKEN` when it will upload; a collector cap variable or test hook set; a plan
   digest other than `STACK_SCHED_EXPECT_PLAN_DIGEST`; a convert tool or `pdftotext` missing; less free disk than
   `STACK_SCHED_MIN_FREE_GB`), on `hold.json`, on `lock.json` held by another cycle, or on a live job on the store.
2. The OpenAlex re-sweep from the floor (`STACK_OPENALEX_FULL=1`) when the last one is 6 days old or older
   (`STACK_SCHED_OPENALEX_FULL_DAYS`): weekly. Each build's `collect.openalex` line is the incremental pass (60 days
   of publication dates below each query's watermark); the re-sweep catches works OpenAlex indexes later than that
   (~11% of 2026 works). Measured: ~40 min / $0.73 from a 2025-10 floor, ~124 min / $1.95 from the 2015 floor. A
   failed re-sweep is a WARN and the cycle goes on (the builds' incremental pass still runs; the re-sweep publishes
   nothing, so it is due again next cycle and starts over: D789).
3. Standalone `collect.semantic_scholar` (again once when scope-incomplete) and `collect.arxiv_sweep` (drained after
   45 min). An S2 run with `failed_permanent` windows or a year span whose upper bound S2 ignored is a WARN (read
   from its run record: S2 runs outside the builds, whose warning would say it; D802).
4. Build 1 (collectors and gather), standalone `collect.s2-lookup`, then one-pass builds of every other line until
   one ends `ok` or is short only on collector lines (at most 5). A build short on triage or an annotate line is
   followed by another; a reason outside the expected ones (acquire's clock, `pending` and `halted`, `budget`,
   `limit`, screen's `tier2_allowance`, the cards and forums reasons), e.g. `unspecified`, is a WARN and the next
   build retries it. **Acquire's retry tail**: a build short on acquire only for `halted`, `time` or `pending` (and
   perhaps on collector lines) is releasable. When acquire ended `halted` (a lane-wide provider refusal such as
   Unpaywall's or the ID Converter's 429 halts the OA lane for the run, and the build no longer repeats a halted run
   that completed fewer than 10 roles: `stack/build.py` `halt_ends_repeat`), no further build runs: the next would
   meet the same 429s. On the clock alone (`time`, `pending`) the next build goes on while the build's last acquire run
   completed at least 10 roles ok (`outcome.ok`, the same threshold; rows written are no measure: the 2026-10-27
   build's run 2 wrote 393 rows with 12 roles ok) (a catch-up); when no build ends `ok`, the newest tail build is
   the release, also when a cap or stop (the wall or USD cap, `hold.json`, a signal) comes before the catch-up build
   or drains it (D769): the tail build's corpus is written and nothing after the stop changes it. After a lost lock
   nothing is released (step 5). **The
   tail's limit**: that build is released only while its tail, the last acquire run's `scope.due` (documents with a
   fetch role due and fetchable when the run started: `not_attempted`, or `retry_after` passed), is at most
   `STACK_SCHED_TAIL_MAX_PCT` (5) percent of that run's `scope.fetch` (the documents in acquire's scope that triage
   lets it fetch, fetched already or not). The 2026-10-27 build: 677 of 72,935, 0.93%. Above the limit, or when
   either count is missing, it is not released (ERROR, exit 3, as before D733); the builds go on or stop by the rules
   above either way. The tail's documents are not in that
   corpus (its folds keep `has_text` rows) and nothing downstream ran on them; the next cycle fetches them and its
   corpus, screen and annotations take them in. A build short on acquire for another reason (`limit`, `not_run`,
   `unspecified`) is followed by another, as before. No releasable build after 5 (or a cap or stop before one) is
   an ERROR.
5. **The lock gate** (D777): just before the export, and again before the upload, the cycle checks that it still
   holds `lock.json`: the watchdog's heartbeat (every 5 min) has not found it lost, and `lock.json` read again
   names this cycle. When it does not, or cannot be read, nothing is released, whatever the build (`ok`, short only
   on collectors or on acquire's tail): `halted: lock lost …` (or `… not readable …`), ERROR, exit 1; a release
   already staged stays staged and is not uploaded (`upload --release-id` uploads it by hand once nothing else
   runs). Another cycle may hold the store then (e.g. after an `unlock` of a lock thought stale) and stage or
   upload the same release id. Otherwise: on an `ok` build, or one short only on collector lines or on acquire's
   retry tail within its limit (a WARN naming the tail: the last acquire run's rows due, roles left
   `failed_transient` and waiting on a `retry_after`,
   the halted lanes, the roles ok over the build's acquire runs, the tail's share of the rows acquire covers, and
   the stop when one came first; `STACK_SCHED_RELEASE_ALLOW_INCOMPLETE=0` turns both off): `stack export
   --config dataset-public` and `dataset-internal` of its corpus (`--allow-incomplete` for an incomplete build;
   `release.json` `build_status` then says `incomplete` with the short stages), release id = the export's UTC date, a
   scan for the bucket name, both bundles staged under the ops prefix with a manifest (sha256 per file) written
   last.
6. **Upload** of both variants to their Hugging Face repos (`hf_repo` of `configs/releases/dataset-*.yaml`). Both
   variants are first fetched back from the staging prefix (sha256 against the manifest, the bucket-name scan) and
   each is checked, every row, by the image's release checker (`data-repo/tools/release-validator.py <dir> --rows
   all`, run with `python -I` and only PATH, HOME and TMPDIR of the environment; the copy the variant ships must be
   byte-identical to it, else it is refused unrun; D804, D805): a variant it refuses (an unsupported format version
   included) uploads nothing and the cycle fails (ERROR). The internal variant's repo must be private, before and after: if it is not, nothing (more) is uploaded, the cycle
   fails (ERROR). The job never changes any repo's visibility; the public variant's repo is uploaded to whatever its
   visibility (it is public). Each variant is **one commit** that makes the repo
   tree exactly the release (files a release no longer has are deleted in the same commit; `.gitattributes` stays),
   made on the head read just before it (a concurrent push fails it), and **tagged `release-<id>`** (e.g.
   `release-2026-11-01`); earlier releases stay in the history under their tags. A tag that exists already (the same
   release uploaded again) is a WARN and is not moved.
7. **Website summary update** (D810): after the Hugging Face upload succeeds, when `STACK_SCHED_WEBSITE_UPDATE` is `1`
   or (`auto`, default) `WEBSITE_GITHUB_TOKEN` is set, the cycle runs the image's own reviewed builder
   (`tools/build_website_summary.py`, D805 precedent — never executing unreviewed code from the website repo) on the
   exact verified public release directory just uploaded to Hugging Face, in an isolated subprocess (`python -I`,
   scrubbed env without keys/tokens), validates `summary.json` and `summary.js` (schema, release id, bucket scan),
   shallow-clones `STACK_SCHED_WEBSITE_REPO` (`https://github.com/thesafetystack/website.git`, branch `main`;
   plaintext `http://` refused), refuses rollback of a newer live release id, re-checks `lock.json`, and commits and
   pushes only those two files when they changed (warning if the website repo's `tools/build_summary.py` drifted from
   the image copy). A website failure after the Hugging Face upload succeeded is a WARN (the dataset release is
   already live) and logs the manual retry command `python tools/scheduled_run.py update-website --release-id <date>`.
   Test cycles never push to GitHub.
8. Balance checks at the start and the end (below), then the summary (`cycles/<id>/summary.json`, `log.txt`,
   `cycles/latest.json`) on the ops prefix and on stdout, and **one email per cycle** (below).

Exit 0 ok, 1 failed, halted or upload refused, 3 incomplete (resumed by the next cycle), 4 refused. Caps (the
defaults below are the code's, `tools/scheduled_run.py` `Settings`; production currently sets
`STACK_SCHED_MAX_USD=50` and runs with no wall or build cap in effect):
`STACK_SCHED_MAX_USD` ($100, every job the cycle started: settled spend plus the open reservations of jobs that are
not live, D797; annotate lines get `--max-usd` of what is left; triage's
$20 per run cannot be lowered), `STACK_SCHED_MAX_WALL_H` (20 h: a Sunday 00:00 start ends by 20:00 UTC, before
Monday morning AoE); at a cap the running job is drained (one SIGTERM), cancelled after 60 min
(`STACK_SCHED_DRAIN_GRACE_S`, 3600: a drained build's final verify takes 29-38 min), and the cycle exits after
another 60 (a release export started after the stop has its own 120 min from its start, and is not signalled). A
build the cancel still cuts short prints no record (exit 130); the cycle reads it as stopped by its cap, not failed
(D781). The ops prefix is `s3://<bucket>/<store prefix>-ops` (`STACK_SCHED_OPS_URI`), outside the store.

## Modes

| Mode | What it does |
|---|---|
| `cycle --dry-run` | Reads only (S3 through a client that refuses writes before signing, `stack status`, `stack build --dry-run`); no LLM, no Hugging Face, no email; balances only with `--check-balances` (two read-only API calls). Prints the steps and every `WOULD REFUSE` problem; exit 4 when it would refuse. |
| `cycle --test` (or `STACK_SCHED_TEST=1`) | A real cycle on the configured store (its runs publish to the store and the next weekly cycle carries their work; they spend), with test caps unless set explicitly (1 build after build 1, $20, 6 h, no OpenAlex re-sweep), the release exported, checked and staged under `test/releases/`, **no Hugging Face call**, summaries under `test/cycles/` (`cycles/latest.json` untouched), alert emails tagged `[test]`. |
| `cycle` | The weekly production cycle. |
| `STACK_SCHED_UPLOAD=0` | A production cycle that stages its release but does not upload it. |

A test that must not write the production store needs another store URI; an empty store would collect the whole
history again, so there is no cheap isolated full test: `--dry-run` for wiring, `--test` for a real but unpublished
run.

## Email, severity, balances, watchdog

**One email after every cycle** (scheduled, manual or test; a dry run sends none), subject
`[test] OK|WARN|ERROR: Stack weekly cycle <outcome> (<cycle id>)` by the cycle's worst finding; the body lists every
error, then every warning, then the steps and, unless OK, the cycle's log tail. A crash of the cycle process mails
`ERROR: … CRASHED` before the traceback. Severity:

| Severity | When |
|---|---|
| ERROR | the cycle exits non-zero: refused to start (preflight, `hold.json`, lock held, a live job), halted (a failed run or build, no progress), no releasable build after 5 builds or a cap/stop before one (a tail build over `STACK_SCHED_TAIL_MAX_PCT`, or of unknown size, is not releasable), the lock lost or unreadable before the export or the upload (nothing released), export or upload failed (incl. the private repo not private, a staged variant its release checker refuses); or a balance at or below zero; or a run of the cycle stopped on its OpenAlex budget; or a crash |
| WARN | the cycle published, but: a build short only on collector lines, or only on acquire's retry tail within `STACK_SCHED_TAIL_MAX_PCT` (also after a cap, `hold.json` or a signal before the catch-up build), was released; a build had an unexpected incomplete reason (retried); a build was retried after a transient failure; the OpenAlex re-sweep failed (D789); the S2 collect stayed short, or a run of it had `failed_permanent` windows or a broken year-span upper bound (D802); a balance below its threshold or a balance check that failed; a re-upload found the release tag already there; a preflight warning (an optional key absent, no mail channel) |
| OK | none of these |

Channel: `STACK_SCHED_ALERT=resend` (Resend's HTTP API, `POST https://api.resend.com/emails`, Bearer key; free tier
3,000 emails a month and 100 a day; resend.com/docs/api-reference/emails/send-email,
/knowledge-base/what-is-resend-pricing; recommended by docs.railway.com/networking/outbound-networking). Use a key
with `sending_access` only. **DNS (the maintainers)**: add and verify `thesafetystack.ai` in Resend (Domains → Add, then the
DNS records it lists at the domain's DNS host) before alerts can go to `info@thesafetystack.ai`; until then Resend's
test sender reaches only the Resend account's own address. `STACK_SCHED_ALERT=smtp` (`STACK_SCHED_SMTP_HOST`,
`_PORT` 587 STARTTLS, `_USER`, `_PASSWORD`) is the alternative (outbound SMTP is Pro-only on Railway).

**Balances**, at the cycle's start and end (`STACK_SCHED_BALANCE_WARN_<SVC>_USD` thresholds):

| Service | Read how | Default WARN below | Auto-charge |
|---|---|---|---|
| OpenRouter | `GET /api/v1/credits` (total credits minus usage; needs a management key, `OPENROUTER_MANAGEMENT_KEY`), else `GET /api/v1/key` (the API key's own `limit_remaining`; SKIP when the key has no limit) | $50 | optional auto top-up (openrouter.ai/docs/faq) |
| OpenAlex | `x-ratelimit-prepaid-remaining-usd` of one free request (a single work), key as Bearer; ERROR also when a run stopped on its OpenAlex budget | $2 | none: prepaid in $1 steps, expires 3 months after the last purchase (help.openalex.org/access/buying-and-renewing) |
| OpenAI, Anthropic | no balance endpoint for an API key: SKIP | - | auto-reload optional in their consoles; set their own spend alerts |
| Semantic Scholar, arXiv, forums, blogs | free | - | - |
| Hugging Face | not checked (private storage free to 100 GB) | - | pay-as-you-go above it |
| Railway, the S3 bucket, Resend | postpaid card / free tier | - | Railway's usage limit caps it |

**Watchdog**: a second cron service, `python tools/scheduled_run.py watchdog`, Mondays 06:00 UTC. It reads the
production `cycles/latest.json` and `lock.json` (read-only) and mails `ERROR: Stack weekly cycle watchdog` when the
last summary is missing, finished more than 7 days ago (`STACK_SCHED_WATCHDOG_MAX_AGE_H`, 168), has severity ERROR,
or a lock is still held; silent otherwise. It catches what a cycle cannot report itself: an out-of-memory kill or
any SIGKILL (Railway: exit code 137), an image that does not build or start, a schedule that did not fire. Not
caught: Railway down as a whole (both services), the mail channel down, or the watchdog's own service broken.
Railway's own crash email (docs.railway.com/deployments/deployment-actions; email notifications on in Account
Settings) and project webhooks (/observability/webhooks) are a second, best-effort net.

## Before the first deploy (the maintainers)

1. **Railway Pro**, a 24 GB replica (a build at the store's size peaked at 13.6 GB RSS; verify reached ~18 GB before
   its build-scoped default; acquire runs 8 converters of 0.6-0.75 GB).
2. **Code pin**: a branch only reviewed commits reach, e.g. `deploy/railway`, fast-forwarded by hand. Railway builds
   the branch it is connected to; there is no per-commit pin. The image records `RAILWAY_GIT_COMMIT_SHA` as
   `STACK_CODE_COMMIT` (the release's `release_code_commit`).
3. **Keys of their own**, revocable without touching the laptop's: an OpenRouter key and an OpenAI key, each with a
   credit limit near the monthly ceiling (the only caps across cycles); an S3 key for the bucket; a Hugging Face
   token with write access to the two dataset repos only (it also creates tags); a Resend key with
   `sending_access`, and `thesafetystack.ai` verified in Resend (DNS); optionally an OpenRouter management key, only
   to read the account balance.
4. A Railway usage limit on the workspace (Usage → hard limit; minimum $10), and email notifications on.

## Railway setup

**Service `stack-cycle`** (New → GitHub repo → this repository, branch `deploy/railway`):

| Setting | Value |
|---|---|
| Builder | Dockerfile (found at the repository root). Check it after setup and after any change to the service: Railway once switched a service to Railpack, whose image has no tini, so the cycle ran without it |
| Start command | empty (the image runs `tini -- python tools/scheduled_run.py cycle`) |
| Cron schedule | `0 0 * * 0` (Sundays 00:00 UTC; Railway cron is UTC, 5 fields, may drift by minutes) |
| Restart policy | Never (a cycle resumes next week; a retry loop could pay twice) |
| Replica limits | 8 vCPU, 24 GB |
| Region | the one nearest the bucket's region |

Variables (sealed where marked: sealed values are hidden in the UI and the API, and not given to `railway run`):

| Variable | Value |
|---|---|
| `STACK_STORE` | `s3://${{stack-data.BUCKET}}/stack-store`: the bucket by reference to the Railway bucket service `stack-data`, so no variable text holds its name |
| `STACK_S3_ENDPOINT_URL`, `STACK_S3_ACCESS_KEY_ID`, `STACK_S3_SECRET_ACCESS_KEY`, `STACK_S3_DEFAULT_REGION`, `STACK_S3_BUCKET_NAME` | references to the bucket service's variables (`${{stack-data.<variable>}}`); the code does not read `STACK_S3_BUCKET_NAME` (the bucket comes from `STACK_STORE`). The code also accepts the older names `STACK_S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `STACK_S3_REGION` (a laptop's `.env`); per setting the new name wins (D726) |
| `TINI_SUBREAPER` | not needed: the Dockerfile sets `TINI_SUBREAPER=1` (tini registers as a child subreaper, so it still reaps orphaned converter processes when it is not PID 1, e.g. under a start command set in Railway); a Railway variable of the same value is redundant and harmless |
| `TMPDIR`, `HF_HOME`, `XDG_CACHE_HOME`, `STACK_TMP` | not needed: the image's working directory `/app` is not writable by its user, so every write goes under `/work` (Dockerfile): `TMPDIR=/work/scratch` takes Python's temp files and duckdb's spill directories (`stack/duck.py`, D732: duckdb's default `.tmp` under the working directory failed the 2026-10-27 test cycle's export), `HF_HOME=/work/cache/huggingface`, `XDG_CACHE_HOME=/work/cache`; `STACK_TMP` overrides the spill base only |
| `OPENROUTER_API_KEY` (sealed), `OPENAI_API_KEY` (sealed), `OPENALEX_API_KEY` (sealed), `SEMANTIC_SCHOLAR_API_KEY` (sealed) | keys; OpenAI's is for the roles llm.yaml routes to the OpenAI API directly (D714: technique_staged, model_roles); the preflight derives the LLM keys a plan needs from its pins and refuses a missing one |
| `HF_TOKEN` (sealed) | write to `thesafetystack/the-safety-stack` and `-private` |
| `WEBSITE_GITHUB_TOKEN` (sealed, optional in `auto` mode) | GitHub fine-grained personal access token scoped to `Contents: Read and write` on `thesafetystack/website` only; enables automatic weekly `summary.json` / `summary.js` updates after Hugging Face upload (D810) |
| `STACK_MAILTO` (sealed: a personal identifier) | the contact address |
| `STACK_SCHED_ALERT`, `RESEND_API_KEY` (sealed), `STACK_SCHED_ALERT_FROM`, `STACK_SCHED_ALERT_TO` | `resend`, the key, a sender on the verified domain, the recipients, e.g. `info@thesafetystack.ai` (comma-separated) |
| `OPENROUTER_MANAGEMENT_KEY` (sealed, optional) | reads the account's credits; cannot make completion calls |
| `ANTHROPIC_API_KEY` (sealed) | only when a plan pin routes to Anthropic (D722); the preflight says so |
| `STACK_SCHED_EXPECT_PLAN_DIGEST` | `ff4932034e2e136dd61eb6293d924555da22b9b2f1732f6914869a52173a1af7`: `batch-2026-10a` revision 12 (D800); a plan revision moves it (`python -c "from stack.plan import load_plan; print(load_plan('configs/plans/batch-2026-10a.yaml').plan_digest)"`), and the cycle refuses to start until the variable is updated. Earlier revisions' digests: `docs/repo-history/superseded-records.md`. A plan revision's operator steps (override imports): `docs/howto/full-run.md` |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | 3600: a redeploy SIGTERMs the running deployment with 0 s grace by default, then SIGKILLs |

Optional (code defaults in parentheses): `STACK_SCHED_MAX_USD` (100; production currently 50),
`STACK_SCHED_MAX_WALL_H` (20), `STACK_SCHED_MAX_BUILDS` (5) (production currently runs with neither in effect),
`STACK_SCHED_ARXIV_MAX_MIN` (45), `STACK_SCHED_OPENALEX_FULL` (`auto`/`1`/`0`), `STACK_SCHED_OPENALEX_FULL_DAYS` (6),
`STACK_SCHED_RELEASE` (`export`/`off`), `STACK_SCHED_UPLOAD` (`1`/`0`), `STACK_SCHED_WEBSITE_UPDATE` (`auto`/`1`/`0`; D810), `STACK_SCHED_WEBSITE_REPO` (`https://github.com/thesafetystack/website.git`), `STACK_SCHED_WEBSITE_BRANCH` (`main`), `STACK_SCHED_WEBSITE_GIT_NAME` (`The Safety Stack Bot`), `STACK_SCHED_WEBSITE_GIT_EMAIL` (`info@thesafetystack.ai`), `STACK_SCHED_RELEASE_ALLOW_INCOMPLETE` (`1`/`0`),
`STACK_SCHED_TAIL_MAX_PCT` (5, percent; step 4),
`STACK_SCHED_MEMLOG_S` (180: a `[mem]` line in the logs and in the cycle's `log.txt` every 3 min while a cycle runs,
the cgroup's memory and the three largest processes by RSS with their CPU, `tools/memlog.py`, D783, D784; not in the
emails' log tails; 0 turns it off),
`STACK_POOL_MAX_THREADS` (unset: each stage pool's threads are capped only by the container's task room less 128,
read from the cgroup's `pids.max`; a positive integer caps every pool at that many threads as well, for when the
limit is enforced where the container cannot read it, D786; the build child inherits it, the preflight refuses a
value that is not a positive integer),
`STACK_SCHED_BALANCE_WARN_OPENROUTER_USD` (50), `STACK_SCHED_BALANCE_WARN_OPENALEX_USD` (2),
`STACK_SCHED_MIN_FREE_GB` (30). Never set `STACK_ENV_FILE` there, nor any
`STACK_*_LIMIT`.

**Service `stack-watchdog`** (same repository and branch): start command
`/usr/bin/tini -- /app/.venv/bin/python /app/tools/scheduled_run.py watchdog`, cron `0 6 * * 1` (Mondays 06:00
UTC), restart Never, 1 vCPU / 1 GB; variables `STACK_STORE`, the S3 ones and the mail ones
(reference them from `stack-cycle`).

## Running by hand

- **The CLI**: `npm i -g @railway/cli`. Authenticate with `railway login`, or put a project token in `RAILWAY_TOKEN`.
  Then `railway link -p the-stack -e production -s stack-cycle` in the checkout, so that the commands below reach the
  service.
- **On Railway, now**: `railway cron run --service stack-cycle --yes` (an off-schedule run of the service's start
  command; `railway cron list --service stack-cycle` shows it). A run while another is active is skipped.
- **A dry run or a test on Railway**: set the start command to
  `/usr/bin/tini -- /app/.venv/bin/python /app/tools/scheduled_run.py cycle --dry-run` (or `cycle --test`), deploy,
  trigger as above, read `railway logs`, then clear the start command. Or a second service `stack-cycle-test` from
  the same repository and branch with that start command, the same variables (reference them from the first
  service) and a schedule that never comes (`0 0 1 1 *`), triggered only by hand. Do the dry run first after setup:
  every `WOULD REFUSE` line is a setup problem.
- **`railway run`** runs a local command with the service's variables, but sealed values are not provided to it, so
  it does not reach the keys; use it only for unsealed settings.
- **Locally, the same image**: `docker build -t stack-cycle .` then `docker run --rm --env-file <a file of the
  variables above> stack-cycle /app/.venv/bin/python /app/tools/scheduled_run.py cycle --dry-run` (or `--test`).
  docker reads the file into the container's environment only.
- **Locally, from a checkout**: `uv sync --frozen --group convert-pdf --group release`, then
  `STACK_ENV_FILE=.env STACK_STORE=s3://<bucket>/stack-store uv run --no-sync python tools/scheduled_run.py cycle
  --dry-run` (or `--test`). The keys are read from the file by name, as every `stack` command reads them.
- A staged release can be uploaded again with `python tools/scheduled_run.py upload --release-id <date>` (same
  private-repo check; never in test mode), and its website summary rebuilt and pushed with
  `python tools/scheduled_run.py update-website --release-id <date>` (`--dry-run` checks staging and token only).

## Operating

- **Watch**: `railway cron list --service stack-cycle --json` (status `EXITED`, `CRASHED`, `SKIPPED`, duration); the
  logs (30 days on Pro); from a laptop with the S3 settings: `stack --store s3://<bucket>/stack-store status` and
  `python tools/scheduled_run.py report` (the last summary). Each `stack` job's own log is in the store.
- **Pause**: `python tools/scheduled_run.py pause --note why` from a laptop: a running cycle drains its job within
  30 s and starts no further build or run; a tail build it already made is still released (step 4: the export and
  the upload); later cycles refuse (and alert) until `resume`. `stack stop` alone drains only the
  running job.
- **Upgrade code**: when `railway cron list` shows no running cycle, fast-forward `deploy/railway`; the next cycle
  runs the new image. A plan revision also needs `STACK_SCHED_EXPECT_PLAN_DIGEST` changed in the same step.
- **A cycle died** (container killed, out of memory): the next one refuses on `lock.json` and alerts. Check `stack
  status` (no job `running, live`), then `python tools/scheduled_run.py unlock --holder <id>` (the id is in the
  refusal). A job the container left `running, DEAD` is harmless; its work is redone
  (`docs/howto/container-lifecycle.md` §6).

Not done by a cycle, by design: `--new-series`, `stack overrides add`, plan or prompt changes, backfills, a convert
pin change, changing a Hugging Face repo's visibility, a hand-written run log (the summaries on the ops prefix
are the record).
