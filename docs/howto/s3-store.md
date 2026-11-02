# A store on S3

An S3 store is `s3://<bucket>/<prefix>`: its records, logs, data, blobs and releases live under that prefix, and
every command works on its local mirror, `$STACK_MIRROR/<bucket>/<prefix>/` (default `v4/mirror/` under the
repository). How jobs write to it (series directories and the publish rule, upload, ticks, recovery, liveness, two
writers) is in `docs/OPERATE.md`, "The store, jobs and upload"; running a build on a disposable machine is `docs/howto/container-lifecycle.md`.

## Settings

In the environment, or in the dotenv file `STACK_ENV_FILE` names (`.env.example`, "S3 store"):
`STACK_S3_ENDPOINT_URL`, `STACK_S3_ACCESS_KEY_ID`, `STACK_S3_SECRET_ACCESS_KEY`, optional `STACK_S3_DEFAULT_REGION`
(the older names `STACK_S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `STACK_S3_REGION` are still
accepted; per setting the new name wins, D726). They go to boto3
explicitly, never through its ambient credential chain; an error names a missing variable, never a value. The bucket
and prefix come from the URI. boto3 is in the `s3` uv group, which the default `dev` group includes (with `uv sync
--no-dev`, add `--group s3`).

## Start a store

Nothing to create on S3: the first job writes under the prefix. The first command naming the URI creates the mirror
directory and its binding `.stack-mirror.json` = `{"uri": "s3://<bucket>/<prefix>"}`.

```bash
export STACK_ENV_FILE=.env STACK_STORE=s3://<bucket>/<prefix>
uv run --no-sync python tools/s3_smoke.py $STACK_STORE      # optional: put, head, get, list, delete under <prefix>/_smoke/
uv run --no-sync stack build <plan> --dry-run               # each line's --in paths and outputs; writes nothing
uv run --no-sync stack build <plan>                         # uploads as it goes
```

On a new store every series directory is empty, so a build's first runs read empty priors and write each
directory's first full file. The binding never changes: a mirror directory serves one URI, and naming it by its path (`--store
v4/mirror/<bucket>/<prefix>`) is refused, so it is never written as a local-only store. To start over under a new
prefix, name the new URI; its mirror is a new directory.

## Read it from a laptop

Any machine with the settings reads the store; reading writes nothing to S3.

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> status        # jobs, liveness, spend, S3 state
uv run --no-sync stack --store s3://<bucket>/<prefix> ls             # every series directory and its head
uv run --no-sync stack --store s3://<bucket>/<prefix> ls data/documents/<series>/    # one directory's files in order
uv run --no-sync stack --store s3://<bucket>/<prefix> head data/corpus.build/<series>/
uv run --no-sync stack --store s3://<bucket>/<prefix> diff latest <build_id> --plan configs/plans/<plan>.yaml
```

`status` refreshes the index (one LIST of `builds/` and `runs/`, a GET of each record json not yet local or
changed) and downloads nothing else; `ls` and `head` read the same index (the run records) and no data file. Commands that read data (`diff`, `verify`,
`release`) download the files they read into the mirror, checked against their recorded sha256. For `verify` that is
every published data file of the store, even with `--in` (`stack/checks/chain.py` `StoreView`; blobs are only listed,
X5), so a first `verify` on a fresh mirror downloads all the store's parquet. Record jsons
land at their keys under the mirror, e.g.
`v4/mirror/<bucket>/<prefix>/builds/<head>.<plan>/runs/<head>.<stage>.json.zst`.

No `stack` verb downloads a log. To read a job's log (a live or dead one), fetch it by key and decompress it:

```bash
M=v4/mirror/<bucket>/<prefix>; KEY=builds/<head>.<plan>/runs/<head>.<stage>.log.zst
uv run --no-sync python -c 'import sys; from stack.store.remote import open_store; print(open_store(sys.argv[1]).fetch(sys.argv[2], refresh=True))' $M $KEY
zstd -dc $M/$KEY | tail -50
```

A build's own log is `builds/<head>.<plan>/build.log.zst`. Without the `zstd` tool:
`uv run --no-sync python -c 'import sys, zstandard; sys.stdout.buffer.write(zstandard.ZstdDecompressor().decompressobj().decompress(open(sys.argv[1], "rb").read()))' <file>`.

A view from another region may lag the writer by a moment; that is harmless for reading.

## Push a local job

A job run with `--no-upload` (or one whose end PUTs failed) is in the mirror only. `stack status` shows it as
`local-only` (or `push pending`). Upload it when it is finished:

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> store push <run or build id | run stem | build directory name>
```

It goes up in publish order (data files, json, log; a build's runs first, in start order), after the publish rule
is checked again against S3. `push` refuses a job whose inputs are not on S3 (push those first) and a run whose
output directory moved on S3 since the run pinned it (its last uploaded file is no longer the output's `after`,
`head_moved`, or does not sort before it, `name_order`): something else published there meanwhile, and pushing
would fork the series. An output that is already its directory's last file on S3 is done (the record had reached
S3; a lost acknowledgement). A refused push uploads nothing of the refused run; such a job stays local, and its work
is redone by a new run against the current head. Data files go up create-only (`If-None-Match: *`): a push never
overwrites one.

## Check before discarding a machine

```bash
uv run --no-sync stack --store s3://<bucket>/<prefix> status          # no job `running, live`
uv run --no-sync stack --store s3://<bucket>/<prefix> store push --check
```

`--check` GETs the S3 copy of every local record and lists each one that S3 lacks or holds with another status
(upload and no-upload jobs alike); exit 1 when any, else `nothing local is missing on S3`, exit 0. A listed
`upload: true` record is sent by the next uploading job's recovery or by `store push`; a listed no-upload job is
pushed or knowingly dropped. Blobs need no check: every job PUTs a blob before any row names it.

## Several stores per bucket

One prefix per store, e.g. `s3://<bucket>/main` and `s3://<bucket>/scratch-20261015`. Within one store, a
separate series (`data/<family>/exp-2/`) is often enough for an experiment; a separate store is for work that must
share nothing. Stores share nothing:
each has its own records, data and blobs (`<prefix>/blobs/<sha256>`; identical bytes in two stores are stored
twice), and each has its own mirror directory. A scratch store can be deleted from the bucket as a unit by deleting
its prefix by hand; no `stack` command deletes from S3 except job markers.

## What never goes to S3

- `cache/llm-responses/`: the machine-local LLM response cache. A fresh machine starts without it; an LLM call it
  would have served is asked again and paid for.
- `*.tmp` and `*.dl` files: staged outputs, atomic rewrites and downloads in progress.
- `.stack-mirror.json` (and the empty `.exclude_from_backup` beside it).

Blobs live only under the store's own prefix, flat (`<prefix>/blobs/<sha256>`, D360): there is no separate blob
bucket (a `STACK_S3_BASE_URI` left in an old `.env` is not read) and no `stack` verb that manages blobs. Snapshots
fetched by the retired v3/v1 archive lane keep `via = 'archive'` (D329).
