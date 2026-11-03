# Operate: running builds and stages

For setup (install, keys, spend caps, stores) see `docs/SETUP.md`; for reading the output, `docs/CONSUME.md`. Task pages
are in `docs/howto/`. `D<n>` refers to a row of `docs/DECISIONS.md`.

## Commands

`uv run --no-sync stack --help`. Global options `--store` (a path or `s3://<bucket>/<prefix>`; env `STACK_STORE`,
default `v4/store-dev` under the repository) and `--plan` (env `STACK_PLAN`, no default), given before the command name. Most commands also accept
them after their name; `stack <cmd> --help` says which. Data paths (`--in`, `--out`, `head`, `ls`, `chain orphan`)
are relative to the store: `data/<family>/<series>/` or an exact file under it. No command prints the S3 bucket: a
URI shows as `s3://<bucket>/<prefix>` and a mirror root as `…/mirror/<bucket>/<prefix>` (D540). Missing S3 keys, a
bad store URI or an S3 client error no command handles is an `error:` line, exit 1, not a traceback; S3 client text
is shown with the bucket and any URL masked (D544).

| Command | What it does | Plan from |
|---|---|---|
| `build PLAN` | runs the plan's stage lines as one build in the plan's series, verifies it, writes a build record | its argument (name under `configs/plans/` or a path) |
| `run STAGE` | runs one stage as a recorded run on the `--in` paths it is given (the same run a build line makes) | `--plan`/`STACK_PLAN` |
| `fold CONFIG --input KIND=…` | one fold (`corpus`, `build`, `release`, `compact`) as a recorded run; `--input` is required: `documents=DIR\|FILE` (corpus, compact) or `corpus=<file_id\|path>` (build: a `fold.corpus` file; release: a `fold.build` file); `corpus` and `build` need `--out DIR` (also `--restart`, `--comment`) | `--plan`/`STACK_PLAN` |
| `release --in DIR\|FILE` | alias of `fold release`: writes `releases/<tag>/` from a `data/corpus.build/<series>/` file (a directory: its last published file); no `--out` (D695) | `--plan`/`STACK_PLAN`, always |
| `export --in DIR\|FILE --out DIR` | the one local export (D647): the same release into the local directory `DIR` (absent or empty, outside the store; its name is the tag), no run record, never pushed; `--config` default `internal-v1` (`docs/CONSUME.md`, "Internal exports"); `--config dataset-public\|dataset-internal` writes the dataset release bundle instead (D719, `docs/howto/dataset-release.md`; `--release-id`, `--blobs DIR`) | none |
| `head DIR` | a series directory's last published file and its version (the newest full file, then its parts) | none |
| `ls [DIR]` | one series directory's published files in order (kind, base, run, digest), or every series directory with its head | none |
| `show ID` | one document (a `doc_id`, or a `source_id` of a member) from the series' heads: identity, metadata, members with their in-band entries, fetch roles, snapshots, the text pointer, annotations; `--json`, `--series`; `--record` or `--text` print only the provider record or the current text ("Inspecting a document and a build") | none |
| `show runs [BUILD]` | one build's runs: state, reasons, wall time, rows per minute, spend per model, totals; `--series`, `--counts`, `--json` | none |
| `verify` | the 47 checks of DESIGN Appendix B; exit 1 when a hard check fails | none (the reference build's own) |
| `diff A B` | per family of two builds: rows `+` added, `-` removed, `~` changed, `=` unchanged | `--plan` when A or B is `latest` |
| `status` | jobs newest first: state and liveness, spend, S3 state; free disk | none |
| `stop [--build ID \| --run ID] [--now] [--wait S]` | asks a running build or run, from any shell or machine, to drain (publish what it completed and stop) or with `--now` to cancel ("Stopping, cancelling and resuming") | none |
| `chain orphan PATH` | takes a published file out of its series directory (`--force`, `--note`) | none |
| `store push JOB`, `store push --check` | upload a finished local job; list local records S3 lacks | none |
| `overrides add FILE --out data/overrides/<series>/` | add human corrections to that overrides series (`docs/howto/overrides.md`); `--comment C` is the record's `comment` and ends the job's head (`runs/<YYYYMMDDTHHMMZ>_<run_id>_<C>.overrides.add.json.zst`) | none |
| `migrate upcast/rekey` | materialise format upcasts (optional: reads upcast on their own); re-keying `sources` under new URL rules | none |
| `acquire reopen --in DIR` | makes failed fetch roles of a documents version due now, after an outage | `--plan`/`STACK_PLAN` (not for `--dry-run`) |
| `eval …` | prompt and model evaluations on frozen sets (`evals/`), not on a store's corpus | `--plan` for `triage`, `screen`, `affiliation`, `model_roles`, `technique_roles`, `technique_staged`, `audit-prompts` (none for the draft `affiliation_orgs`: `tools/org_vocab.py`; none for `translation`) |

Stages are `workflows/<phase>/<name>/main.py` (stage id `<phase>.<name>`): `collect.*` (arXiv sweep, OpenAlex,
Semantic Scholar, the forums, LW/AF GraphQL, blogs, cards, feeds, manual, links, the OpenAlex/S2/card-dates/X
lookups, v3 import), `gather`, `triage`, `acquire`, `annotate.*` (screen, affiliation, model_roles,
technique_roles, technique_staged, affiliation_orgs: model-free, D736; translation: D755) and `fold.*`. `echo_test` is a test stage.

## A build

```bash
export STACK_ENV_FILE=.env STACK_PLAN=configs/plans/<plan>.yaml
uv run --no-sync stack build <plan> --dry-run     # the plan's series:; each line's --in paths, outputs, kind, caps
uv run --no-sync stack build <plan> [--series S] [--skip 'collect.*'] [--max-usd annotate.screen=0.5] [--passes 2]
uv run --no-sync stack head data/corpus.build/<series>/   # the last fold.build file of the series
uv run --no-sync stack release --in data/corpus.build/<series>/ --config public-v1 --tag <tag>
```

A build is a runner and nothing more. It runs the plan's lines in order, each as a recorded run with the `--in` and
`--out` paths a user could pass to `stack run` by hand. The paths come from the line's series: the plan's
`series:`, or `--series S` over it, or a line's own `out: {series: …}`; a line with none is refused
(`stage_refused`). Each line reads the series directories its stage reads by default (the table in
`configs/plans/README.md`, "Stage lines"), and annotate and `fold.build` read the exact corpus and annotation files
this build wrote. Each run pins the directory heads current at its own start, so a line reads what the line before
it published. There is no prior build: the next build, of this plan or of another plan with the same series,
continues from each directory's head, whichever build or standalone run wrote it. `fold.compact` runs first when
`documents/<series>/` is due (`versions.compaction`).

**The series is document identity** (D491). A collector reuses a source's `source_id` only from its own chain in
the series it writes (`sources.<p>/<series>/`), else mints a new one, and a `doc_id` is its anchor member's
`source_id`. A build into a series with no `documents` file therefore re-mints every id: it re-collects the whole
window and re-pays every triage, screen and annotate call and every fetch, and the other series' overrides do not
apply. So a build whose series has no `documents` file while another series of the store has one is refused before
any record (exit 1; the error names the series that have `documents`), unless `--new-series` is given: pass it
once, on the first build of a deliberately new series.
A build into an empty store, or into a series that has `documents`, needs no flag; `--dry-run` prints the verdict
as its `new_series` line. A `stack run collect.<p>` with no `sources` prior of its own, into such a series, is
refused the same way and takes the same flag. There is no tool that copies or renames a series.

Each stage writes its output files only at its end. A cancelled run leaves a `cancelled` record and nothing a
record names in `data/` (its staged `.<name>.tmp` output is garbage), and a rerun hits the LLM responses cached in
`cache/llm-responses/`; a drained run publishes what it completed ("Stopping, cancelling and resuming"). On an S3
store each run's data and record go up as it finishes ("The store, jobs and upload").

Build options, all recorded in the build record (`series`, `note`, `overrides`, `warnings`):

- `--series S` sets the series of every line that does not name its own.
- `--new-series` allows a build into a series with no `documents` while another series has some (above).
- `--skip PATTERN` leaves out the lines whose stage matches (fnmatch).
- `--filter STAGE=SQL` narrows a stage (not `fold.*`, not `triage`).
- `--max-usd STAGE=USD` lowers an `annotate.<wf>` line's `budget.max_usd` for this build (not triage's: its
  budget is part of its config digest).
- `--passes N` runs up to N builds in a row, each continuing from the heads the one before published; pass k+1
  starts only after pass k ended `ok` or `incomplete`. It stops early after a pass with no call and no changed row
  (`converged`, or `stopped: no progress` when that pass was `incomplete`, e.g. a run's budget is reached), and
  after a pass a drain stopped (`stopped: drained`). Every
  stage carries its `ok` rows, so a later pass calls only for new rows and for failures that are due again (a
  transient failure's first retry is due at once, later ones back off 2 h, 4 h, …).
- `--verify-full`: the verify at the build's finish is the full one (below), not its structural check and the
  check of its own files and build set.
- `--note TEXT`: free text in the build record; `--comment C` (`[a-z0-9-]+`) ends the build's head.

`build --dry-run` writes nothing. It prints a header block (`plan`; `series`; `compaction`: `due (<why>)`,
`not due (head <file>)`, or `-` with no `documents` file; `new_series`: `-`, `allowed by --new-series: …` or
`error: refused: …` (the series guard); `preflight`: `-`, or one `error: environment: …` line per build-start
refusal), then one line per plan line with the columns `stage call run inputs outs kind
filter max_usd max_rows repeat`, then warnings. `call` numbers repeated lines of one stage (`fold.corpus` 0 and 1);
`run` is `run`, `skipped` (`--skip`) or `error: …` (the line would be refused); `inputs` lists each `--in` path and
the file it resolves to now (`→(empty)` for an empty series directory, `<this build: …>` for a file this build
writes); `outs` lists the line's explicit `--out` paths (`-`: the outputs default to the input directories). It does not estimate rows to ask or cost.

The build record's `files` lists this build's own outputs (the last per family); `files.corpus` is its `fold.build`
file. `version_chain` is the walk of this build's last `documents` file back to its newest full file.

## `incomplete` builds and resuming (D265)

A stage that ends `ok` with `scope_complete = false` (its `budget.max_usd` or per-run default, `max_rows`,
`max_minutes`, `batch_wait_minutes` or a failed batch under `llm_mode: batch` (reason `unanswered`), held or
failed-transient collector windows, a collector window a cap cut short, a convert stop, an OpenAlex per-run cap,
an `arxiv_pdf: on_miss` PDF made due after `quality` (reason `pending`, D482)) does not stop the build. Every line
runs, then verify; if a stage writing a family in the plan's `require_complete` stopped short, the build ends
**`incomplete`**, its record lists `incomplete: [{stage, run_id, reasons}]`, and `stack build` exits **3**. So `stack build … && stack release --in …` never releases an `incomplete` build's corpus by accident. A stage line with
`require_complete: false` (the production plan's `collect.s2-lookup`) is exempt: its shortfall is listed under
`incomplete_allowed`, warned, and retried next build, and the build can still end `ok`.

**Acquire's clocks** (D392, defaults D483). `budget.max_minutes` stops acquire's fetch; convert then runs on a clock
of its own, started after the fetch: `options.convert_max_minutes` minutes, absent the same `budget.max_minutes`, and
`options.convert_max_rows` rows, absent no row cap (whatever `max_rows` is). So a run can take up to **twice**
`budget.max_minutes` (batch-2026-10a: 45 min of fetch plus up to 45 min of convert, up to 9 h per build at `max_runs: 6`),
and a fetch stopped by its budget or by `max_rows` still converts what it fetched and every pending in-band body
in scope. Set either convert option to bound convert tighter; an explicit value wins.
**A halted run ends the repeat**: a run that ended `halted` (acquire: a lane's provider halt, `halted.<lane>`) with
fewer than 10 items ok (`outcome.ok`; `stack/build.py` `HALTED_REPEAT_MIN_OK`) is not repeated within the build
(a build warning names it); the build goes on and ends `incomplete` (`halted`), and the next build asks its rows
again. A halted run that still completed 10 or more is repeated.

The record's `reasons` per stage (`stack/build.py` `INCOMPLETE_REASONS`, read from the run's counts): `budget`
(an LLM line's `budget.max_usd`, S2's page budget, an OpenAlex or X spend cap), `limit` (a row or result cap:
triage's or acquire's `max_rows`; **a collector window cut short by a cap**, D492: `max_records`, a provider's own
cap such as forums' 10,000-hit result window (D642) or GitHub's truncated tree, cards' `max_urls`/blogs' `limit`, arXiv's
per-query cap, S2's `max_pages`), `time` (acquire's or convert's `max_minutes`), `halted`, `held` (a provider
`retry_after`; acquire: a lane's provider halt, e.g. arXiv's fourth limited answer in a row, D584),
`windows_failed` (transient collector windows or lookup batches), `unanswered`, `not_run`,
`pending` (v3_import's next slice; an `arxiv_pdf: on_miss` PDF made due after `quality`, D482), `no_credentials`,
`stopped` (a drain stopped the run, D570: next section), `tier2_allowance` (screen: a borderline whose tier-2 call
did not fit the line's tier-2 allowance, counted `not_attempted.deferred`, asked again next run; D706), else
`unspecified`. A capped collector window is `partial`
(counted `windows_capped`); in a later pass within `relist_after_h` (default 12 h) the same unit is not asked again
(it would truncate the same way) and is counted `capped_not_due`: the run stays short, so a build over a capped
query stays `incomplete` (and `--passes` stops on `no progress`) until the cap is raised or the window split.

- To resume, rerun the build, or use `--passes N`: the next build reads the same series directories, whose heads
  include what the `incomplete` build's runs published, carries the `ok` rows and asks only the rest.
- `stack release --in` (and `stack fold release`) releases a corpus file only when an `ok` build record names it
  (that build is the release's `build_id`); a corpus only an `incomplete` build names needs `--allow-incomplete`;
  one no build record names (a standalone `fold.build` run) needs `--allow-unbuilt` (`build_id: none`); one only
  `failed` (`failed[verify]` included), `cancelled` or `running` builds name is refused whatever the flags (D494).
  The refusal comes before any run; otherwise one stderr line names the build released and its status
  (`release of corpus <id>: build <build_id> (ok)`). `latest` means one build everywhere (D703): the newest
  finished one, `ok` or `incomplete` (never `failed`, `cancelled` or `running`): `stack diff latest …` (of `--plan`'s
  plan), `stack verify --build latest` and verify's default build (of the store; with `--in`, of that series). Release
  and export never read it: they take a corpus file, and the gate above still refuses an `incomplete` build's corpus
  without `--allow-incomplete`.
- `stack head` is not build-aware: it names a directory's last published file, which a run of an `incomplete` or
  `failed` build may have written (every finished run publishes, whatever its build ends as). A consumer who needs a
  verified build reads a build record (`docs/CONSUME.md`).
- A run that ends `failed` still fails the build at once.
- A collector window that failed transiently makes the collector run incomplete (the next pass retries it); a
  permanently failed window keeps the scope complete and becomes a build warning.

## Stopping, cancelling and resuming (D570-D573)

A build or run can be stopped two ways. Neither loses a finished run's work; a **drain** also keeps what the run in
flight completed.

| To | Do | The run in flight | The build |
|---|---|---|---|
| **drain** | `kill -TERM <pid of the stack process>` once, or `stack stop` | admits no new work, gives the work in flight up to 120 s (`stack stop --wait S`), publishes what is done, ends `ok` with `scope_complete = false`, reason `stopped` | starts no further line, repeat or pass; verifies; ends `incomplete` (exit 3) with `stopped` in its record |
| **cancel** | a second SIGTERM (5 s or more after the first), Ctrl-C (SIGINT), or `stack stop --now` | publishes nothing; ends `cancelled[operator]` | ends `cancelled[operator]` |

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> stop                     # drain every live build and standalone run
uv run --no-sync stack --store s3://<bucket>/<prefix> stop --build <build_id>  # one build (its running run drains with it)
uv run --no-sync stack --store s3://<bucket>/<prefix> stop --run <run_id>      # one run (in a build: the build stops after it)
uv run --no-sync stack --store s3://<bucket>/<prefix> stop --now               # cancel
```

**`stack stop`** (D572) needs no pid and works from any machine with the store's settings: it writes a stop request
`<stem>-stop.json` (a build: `build-stop.json`) beside the job's `-running.json` marker, locally and, on an S3 store
for a job that uploads, as an S3 object at the same key. The job reads it at its next tick (every 30 s: the local file,
then a HEAD of the S3 key), logs `stop request (drain)`, and removes it at its end (with the marker DELETE, the only
objects a job deletes). A finished, dead or unknown target is refused before anything is written. A job running with
`--no-upload` reads only its local file: stop it on its own machine. **SIGTERM** (D570) drains the same way at once;
it reaches the process directly (`kill -TERM` to the `stack` pid). A SIGTERM to a whole process group reaches `stack` twice under `uv run`, which forwards it; a second SIGTERM within
5 s of the drain's start is taken as the same stop, a later one cancels.

**What a drain keeps** (D571): everything the run completed is published as at a normal end, blobs first (D407).
`acquire`: every role fetched (its raw snapshot and the role's state), so the next run fetches none of them again;
it does not start `convert` after a drained fetch, and the next run converts what this one fetched (scope clause
`convert`, D403); a drain during `convert` keeps the conversions done. `triage` and `annotate.*`: every answer
received; the pairs or rows not asked are `not_attempted[stopped]` (counts `ask.not_attempted.stopped`,
`not_attempted.stopped`) and the next run asks exactly those. Collectors and lookups: every window or batch closed
(`ok` with its watermark, or failed as it failed) and every observation made; no window, page or batch is started.
**What it drops**: work still in flight when the wait ends is abandoned as not attempted: a lane call under
`fetch_workers > 1` (`drain.abandoned`; its role stays due), a conversion (`convert.abandoned`; the snapshot is put
back uncharged), an LLM call (`drain.abandoned`, `ask.drain.abandoned`; the row `not_attempted[stopped]`; the call
was sent, so it may be billed, and that cost is then not in the run record's `spend`, whose open reservation is
released at the run's end; an answer that still lands goes to the response cache, which the next run reads free), and a collector window whose later page was not asked (no coverage row: uncharged, the watermark does not
move, the next run asks the window again; its observations so far are kept). With `fetch_workers: 1` the one call
in flight ends on its own timeout. The abandoned calls' threads end on their own timeouts; the process may wait for
them before it exits. `llm_mode: batch` lines are not interrupted inside their batch phase (`batch_wait_minutes`
bounds it); their replay then admits nothing new. gather and the folds run to their end.

**How long it takes.** Up to 30 s for a stop request to be read, up to the wait (120 s) for the work in flight,
then the publish (blob uploads first) and, for a build, verify over the store and the build record. At the
production store's size verify takes about half an hour for a build with triage, acquire and the annotate lines (it
grows with the build's acquire runs, each a full `documents` file), 8-15 min for a build of collectors and gather. A
supervisor that escalates to SIGKILL must allow for all of it:
`systemd-run -p TimeoutStopSec=60min …` (the default is 90 s), `docker stop -t 3600`. Or drain with `stack stop`
and stop the supervisor when `stack status` shows the job finished. A SIGKILL leaves a dead job (below).

**Resume**: run the same `stack build <plan>` (same series) again. It continues from each directory's head like any
build after an `incomplete` one ("`incomplete` builds and resuming"): every line runs, carrying `ok` rows. The
stopped build's record has `stopped: {by, requested_at, wait_s, runs_drained, not_run}` (`by`: `signal` or
`request`; `not_run`: the plan lines, not `--skip`ped, with no run in it). Its corpus, if it wrote one, is released
only with `--allow-incomplete`, as any `incomplete` build's (D494). `stack status` shows it `incomplete[stopped]`,
and a drained standalone run `ok[stopped]`. A drained standalone `stack run` exits 0 (its record is `ok`); rerun it
to continue. A cancelled run or build is resumed the same way; the cancelled run's own work is redone.

## Runs stand alone

A run reads only the paths it is given and writes where they say. A build passes the same flags, so any build line
can be rerun by hand as the same run (`stack build --dry-run` prints them). `stack run` needs `--plan` for the stage
line's options and pins; a stage not in the plan runs with a bare stage line and a warning. A standalone run's
record is under `runs/`.

```bash
export STACK_PLAN=configs/plans/batch-2026-10a.yaml        # its series: batch-import
uv run --no-sync stack run triage --in data/documents/batch-import/ --in data/triage_evidence/batch-import/
uv run --no-sync stack run fold.corpus --in data/documents/batch-import/ --out data/corpus/batch-import/
uv run --no-sync stack run annotate.screen --in data/corpus/batch-import/<name>.full.parquet \
      --in data/screen/batch-import/ --in data/screen_evidence/batch-import/     # outputs default to the prior dirs
uv run --no-sync stack run acquire --in data/documents/batch-import/ --only point   # substages: the stage's SUBSTAGES
```

**Inputs.** `--in` is repeatable; nothing is selected by plan, build or family:

| `--in` value | Means |
|---|---|
| a series directory `data/<family>/<series>/` | its last published file, resolved as a version (walked back to the newest full file) |
| an exact file `data/<family>/<series>/<name>.parquet` | that file's version, walked back from it if it is a part |
| an exact file `data/<family>/<name>.full.parquet` | a file without a series |
| `data/<family>/` alone | refused |

- A series directory with no published file is an empty prior when the stage reads that family as a prior (a
  series' first run); otherwise it is refused.
- An answers file and the evidence file read with it (`screen` + `screen_evidence`, `<wf>` + `<wf>_evidence`) must
  come from the same run, by the run id in each resolved file's name; a mismatch is refused.
- Overrides are read only when given (`--in data/overrides/<series>/`); none given means none, and the record's
  `in_files` shows it. A run of gather, `annotate.<wf>` or `collect.manual` that gives none, into a series whose
  `data/overrides/<series>/` has a file, is refused before it starts unless `--no-overrides` (D495; a build passes
  that directory whenever it has a file).
- The run record pins every input in `in_files` (`path`, `file_digest`).

**Outputs.**

- **Where.** `--out data/<family>/<series>/` (or `data/<family>/` for a file without a series), one per family;
  else the directory of the run's input of that family (its prior); else `data/<family>/<--series>/`. An output
  family with none of these is refused, and so is a path naming a series together with a different `--series`.
- **Kind.** Full by default. `--part` (plan: `out: {kind: part}`) writes a part on the directory head the run read.
  A stage that emits parts (the collectors, triage, acquire) writes, without `--part`, the full its part makes on
  the version it read (counted `materialized.<family>`); the production plan writes full files on every line
  (`out: {kind: full}`, so a reader of a series directory takes one file), the other plans say `out: {kind: part}`
  on those lines. A stage that emits full files (gather, the folds, annotate) refuses `--part` into a non-empty
  directory (no full-to-part diff is computed). `corpus`, `<wf>`, `<wf>_evidence` and `triage_evidence` files are
  always full. A file without a series is always full.
- **Base.** An output's base is the file the run read from the destination directory: the head it pinned, or the
  exact file it was given from that directory. An output into a directory the run did not read has no base. A run
  given an exact file that is not its directory's last, of a family the stage writes and not redirected by `--out`,
  is refused before it starts: writing back there would fork the directory.
- **Restart.** A full without a base into a non-empty directory needs `--restart` (plan: `out: {restart: true}`)
  when the stage reads that family by default; otherwise it is refused, before the run for an explicit `--out`.
  This catches a run that left out its destination's prior (gather without `--in data/documents/<series>/`), which
  would hide the series' history from every later reader. fold.corpus (it does not read `corpus`) and fold.build
  (exempt: it reads only this build's corpus file) write a base-less full every time; fold.compact `--out` into
  another non-empty documents directory needs `--restart`. The first file of an empty directory needs no flag.
- **New series.** A `collect.*` run that pins no `sources` prior of its own and writes into a series with no
  `documents` file while another series has one is refused before the run (it would mint every `source_id`
  afresh); `--new-series` allows it (D491; a build passes it to its runs after asking the same of its series).
- `--comment C` (`[a-z0-9-]+`) is a label in the record and in every output's name, never a selector; `--note TEXT`
  is free text in the record.

The run prints one JSON line: `run_id`, `status`, `error_class`, `scope_complete` and each output's path.

Examples (`<S>` = `--store`; `weekly` is an example series, not one the plans in the tree use):

- **Rerun one build line:** `stack run triage --in data/documents/batch-import/ --in data/triage_evidence/batch-import/`
  is the run the production plan's triage line makes.
- **Prompt experiment seeded from weekly:**
  ```bash
  stack --store <S> run annotate.screen --in data/corpus/weekly/<corpus>.full.parquet --in data/screen/weekly/ \
        --in data/screen_evidence/weekly/ --out data/screen/exp-2/ --out data/screen_evidence/exp-2/ --comment v13
  ```
  It writes fulls without a base into `screen/exp-2/` and `screen_evidence/exp-2/`, with weekly's answers as priors;
  later steps read `data/screen/exp-2/`. Weekly is untouched.
- **Experiment without a series:** `stack run gather --in data/documents/weekly/ --in … --out data/documents/
  --comment exp-2` writes `data/documents/<utc>_<run>_exp-2.full.parquet`. Nothing reads it unless named exactly.
- **Reproducing an old state:** pass exact files, e.g. `stack run fold.corpus --in
  data/documents/weekly/20261007T080000Z_r1.part.parquet --out data/corpus/repro-1007/`. Any old run's record lists
  the concrete inputs to copy (`in_files`).
- **Compaction:** `stack run fold.compact --in data/documents/weekly/` writes a full whose base is the head, so the
  next reader stops walking at it. A build runs it first when `versions.compaction` says it is due.
- **Forking a series:** `--in data/documents/a/ --out data/documents/b/ --full` writes a full with no base, the first
  file of `b` (into a non-empty `b` it is a restart and needs `--restart`).

## The store, jobs and upload

How a store is laid out, how a job writes to it and what reaches S3 (D320-D366; the series layout,
D372-D375). Task pages: `docs/howto/s3-store.md` (a store on S3), `docs/howto/container-lifecycle.md` (a run on
a disposable machine).

### Stores and the mirror

`--store` is a path (a local-only store) or `s3://<bucket>/<prefix>` (an S3 store; D321). An S3 store is worked on
through its **mirror**, `$STACK_MIRROR/<bucket>/<prefix>/` (default `v4/mirror/` under the repository,
git-ignored), which holds the same keys as S3. The first command naming the URI creates the directory with its
binding `.stack-mirror.json` = `{"uri": "s3://<bucket>/<prefix>"}`; the binding is never uploaded and never changes
(another URI there is refused). A mirror named by its path is refused (`use --store s3://…`), so nothing writes into
it as a local-only store. A bucket may hold several stores, one prefix each. One store per run; `--in` and `--out`
are relative to it.

A mirror file is present when it was downloaded or written there; an absent one is downloaded when read (data files
and blobs to a temp file, checked against their recorded sha256, then renamed into place). A file that is on S3 can
be deleted from the mirror at any time.

Credentials: `STACK_S3_ENDPOINT_URL`, `STACK_S3_ACCESS_KEY_ID`, `STACK_S3_SECRET_ACCESS_KEY`, optional
`STACK_S3_DEFAULT_REGION` (the older names `STACK_S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`STACK_S3_REGION` still accepted, D726), from the environment or else the dotenv file `STACK_ENV_FILE` names, passed to
boto3 explicitly (never its ambient chain).
The bucket and prefix come from the URI. A URI that resolves to the read-only v2 bucket is refused.

Layout, the same on S3, in a mirror and in a local-only store (a job's `<head>` = `<YYYYMMDDTHHMMZ>_<id>[_<comment>]`,
its start minute; a build's runs carry no comment):

```
builds/<head>.<plan>/build.json.zst, build.log.zst, build-running.json      a build
builds/<head>.<plan>/runs/<head>.<stage>.json.zst, .log.zst, -running.json  its runs
runs/<head>.<stage>.json.zst, .log.zst, -running.json    standalone: stack run, release, migrate upcast,
                                                         chain orphan, overrides add
data/<family>/<series>/<utc>_<run>[_<comment>].full.parquet   a full file in a series
data/<family>/<series>/<utc>_<run>[_<comment>].part.parquet   a part on the previous file of the directory
data/<family>/<utc>_<run>[_<comment>].full.parquet            no series: read only by its exact path
blobs/<sha256>                                           fetched bytes and converted texts, flat
releases/<tag>/                                          release.json last
cache/llm-responses/, cache/provider-responses/          local only
```

Data files are written at publish and never changed or deleted. Never on S3: `cache/`, `*.tmp`,
`.stack-mirror.json`. There is no `store.json`, no lock file, no ledger file, and no state directory a run writes
to.

### Data files: series and chains

**Names.**

- `<family>` is the family directory: the family name, with the provider for the per-provider collector families
  (`sources.af_graphql`, `coverage.af_graphql`) and a suffix for the two other corpus directories. In use:
  `sources.<p>`, `coverage.<p>`, `documents`, `triage_evidence`, `corpus` (fold.corpus), `corpus.everything` (the
  diagnostic fold.corpus), `corpus.build` (fold.build), `<wf>`, `<wf>_evidence`, `overrides`.
- `<series>` is `[a-z0-9][a-z0-9-]*`, e.g. `batch-import`, `exp-2`, `legacy`. A series need not be a plan's name
  (D378): the production plan `batch-2026-10a` writes `batch-import`, which a later dated plan can continue; the
  other plans in the tree use their own name.
- `<utc>` is `YYYYMMDDTHHMMSSZ`, one stamp per run: its start floored to the second, moved to one second after the
  newest of its input files and of each destination directory's last file when it would not sort after them, so
  every output sorts after every input the run read and after the file it lands behind (D381). The record's
  `started_at` stays the true start; the name's stamp only orders. Two concurrent runs can share a second; their
  names then order by run id.
- `<run>` is the run id; `<comment>` is the run's `--comment` (`[a-z0-9-]+`), a label only, never a selector.
- The writer stage is in the record and the footer, not in the name.

**Footer and record.** Each file's `stack.*` footer carries, besides the keys of DESIGN A.6, `series` (empty without
one), `comment`, `kind` (`full` or `part`), `base` (the base file's name in the directory; empty for a full without
one) and `after` (the directory's last published file when the run pinned its inputs; empty for a directory's first
file). The run record's `outputs` lists each file with its path, `file_digest` and the same fields.

**Published.** A data file is published when an `ok` run record lists it by path and digest and no `excludes`
record (`stack chain orphan`) takes it out. Every reader ignores every other file.

**A chain** is the published files of one series directory, in name order.

- The **head** is the last file. A **version** is the newest full file at or before a file plus the parts after it;
  a reader resolves it by walking back from the file to that full.
- A **part**'s base is the previous published file of its directory. A part is never a directory's first file, and
  a run writes a part only into the directory whose last file it read.
- A **full with a base** extends the series; its base must be the previous file. This catches a lost update: a full
  computed from an older head.
- A **full without a base** is a series' first file or a deliberate restart (`--restart`, "Runs stand alone").

**The publish rule**, one rule for every write into a series directory, checked against a refreshed index:

- the directory's last published file must still be the new file's `after` (so a part's or an extending full's base
  is that file too); otherwise the run ends `failed[head_moved]`;
- the new name must sort after it; otherwise `failed[name_order]`;
- when the directory's last file is already this run's own output, an earlier attempt committed (a lost
  acknowledgement) and the publish is done (recovery and `store push`).

A file without a series must only not exist yet.

**Which store's view.** An uploading run resolves its inputs and checks the publish rule against the records on S3
(its index counts uploaded records only, D353); a no-upload run against the mirror's. A mirror can be ahead of S3
until `stack store push`.

**Two series of one family** (each line a published file):

```
data/documents/a/20261014T080000Z_r1.full.parquet                    full, no base
data/documents/a/20261014T090000Z_r2.part.parquet   base …r1.full    part
data/documents/b/20261014T100000Z_r3.full.parquet                    full, no base (b's first file)
data/documents/b/20261014T110000Z_r4.part.parquet   base …r3.full    part
```

- `--in data/documents/a/` resolves to r1 ← r2, and `--in data/documents/b/` to r3 ← r4.
- r4 cannot name r2 as its base: a base is always in the same directory.
- r3 forked b from a's state: a run given `--in data/documents/a/` and `--out data/documents/b/` writes a full with no
  base. Into a non-empty b it would be a restart and need `--restart`.

### Upload

Upload is yes or no per job (a build or a run), fixed at its start and recorded as `upload`; a build's runs
inherit the build's. On an `s3://` store the default is yes. `stack build … --no-upload` and `stack run …
--no-upload` write the job's records, log and data only into the mirror; `stack store push` uploads it later. The
other writing verbs (`fold`, `release`, `overrides add`, `chain orphan`, `migrate upcast`, `migrate rekey --apply`,
`acquire reopen`) have no `--no-upload` and always upload on an S3 store.
Blobs are PUT by every job, no-upload ones too (content-addressed; an unreferenced blob is harmless). An uploading
job reads only the records that are on S3, so S3 never depends on a mirror-only file.

Data and release files are PUT with `If-None-Match: *` (create-only): a retry never overwrites one, and a 412 is
accepted only when the object's sha256 equals the record's digest (otherwise the run fails). Record jsons, logs and
markers are PUT unconditionally: a job PUTs its record json at one key at start, at every tick and at the end, and
only that job writes the key (its run id is unique).

### Jobs: record, log, marker and tick

A job owns three files: its record (`….json.zst`, `build.json.zst`), its log (`….log.zst`) and its marker
(`…-running.json` = `{"tick": "<utc>"}`). The order (D352-D355):

1. **Prepare.** Refresh the index (LIST `builds/` and `runs/`, GET the record jsons that are new or changed) and,
   when uploading, run recovery (below). This comes before the inputs are pinned, so the pins see the directories as
   S3 has them (D353). An S3 failure here that the client's retries did not
   get past (connection, timeout, 5xx, throttling) leaves no record: a build ends `failed[s3_transient]`, `stack run`
   exits 1 with `error: [s3_transient] ...` (D461).
2. **Start.** Write the `running` json, the log and the marker locally and, when uploading, PUT them, before any
   work. A failed start PUT ends the job `failed[tick_failed]`.
3. **Tick**, every 30 s on a thread: rewrite the json locally (counts and spend so far; a build: its runs so far),
   then PUT json, log, marker. A tick never writes a final json. When ticks fail **continuously** for 2.5 min,
   counted from the first failed tick (D493), the job cancels itself: it ends `cancelled[tick_failed]` without
   publishing (the flag is checked where a stage reports progress, in annotate's and triage's ask loops after each
   round, between a build's stages, and before the publish rule is checked; never after it). A good tick ends the
   streak and lifts a cancel no check has acted on yet, so one failed tick after a laptop suspend, or an S3 outage
   shorter than 2.5 min, cancels nothing; once a check has acted on it the cancel stands. Each failed tick is a
   `warning: … tick failed …; retrying` log line and counts in `tick_errors`. A build whose run cancelled this way
   ends the same.
4. **Publish** (an `ok` run): the cancel check; the publish rule per output directory against a refreshed index
   (`failed[head_moved]`, `failed[name_order]`); rename each `data/<family>/<series>/.<name>.tmp` into place, never
   clobbering; PUT the data files (create-only; a transient S3 failure is repeated after 5, 15 and 45 s, then the
   run ends `failed[s3_transient]`, D461); then the final record. That record PUT is the commit point on S3,
   and since one record lists all of a run's outputs, a run with several outputs commits them at once. A finished
   run's data is therefore on S3 even when its build later fails. A release run PUTs `releases/<tag>/` after the
   rename, `release.json` last.
5. **End** (every outcome): stop and join the tick thread, write the final json locally, then PUT json, PUT log,
   DELETE marker (the only delete a job makes on S3). A failed end PUT is a warning: the final record is local, and
   the next uploading job's recovery, or `stack store push`, sends it.

If the final record cannot be written after the rename, the renamed data files stay: no `ok` record names them, so
they are unpublished (garbage, an X1 warning locally; on S3 invisible). A drain or a cancel ends the job through the
same end steps ("Stopping, cancelling and resuming"); a tick also reads the job's stop request. A build started
detached from a non-interactive shell (`setsid nohup … &`) inherits SIGINT ignored, so `kill -INT` does nothing to
it; stop it with SIGTERM to the `stack` process, or `stack stop`.

**Liveness** (D344), read from the index: a final json means finished, whatever marker is beside it; a `running`
json with a marker younger than 5 min is **live**, with an older one **dead**, without one **unknown**. The marker's
age is the newer of its local mtime and its S3 `LastModified`. A stale marker is a heuristic, not proof that the
process is gone (a lost network looks the same). Nothing finalises a dead job: its last json is its last state.

### Logs

One zstd log per job, kept in memory and written whole at each tick and at the end (D357). A build's log holds only
the orchestrator's lines: a start line, one line per stage run (`build <id>: <stage> run <run_id> <status>`) and an
end line. Each run's lines (progress, warnings, batch ids) go to that run's log. The `stack` console script captures
stderr, subprocess and converter output included, into the active job's log while still printing it. Read one with
`zstd -dc <file>.log.zst`, or without the `zstd` CLI through the venv's `zstandard` (a record `….json.zst` the same
way, or as a dict with `stack.store.index.read_json_zst(path)`):

```bash
uv run --no-sync python -c "import sys, zstandard; sys.stdout.buffer.write(zstandard.ZstdDecompressor().stream_reader(open(sys.argv[1], 'rb')).read())" <file>.log.zst
```

Reading one from another machine: `docs/howto/s3-store.md`; other ways to look into a store: `docs/howto/inspect.md`.

### Recovery

At the start of every uploading job (D353), every local `upload: true` record that is **final** while S3 lacks its
json or still holds its marker (a crash or a failed PUT between the local final json and the marker DELETE) is pushed
in publish order: the publish rule is checked again against S3, then the data files it names that S3 lacks
(create-only), the json, the log, then the marker DELETE. Runs go before builds, each by `finished_at`. No GET is
needed: a job PUTs its marker before any work and deletes it only after its final json and log are up, so "json on
S3, no marker" means the final json is there. A local `running` record is never pushed (a run that died before its
local final record is dead, and its files stay unpublished). A record naming a data file that is neither local nor
on S3 is a warning and is skipped. The one case recovery misses (the start's marker PUT failed and the final json
PUT failed too: a job that did no work) is listed by `stack store push --check`.

### `stack store push` and `--check`

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> store push <job>    # a finished local job
uv run --no-sync stack --store s3://<bucket>/<prefix> store push --check  # what discarding this machine would lose
```

`<job>` is a run or build id, a run stem (`<head>.<stage>`) or a build directory name (D358); a build id names the
build (with its runs), never one of its runs, and a run id names that run only (D420). A run is pushed in
publish order (data files, json, log, marker DELETE); a build pushes its runs in start order, then its own json and
log. A record whose S3 copy already has its status is skipped (`<job>: already on S3` when nothing was pushed). It is
refused (`error: …`, exit 1, nothing of the refused run uploaded) when the job is still `running`, when a run's
inputs are not named by `ok` records on S3 (push those jobs first), or when the publish rule fails on S3: an output
directory's last uploaded file is no longer the output's `after` (`head_moved`: another job published there
meanwhile) or does not sort before it (`name_order`). An output that is already its directory's last file on S3 is
done (the record reached S3 before; a lost acknowledgement). A refused record stays local and `--check` lists it;
its work is redone by a new run against the current head. A record is pushed as written: a no-upload job keeps
`upload: false` on S3.

`--check` GETs the S3 copy of every local record and prints one line per record whose copy is absent or of another
status (`<key>: local ok (upload job), S3 running`), for upload and no-upload jobs alike, then a count; exit 1 when
there is any. With nothing missing it prints `nothing local is missing on S3` and exits 0. On a local-only store both
`push` and `--check` are refused (`error: …`, exit 1): there is no S3 to push to or compare with.

### Two writers, forks and their repair

There are no locks (D355, D356). The rule is **one writer per series** at a time. Two runs that
both pass the publish rule before either commits both commit.

- **Detection.** A reader walks from the requested file back to the newest full file (that full included) and
  checks that each file's `after` is its predecessor in name order (empty only for the directory's first file).
  Otherwise it stops with `ForkError` naming both runs, or `MissedFileError` when `after` names a file no longer
  published. A restart records `after` too, so two racing restarts, or a restart racing a part, are caught as well.
  Links older than that newest full are not checked: a restart or a compaction ends the history a reader validates.
  A build reading such a directory ends `failed[chain_error]`; `stack ls <dir>` shows the error; verify's V11
  reports it.
- **Repair.** `stack chain orphan <path> --force` on the unwanted branch (a standalone `chain.orphan` run whose
  record `excludes` the file; nothing is moved or deleted; `--force` for a file that is not the directory's last).
  Orphan the pair sibling too (an answers file and its evidence file come from one run; the pair check refuses
  until both are out). Then rerun what wrote it, or write a new full.
- An orphaned file mid-chain makes the next file fail the reader's `after` check loudly (it is no longer the
  predecessor): orphan the files after it too, or restart the series with a full.
- Before starting a replacement for a job that looks dead, make sure the old container is stopped: a stale marker
  does not prove it is.

Two plans on one series (a collect-only plan and an acquire-only plan, both `series: weekly`) extend the same
directories when run one after the other. Run concurrently, they are two writers on `documents/weekly/`: the later
publish refuses `head_moved` when it checks after the first committed, and the reader's `ForkError` catches the case
where both passed the check before either committed.

A build runs its lines one after another; readers (`status`, `head`, `ls`, `diff`, `verify` from a laptop) never
conflict with a writer. Concurrency inside a stage (worker threads per stage line) is in `docs/howto/concurrency.md`.

### Consistency assumption

The index refresh before pinning and before the publish rule assumes read-after-write LIST and GET on the bucket
(the production provider, Tigris, gives this within one region; writers run in one region). A view from another region may lag briefly, which
is harmless for reads. If a lagging LIST ever let a writer publish on an old head, the result is a fork that the next
reader reports, not a silent loss.

### Spend in run records and per-run caps

Each run's record holds `spend` (D328, D364), written at every tick and in the final record:

- `usd_micro`: settled micro-USD per `openrouter/<model>`, `x`, `openalex`;
- `reserved_open_usd_micro`: open reservations of calls and batches in flight;
- `batch_ids`: batches in flight; `abandoned_batch_ids`: batches abandoned at the wait cap, billed at the run's
  actual/reserved ratio, and batches whose poll answered 404/410, charged at their full reservation (D366, D370).

A dead run's spend is its last tick's settled plus open amount. Every paid call reserves against an in-memory ledger
of its run first. Caps are per run: an LLM stage's is its line's `budget.max_usd`, else `configs/llm.yaml`
`budget.per_run_usd`; X and keyed OpenAlex at most $10 per run (lowered by `STACK_X_CAP_USD`,
`STACK_OPENALEX_CAP_USD`; X also by the line's required `budget.max_usd`; OpenAlex by the line's `options.max_usd`,
default $0.50, lowered by `budget.max_usd` when set, and `options.max_requests`). There is no cap across runs; the
provider keys' own credit limits are the outer bound. `stack status` shows each job's spend from the records (a
build's is the sum of its runs'); it prints no total across jobs. A total for a period is the sum of every job's
`spend_usd_micro` (settled) and `open_usd_micro` (open reservations; for a dead job, part of its spend):

```bash
uv run --no-sync stack --store <store> status --limit 0 --json | python3 -c '
import json, sys
since = "2026-10-01"   # the period start (UTC), compared with each job started_at
jobs = [j for j in json.load(sys.stdin)["jobs"] if j["started_at"] >= since]
usd = sum(j["spend_usd_micro"] + j["open_usd_micro"] for j in jobs) / 1e6
print(f"${usd:,.2f} over {len(jobs)} jobs")'
```

`jobs` holds builds and standalone runs. A build's runs are counted in its own entry and are not jobs of their own,
so nothing is counted twice; the `active_runs` listed under a running build repeat part of its spend and are left
out. The scheduled cycle's spend cap (`STACK_SCHED_MAX_USD`, `docs/DEPLOY-railway.md`) counts this total over the jobs
the cycle started, less the open reservations of live jobs, which their own per-run caps bound (D797).

### What a crash leaves, and why it is safe

Readers go through records, so none of these is ever read as data:

- `data/<family>/<series>/.<name>.tmp`: a run's staged output (a run that did not publish). Garbage; safe to delete.
- `releases/.<tag>.<run_id>.tmp/`: a release being written. Same.
- `.<file>.<pid>.<…>.tmp` or `.<file>.<pid>.<thread>.dl` beside a file: an atomic rewrite or a download cut short.
  Ignore or delete.
- A `data/` file no `ok` record names: a crash after the rename or the data PUT, before the final record. A run
  that died before writing its local final record is dead (a `running` record is never pushed), so its files are
  unpublished; on S3 they are invisible. `stack verify`'s X1 lists local ones as a warning; safe to delete locally.
  The retry is a new run with new names.
- A `running` json with a stale marker: a dead job; its json is its last state. Rerun what it was doing.
- A final local record whose PUT failed: recovery or `stack store push` uploads it after checking the publish rule
  again ("Recovery", "`stack store push` and `--check`"). Retry or push, never both.
- A data PUT retried after a lost acknowledgement never overwrites ("Upload").

### `stack status`

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> status [--limit N] [--json]
```

Reads the index only (D365); on a mirror it refreshes it first (one LIST of `builds/` and `runs/`, GETs of record
jsons only). One line per job, builds and standalone runs, newest first (`--limit`, default 20, 0 = all): start
minute, kind, id, plan or stage, state, spend and, on a mirror, the S3 state. A build lists its unfinished runs under
it. For example (a local-only store):

```
store /path/to/store
jobs: 1 (0 live, 0 dead, 0 unknown, 1 finished)
  20261014T0035Z  run    r3rpts3mehemj  echo_test               failed[code_bug]            $0.00
free disk 209.9 GiB
```

- state: `ok`, `incomplete`, `failed[<class>]`, `cancelled[<class>]` (`incomplete[stopped]`: a build a drain
  stopped; `ok[stopped]`: a drained standalone run), or for a `running` record `running, live 40s`,
  `running, DEAD 7m` (the marker's age) or `running, unknown` (no marker);
- spend: `$<settled>` plus `+$<open> open` when reservations are open; a build sums its runs' records;
- S3 state (a mirror only): `s3`; `local-only` (an `upload: false` job not pushed); `push pending` (an
  `upload: true` final record whose json S3 lacks or whose marker S3 still holds: recovery or `stack store push`
  sends it).

Then a `warning: repeated_inflight (X11)` line for each key in flight in two dead runs, and the free disk. Exit 0
whatever it finds; an unreadable record or a missing store is an `error:`, exit 1. Garbage and lost data files are
`stack verify`'s X1, missing blobs its X5.

### Inspecting a document and a build

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> show <doc_id|source_id> [--series S] [--json]
uv run --no-sync stack --store s3://<bucket>/<prefix> show <source_id> --record | jq .   # the provider's record, as stored
uv run --no-sync stack --store s3://<bucket>/<prefix> show <doc_id> --text > doc.md       # the current text, as stored
uv run --no-sync stack --store s3://<bucket>/<prefix> show runs [<build_id>] [--series S] [--counts P,…|all] [--json]
```

Both are read-only (D541-D543): nothing is written, created or uploaded.

`stack show ID` reads the head version of `data/documents/<series>/` and of every `data/sources.<collector>/<series>/`
of the series (`--series`, default the store's only `documents` series). On a mirror the version's data files are
downloaded first, as any reader does; in-band entries and snapshots are listed with their BLOBs (media type, size,
sha256) as the rows record them (D690), with no blob lookup, and only `--text` and `--record` download blobs. `ID` is a `doc_id` when the view has it (also a retired or masked row), else every document whose
members include it, else a source row no document holds yet; otherwise `error:`, exit 1. For example (the synthetic
test world: a LessWrong post with its AlignmentForum crosspost, found by the crosspost's id; trimmed):

```
data/documents/main/ head 20261013T073000Z_r00000000000y.part.parquet  query s00000000000p (matched by member)
document s00000000000n  state acquired  created 2026-10-12 09:30:00  updated 2026-10-12 09:30:00
  anchor s00000000000n  channels alignmentforum,lesswrong  keys forum_post:abc123
metadata
  title: Sandbagging evals: what we found
  …
members (2)
  s00000000000n  lesswrong  external_id abc123  anchor  live
    matched_by kw:group:evals, feed:lesswrong  source row data/sources.lesswrong/main/
    in-band:
      post             raw text/html 9.4 KiB 5ce54cc5101a  text text/markdown 9.4 KiB f4a368225afc
      provider_record  raw application/json 75 B 315edccbf9d3
  s00000000000p  alignmentforum  external_id abc123  key:forum_post->s00000000000n  live
    …
fetch roles (2)
  s00000000000n  post         lane inband   ok                attempts 1  snapshot r00000000000m.11
  …
current text: ok  media md  snapshot r00000000000m.12  source s00000000000p  role post  digest 08d12e4578c9  size 9.4 KiB  quality -
annotations (3)
  triage           ok                     decision fetch; wanted_by screen; safety_relevance=relevant (0.80)
  screen           ok                     verdict relevant (0.85); buckets evals
  affiliation      ok                     1 item(s): A. Author -> example-lab
```

A fetch role's reason is its `error_class` and `error`. `--json` prints the report as data with full digests and payloads.

`--record` and `--text` print only the blob's bytes as stored, nothing else, so they pipe (D545); give one of them, and
not with `--json`. `--record` is the record of the source named (for a `doc_id`, its anchor member): its newest in-band
`provider_record` entry, on the document row or the member's sources row (D546; a file written before that role reads
with its record upcast to that entry, D524); to see a crosspost's other record, name that member's `source_id`. `--text` is the current text of the one document
the id names; an id that several documents hold is refused (pass the `doc_id`). Either is `error:`, exit 1, when there
is nothing to print.

`stack show runs` reads the store index only (a mirror's is refreshed first, as `stack status` does). The build is the
given id or `builds/` directory name, else the newest build of `--series`, else the newest of the store, whatever its
status. One line per run in start order: stage, run id, state, `scope_complete`, reasons (a failed run's class; an
`ok` run that stopped short: the build's incomplete reasons, `limit`, `budget`, `time`, …), wall time, rows touched,
rows per minute and spend; then the run's counts whose key starts with one of the `--counts` prefixes (default the
usual rate counters, `all` for every count) and its spend per model. For example (a test fixture; the acquire run is a
running record whose marker last ticked a minute after its start, two days before the command ran):

```
build w000000000002  plan dev-v2  series main  running  started 2026-10-12T02:30:00  finished -  wall -
  incomplete: triage run r00000000001y (limit)
  stage                      run            state                      compl    wall     rows  rows/min     spend  reasons
  collect.arxiv              r00000000001x  ok                         yes      2.0m      120      60.0     $0.02  -
      counts: http.200=10, requests=10
      spend: openalex $0.02
  triage                     r00000000001y  ok                         no       5.0m       50      10.0     $1.50  limit
      counts: answered=48, asked=50
      spend: openrouter/m/a $1.25, openrouter/m/b $0.25
  annotate.screen            r00000000001z  failed[code_bug]           no       1.5m        -         - $0.50 +$0.10 open  code_bug
      error: ValueError: boom
      spend: openrouter/m/a $0.50
  acquire                    r000000000020  running, DEAD 2d14h        no       1.0m        -         -     $0.00  -
totals: 4 run(s) (1 failed[code_bug], 2 ok, 1 running); runs' wall 9.5m; build wall -; rows 170; spend $2.02 (+$0.10 open)
  openalex                                      $0.02
  openrouter/m/a                                $1.75
  openrouter/m/b                                $0.25
```

A running run's wall time runs to its last tick. `--json` carries every count unless `--counts` is given.

The default counts include the arXiv lane's (D584), so "called" and "requested" can be told apart on an acquire run:
`lane_calls.arxiv` counts every role handed to the lane; `arxiv.requests` and `arxiv.html.requests` the PDF and HTML
requests it made (retries included), `arxiv.downloads`/`arxiv.html.downloads` what they minted; `arxiv.atom_requests`
the Atom pages asked. arXiv's 429/503 is waited out (60, 120, 240 s; `arxiv.limited_waits.atom`,
`arxiv.limited_waits.web`, `arxiv.limited_wait_s`); the fourth in a row gives up: for Atom `arxiv.atom_limited` 1
and every paper whose version is still unknown answered without a request (`arxiv.limited_roles`, roles
`failed_transient[rate_limit]`, due again next run; `html` roles first ask the unversioned HTML URL,
`arxiv.html.unversioned`, D585), for the PDF/HTML host `halted.arxiv` and `halted_roles` (the lane halts for the
run).

## Verify

`stack verify` runs the 47 checks of DESIGN Appendix B (S: what a writer refuses, V: per family and row, X: over a
build's set of files) over the store and the newest finished build (`ok` or `incomplete`, never `failed`); `--in
data/<family>/<series>/` (repeatable; X1 still
runs over all), `--names` narrow it, `--json` prints every item. Nothing carries over from one verify to the next.
What runs:

- **Default, a build's check** (`stack verify`): every check on the files the newest finished build's runs wrote (the
  file checks, the pair checks against their base views, V7, V8), and the build checks (X2-X25) on that build.
  `--build <id|path|latest>` (`latest`: the newest finished build, `ok` or `incomplete`, the default's build; D703)
  or `--series S` (its newest finished build) picks another build; `--run <run_id>`
  (repeatable) checks those runs' files instead; `--no-build-set` leaves the build checks out.
- **Structural** (`stack verify --structural`; every build at its start and its finish): file damage over the whole
  store, without reading a data file in full: S1 from each file's footer, V11 (every chain resolves; keys unique in
  the files from its newest full file to the head), X23 (every base a published file of its directory), X1 (every
  recorded file present, its footer's row count the record's, on a mirror its size the S3 object's; no re-hash) and X5
  (every referenced blob listed, a raw snapshot's `bytes` its size). About 20 s plus one S3 LIST of `blobs/` (about
  70 s on the production store). A failure at a build's start ends the build `failed[verify]` before any run
  (`verify_start` in the record).
- **Full** (`stack verify --full`; `stack build --verify-full` at the finish): every check on every file, and every
  local data file re-hashed against its record. About 2 h and 3.6 GB at the production store's size: run it
  explicitly, after changing a check, the registry or a schema, and before a release, never while a build runs.

A check of a part that did not run prints `skipped[structural]`, `skipped[files]` or `skipped[build_set]`. The last
line says what ran (`mode: files+build_set (...)`, `mode: structural (...)`, `mode: full (...)`); the build record's
`verify.mode` and `verify_start.mode` too. A same-size corruption of a data file, or a file an edited check would now
flag, is found by the next full verify only.
`verify` on a store that has no builds skips the build checks (`skipped[no_builds]`), so an
empty directory passes (exit 0): check `--store` when a verify looks too quiet. A path that does not exist is refused
(`error: no store at …`, exit 1; D423).

**X6** (`policy_pins`) checks annotation evidence against the prompt pin its run recorded (`prompt_pin` in the
`annotate.<wf>` run record). A plan or `configs/llm.yaml` edit since then shows as an X6 `warn`, not a failure; the
next build re-asks. Runs recorded before that field existed are judged by today's pin only when its config digest
equals the file's header, else `warn`.

## The convert pin and the venv

The convert pin hashes the plan's convert config and the installed converter versions (D9, D250). Any `acquire`
run (any `--only`, `point` included) first checks that the venv has the config's tools: `pymupdf4llm-v1` needs `uv
sync --group convert-pdf`, `docling-v2` needs `--group convert`. Without them the run ends `failed[environment]`
(a build stops with the same class) instead of re-converting every stored text at a degraded tier. `stack build`
checks this before its first line (the build-start preflight; `--dry-run` prints a `preflight error:
environment: …` line), so it does not surface after the collectors. Escape hatches:
`options.allow_degraded_convert: true` on the acquire line (runs at the degraded pin, counted
`convert.degraded_allowed`); and the optional plan field `acquire.convert.expect_pin` (a pin prefix; a mismatch
refuses and prints the pin document) can be changed or dropped. A changed pin re-converts every current text on the
next `acquire`, and screen and affiliation then re-run on the new texts.

Structured text (JATS XML, arXiv LaTeXML HTML, the PDF as fallback) is the acquire option `structured: {jats,
arxiv_html}` (D276; on in batch-2026-10a). Turning it on changes the pin and so re-converts once; a plan that turns it on
must widen `content.current_role = 'full_text'` filters to `IN ('full_text', 'xml', 'html')`. With `arxiv_html` on,
every member whose key carries an arXiv id (an `arxiv` key or a `10.48550/arxiv.<id>` DOI) wants `html`, whichever
channel found it (OpenAlex and S2 papers too, not only the `wants` channels), one fetch per arXiv id.

Which arXiv PDFs are fetched is the acquire option `arxiv_pdf` (D480-D482; `workflows/acquire/carry.py`, "arXiv
PDF"). It covers every `full_text` role on lane `arxiv`, so OpenAlex and S2 papers keyed by an arXiv id or DOI too;
`wants` alone cannot stop those. The production plan sets `on_miss`.

| `arxiv_pdf` | fetched | the current text of an arXiv paper |
|---|---|---|
| `always` (default; absent or null) | the PDF of every id, and its HTML with `structured.arxiv_html` | the HTML when its text is `ok` and at least half its PDF's length (same revision); else the PDF |
| `on_miss` | the HTML; the PDF only where the HTML is a miss: no `html` role, arXiv has no acceptable page (`empty`), the role failed for good, or its text failed conversion or the quality gate. Not a miss: a withdrawn version, or HTML still pending | the HTML when its text is `ok` (no PDF to be `too_short` against); on a miss the PDF, fetched by the run after the one that found the miss |
| `never` | the HTML only | the HTML when its text is `ok`; else another member's text or the abstract (`no_raw` without one) |

`on_miss` and `never` need `structured.arxiv_html: true` (refused otherwise, as is any other value). A PDF not requested
is `skipped[not_requested]` with `rule = arxiv_pdf` on the role (the rule recognises its own skips by it, D690) and,
for humans, error `arxiv_pdf: never` or `arxiv_pdf: on_miss, <why>`; PDFs held from an earlier
setting stay and the pointer still ranks them. Under `on_miss` a PDF role already `ok` is left alone while the HTML is
pending (a merged member's `html` not fetched yet) and set aside only once the HTML is `ok` or withdrawn (D643). Under
`on_miss` a run that finds a miss ends `scope_complete = false` (count `arxiv_pdf.due_next_run`, build reason
`pending`), so the build's acquire repeat fetches the PDF in its next run. **Adding PDFs later**: set `arxiv_pdf:
always` (or drop the key); the next acquire makes only the skipped PDF roles due, asks no HTML again, and moves a
paper's current text only where the HTML is now `too_short` against its PDF. The convert pin does not change.

An HTML-only paper is a full member of the corpus: `has_text`, the corpus and `excluded_reason` read the current
text pointer, screen takes the corpus, the production plan's affiliation filter takes `current_role IN ('full_text', 'xml',
'html')`, and `acquire_pending`/`acquire_failed` do not count a `skipped[not_requested]` PDF as open work. Triage
treats an `ok` arXiv `html` role as the primary `full_text` when the plan's acquire line sets `on_miss` or `never`
(D485), so a `gate:` edit does not re-ask such papers and X14 agrees. Plans with an affiliation filter of
`current_role = 'full_text'` (std-v2, dev-v2, dev-v2-m4, ann-scratch) would leave HTML-current papers out; none of them turns
on the arXiv HTML.

## Failed fetches and `acquire reopen`

A fetch role is quarantined (`quarantined[cap_reached]`) at its fourth counted failure since its last `ok`
(`stack/fetchcycle.py` `FETCH_CAP`), backing off between them (the next run, then 2 h, 4 h, … up to 7 d, or a later
`Retry-After`). A rate limit (`failed_transient[rate_limit]`: a host's 429 after the retries, or at once when it asks
for a longer wait than the session sleeps through (120 s in all per request, D588), an OA host's or S2's
limit, a paper left unanswered once the arXiv lane gave up on Atom, D411, D584) is not counted (D484): the role backs off (its `Retry-After`,
else the backoff its counted failures give, else the next run), its `attempts` stays, and it is never quarantined
by rate limits alone, so a long throttled fetch holds rows back without losing them. Other transient failures
(`http_5xx`, `timeout`, `network`, `forbidden`, …) count.

Roles `quarantined[cap_reached]` and `failed_permanent` roles of class `dead`, `blocked` and `no_fetchable_url` are
due again 90 days after they ended (`stack/fetchcycle.py` `TERMINAL_RECHECK`); arXiv's `atom_unknown`, a 200 that is not
a PDF and a DNS failure are transient until the fetch cap. After a known outage:

```bash
uv run --no-sync stack acquire reopen --lane arxiv --in data/documents/<series>/ [--error-class dead] [--since 2026-10-01] [--filter SQL] --dry-run
uv run --no-sync stack acquire reopen --lane arxiv --in data/documents/<series>/ …   # one acquire run with options.reopen; counts reopen.roles
```

## After a family format change

Adding a registry entry or a field changes the format of the families that carry it: a new version in
`stack/schemas/upcasters.py` (`docs/howto/change-family-format.md`, D295; amends D43). Every reader reads older files
**upcast to the current version**, a chain may mix versions, and the next writes put current-version files on top:
nothing has to be migrated unless a step is `Incompatible` (for example "Blob-reference upcast (D691, removed by D693)" below). A file's format version is its footer's `stack.family_version`; `stack release`/`stack
fold release` print a `note:` when they read an older corpus. A file that does not read (newer than the code, an unknown format, behind an `Incompatible` step) is
refused by name, with the step's reason.

A migration is optional; it materialises the current version:

```bash
uv run --no-sync stack migrate upcast --family documents [--dry-run]          # each series dir: a fold.compact full on its head
uv run --no-sync stack migrate upcast --family documents --in data/documents/main/   # only these series dirs (repeatable)
uv run --no-sync stack migrate upcast --family screen_evidence --file <file_id>   # always-full families: a new file
                                                                                  # (and its pair sibling, one run)
```

Run it to drop the per-read upcast cost of a large older version or to hand duckdb consumers current files (the old
files stay: nothing deletes data files). The dry run lists per file `v<k> → v<N>` and the step kinds. An older or a
migrated evidence file both work as `--in` (V8 accepts an evidence file written by `migrate.upcast`). Upcasting one
file of an answers/evidence pair upcasts its sibling (the same old run's file of the other family) in the same run,
one record listing both, so the pair rule of `--in` holds (D380). `--file` must name its directory's current last
published file, and its sibling too, neither orphaned: the new file goes after that head, so an older file would become
the series' newest (refused, dry run included; D421). `corpus`
files are never migrated (the next `stack build` writes a current one).

### Blob-reference upcast (D691, removed by D693)

`documents`/`corpus` files before v6 and `sources` files before v4 (D690) record no size for converted texts and
in-band bodies and do not read: `stack/schemas/upcasters.py` keeps their version entries behind `Incompatible` steps,
so every reader refuses such a file by name (`IncompatibleFormat`, S1 in `stack verify`). Every store's files were
rewritten once by a one-off tool (D691), since removed (D693). Stored triage gates of builds made before D690
(`blobs/<gate_digest>`, read only by X14) name the column `inband_text_digest`; `stack.gate.load_gate` reads it as
`inband_text_sha256`.

## Keeping answers across a config change (accepted configs)

A change to an annotate pin, a prompt, a vocabulary file or a triage question's pin moves the config digest, and the
next build asks every document again. When the change does not change the answers (the same request bytes, D738's
1.1.0 envelope) or is not retroactive, the line can accept the earlier answers instead (D758; `docs/CONCEPTS.md`
"Accepted configs"):

1. Find the digests and names: `uv run --no-sync python tools/list_configs.py --store <store> --plan <plan>
   annotate.<wf>` (or `triage`) prints the configs the stage recorded with their names, the rows of the latest build
   under each, the plan's current one (`CURRENT`) and the line's selection digest.
2. Write the line's `target` (the config its pin gives now) and its `accepted:` list, with a decision row in
   `docs/DECISIONS.md`:

   ```yaml
   - stage: annotate.technique_staged
     # ... the line as before ...
     target: <the current config digest, 64 hex>
     accepted:                           # preference order: the first with a decided answer wins
       - {config: <an earlier config digest>, name: technique-staged-v5-a1.1@techniques-1.0.0,
          decision: D758, note: "same request bytes"}
   - stage: triage
     target: {safety_relevance: <question digest>}
     accepted: [{question: safety_relevance, config: <earlier question digest>, name: safety-relevance-v3, decision: ...}]
   ```

   The plan load refuses a malformed list (another stage, malformed digests, the target in the list, a config twice,
   a triage question the gate does not ask). The plan digest moves (a scheduled run needs the new
   `STACK_SCHED_EXPECT_PLAN_DIGEST`); no config digest does.
3. Build with low caps first. The build refuses at its start (and `stack build --dry-run` lists under `preflight`) a
   `target` that is not what the pin gives: change the pin later and the list is not inherited unseen. At stage start
   each accepted config must be one the stage recorded (annotate: a run record's `prompt_pin` with the same workflow,
   registry version and produced records, and the `name` `tools/list_configs.py` prints; triage: the prior
   `triage_evidence`, `name` its prompt name), else the stage refuses before any call. The run counts
   `carried.accepted` (rows kept) and records its `selection` (target, list, selection digest); `stack verify` checks
   the kept rows against their own runs' pins (X6) and reselects with the recorded selection (X8). The release ships
   each kept config's taxonomy version beside the current one and states per stage `release.json` `stages`.

Accepted rows count as `carried`. To have some documents asked again, there is no list: drop the entry (every document
without a decided answer under the target is asked) or leave it (none is).

## URL rules and re-keying

`stack/urls.py` holds every URL rule (`fetch_url`, `url_key`, `link_keys`, `alternate_url_keys`); duplicate
detectors are in `stack/identity.py`. After a rule change, re-key the stored `sources` rows and gather again:

```bash
uv run --no-sync stack migrate rekey [--report FILE]   # dry run: rows re-keyed, merges and splits a gather would make
uv run --no-sync stack migrate rekey --apply           # one migrate.rekey part per changed sources series dir
uv run --no-sync stack run gather --in data/sources.<p>/<series>/ … --in data/documents/<series>/
```

See `docs/howto/duplicates-and-urls.md`.

## Environment overrides

Several collector options can also be set by environment variable (`STACK_COLLECT_LIMIT`, `STACK_ARXIV_FROM`,
`STACK_OPENALEX_FULL`, …; `.env.example`), and **the variable wins over the plan line's `options`**. None of them
enters the config digest. **A cap or sampling variable (`STACK_*_LIMIT`, `STACK_*_FRACTION`, `STACK_*_MAX_QUERIES`)
that is set makes `stack build` and `stack run collect.*` refuse to start** (exit 1, before any record; the error lists
them; D553) unless `--allow-env-limits` is given; then the values are recorded as `env_limits` in the build record and
in each collector run's record, a winning one is counted (`env_override.<VAR>` = 1) and warned, and the build warns at
its start (D445). `stack build --dry-run` shows the verdict on its `env_limits` line. The others (`*_FROM`, `*_FULL`,
…) leave no trace in the record. Leave them unset for any build whose results you keep.
