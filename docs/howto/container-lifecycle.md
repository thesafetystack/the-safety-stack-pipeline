# A build on a disposable container

A fresh VPS or container runs builds against an S3 store and is thrown away afterwards; nothing on it needs to
outlive it once `stack store push --check` passes. Background: `docs/OPERATE.md`, "The store, jobs and upload";
the store itself: `docs/howto/s3-store.md`.

## 1. Pull

```bash
git clone <repo> stack && cd stack && git checkout <sha>
uv sync --frozen --group convert-pdf   # acquire's converters; boto3 comes with the default dev group (s3)
                                       # with --no-dev: uv sync --frozen --no-dev --group convert-pdf --group s3
```

`.env` (copy `.env.example`): the S3 settings (`STACK_S3_ENDPOINT_URL`, `STACK_S3_ACCESS_KEY_ID`,
`STACK_S3_SECRET_ACCESS_KEY`, optional `STACK_S3_DEFAULT_REGION`; older names accepted, D726), the provider keys (`OPENROUTER_API_KEY`; optional `SEMANTIC_SCHOLAR_API_KEY`,
`OPENALEX_API_KEY`, `X_API_BEARER_TOKEN`) and `STACK_MAILTO`. The code reads them from the environment, else from the
file `STACK_ENV_FILE` names; do not source the file. The OpenRouter key's own credit limit is the only cap across
runs (each run is capped by its plan line).

Pull nothing from S3 up front: the first command refreshes the index, and data and blobs download when read. There
is no state directory and no ledger to carry over.

## 2. Run

Detached (tmux, or `systemd-run --user` below), from the checkout:

```bash
export STACK_ENV_FILE=.env
uv run --no-sync stack --store s3://<bucket>/<prefix> build <plan> --passes 6
```

A `systemd-run --user` unit starts in the home directory with the user manager's environment, not the shell's, so
the checkout, an absolute `STACK_ENV_FILE` and every other variable you exported are passed explicitly, and `uv` by
its absolute path (this template follows systemd's documented defaults; it was not run on a host with a user bus):

```bash
systemd-run --user --unit=stack-build --collect --working-directory="$PWD" -E STACK_ENV_FILE="$PWD/.env" \
  -p TimeoutStopSec=30min "$(command -v uv)" run --no-sync stack --store s3://<bucket>/<prefix> build <plan> --passes 6
journalctl --user -u stack-build -f     # its output; `systemctl --user stop stack-build` sends SIGTERM (a drain)
```

(`build` takes a plan name under `configs/plans/` or a plan file path; it reads and writes the plan's `series:`, or
`--series S`. The production plan `batch-2026-10a` runs as a fixed sequence of builds and standalone runs:
`docs/howto/full-run.md`. `--dry-run` first prints each line's `--in` paths and outputs and writes nothing.) Upload is automatic: every job PUTs its record, log and marker at start and every 30 s, each finished run
PUTs its data files and final record, and every blob goes up before a row names it. `incomplete` builds and
`--passes`: `docs/OPERATE.md`, "`incomplete` builds and resuming".

To stop early without losing work, **drain** (D570-D572; `docs/OPERATE.md`, "Stopping, cancelling and resuming"):
`stack --store s3://<bucket>/<prefix> stop` from any machine with the S3 settings, or one SIGTERM to the `stack`
process (`systemctl --user stop stack-build`, `docker stop -t 1800`). The run in flight stops taking new work,
gives what is in flight up to 120 s, publishes what it completed and ends `ok`; the build starts nothing more,
verifies and ends `incomplete` (exit 3) with `stopped` in its record; `stack build` again resumes. A supervisor's
SIGKILL must wait for all of that (the `TimeoutStopSec=30min` above; systemd's default is 90 s), or the job dies
mid-publish. To **cancel** at once (nothing of the run in flight published): Ctrl-C in tmux, a second SIGTERM 5 s or
more after the first, or `stack stop --now`; the run and build end `cancelled` and their end PUTs go out. A
`kill -9` leaves a dead job instead (below).

## 3. Watch

From a laptop with the same S3 settings:

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> status
```

A live job shows `running, live <age>` (its last tick); a build lists its unfinished runs under it, with their spend
(`$settled +$open open`). A run's log: `docs/howto/s3-store.md`, "Read it from a laptop". A `running, DEAD <age>` job
has not ticked for 5 min: see 6.

## 4. Discard

On the container:

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> status             # no job `running, live`
uv run --no-sync stack --store s3://<bucket>/<prefix> store push --check  # exit 0: nothing local is missing on S3
```

When `--check` lists records, run any uploading job (its recovery pushes `upload: true` final records) or
`stack store push <job>` for each, and check again. Then the container can go. Its `cache/llm-responses/` goes with
it: a later rerun pays again for LLM calls that only a lost cache entry held.

## 5. Disk

The raws and texts a container fetches stay in its mirror until it is discarded: ~100-150 GB for the backfill
(an estimate from the S3 store's design, not measured), less with the acquire option `arxiv_pdf: on_miss` (`docs/OPERATE.md`). Data files and blobs already on S3 can be deleted from
the mirror between builds to free space; they download again when read. Files of a no-upload job cannot.

Costs (estimates at Tigris prices from the same design, not measured): ticks ≈ $0.004/h with a build and a run live; blob PUTs
≈ $0.55 for the backfill; storage ≈ $1.1-1.6 per month; egress free.

## 6. When the container dies

1. Wait 5 minutes: the job's marker goes stale and `stack status` shows `running, DEAD <age>`.
2. Make sure the old container is stopped (the provider's console). A stale marker is not proof: a container that
   lost its network looks the same and may still publish when it comes back, which would fork a series
   (`docs/OPERATE.md`, "Two writers, forks and their repair").
3. Read what happened from a laptop: the dead job's record json (downloaded into the mirror by `status`, e.g.
   `v4/mirror/<bucket>/<prefix>/builds/<head>.<plan>/runs/<head>.<stage>.json.zst`; `zstd -dc`, or without the
   `zstd` tool `stack.runs.read_run_record` as in `docs/howto/inspect.md`, "3. Run records") holds its last
   tick's counts and spend; its log is fetched as in `docs/howto/s3-store.md`.
4. If the old disk survives, start the replacement on it: the first uploading job's recovery pushes any final record
   the dead machine wrote locally but did not upload. Otherwise start a fresh container (steps 1-2).
5. Rerun the build (or `--passes N`). Every run that finished before the crash is on S3 with its data, and the
   next build continues from each series directory's head, whichever build wrote it: collect, gather, triage,
   acquire and annotate work that finished is not redone (an annotate line carries the `<wf>` and `<wf>_evidence`
   files the dead build's run published). The run that died published nothing; its work is asked again (from the
   response cache if this machine still has it). The rerun's `fold.corpus` and `fold.build` write new files, since
   annotate and `fold.build` read only the corpus files of their own build. The dead run's last spend stays in its
   record (settled plus open), and the dead job stays `running, DEAD` in `status`: nothing finalises it.
