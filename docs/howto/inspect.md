# Inspect what a build fetched

How to answer "what did this build fetch, and what is missing?" from the store alone, read-only. Every command
below ran on the synthetic world (`docs/SETUP.md` §5) after one `synth-m5` build; outputs are real, trimmed (duckdb
tables without their borders). Synthetic, and different in a real store: the series (`main`), the plan, the
channels, the stand-in stages' `counts` (`tests/synth/stages/`), the blob contents and the spend (zero).

Run from the repository root with `S` set to the store's local root: a local-only store's path, or for
`s3://<bucket>/<prefix>` its mirror `$STACK_MIRROR/<bucket>/<prefix>` (default `v4/mirror/`; run `stack status` on the
URI first so the records are local; the snippets download absent files, with the settings of `docs/howto/s3-store.md`;
not run here). No `duckdb` or `zstd` CLI is needed: `uv run --no-sync python -c "import duckdb, zstandard"`.

## 1. Where things are

The layout (`docs/OPERATE.md`, "The store, jobs and upload"): `builds/<head>.<plan>/` holds a build's record, log and
its run records under `runs/`; `runs/` the standalone runs; `data/<family>/<series>/` the parquet chains;
`blobs/<sha256>` fetched bytes and converted texts. `stack status` lists jobs, `stack ls` the series directories,
`stack head` a directory's head and the files of its version (the newest full plus the parts after it).

```bash
uv run --no-sync stack --store <store> status --limit 3
#   20261014T1534Z  build  wnxg8q1pnwp8a  synth-m5                ok                          $0.00
uv run --no-sync stack --store <store> ls | head -4
# data/coverage.arxiv/main/: 1 file(s), 1 in version; head 20261012T013000Z_r000000000001.full.parquet
uv run --no-sync stack --store <store> head data/documents/<series>/
# head	data/documents/main/20261014T153445Z_r7qna4cqknccc.part.parquet
```

## 2. The build record

Build directories sort by start minute, so the last one of a plan is the newest (`stack status` names it too).
`build.json.zst` holds `status`, `runs`, `files` (each family the build wrote, by path), `verify`, `warnings`, and,
when a required stage stopped short, `incomplete` (and `incomplete_allowed`): `{stage, run_id, reasons}` entries;
when a drain stopped it, `stopped: {by, requested_at, wait_s, runs_drained, not_run}` (D573).
What the reasons mean and how to resume: `docs/OPERATE.md`, "`incomplete` builds and resuming" and "Stopping,
cancelling and resuming".

```bash
B=$(ls -d $S/builds/*.<plan> | tail -1)
uv run --no-sync python - $B <<'EOF'
import sys
from stack.runs import read_build_record
b = read_build_record(sys.argv[1] + "/build.json.zst")
print(b["status"], b["build_id"], "verify", b["verify"]["status"], "incomplete", b.get("incomplete", []),
      "incomplete_allowed", b.get("incomplete_allowed", []))
for key, f in b["files"].items():
    print(f"{key:24} {f['path']}")
EOF
# ok wnxg8q1pnwp8a verify ok incomplete [] incomplete_allowed []
# corpus                   data/corpus.build/main/20261014T153448Z_raayqxrs6zy6e.full.parquet
# documents                data/documents/main/20261014T153445Z_r7qna4cqknccc.part.parquet
# sources/sources.arxiv    data/sources.arxiv/main/20261014T153441Z_rypcf31x7kez6.part.parquet
```

Its `counts` summarises each run (`stage, status, scope_complete, rows, calls, carried, new_rows`). A `.zst` file:
`uv run --no-sync python -c 'import sys, zstandard; sys.stdout.buffer.write(zstandard.ZstdDecompressor().decompressobj().decompress(open(sys.argv[1], "rb").read()))' $B/build.log.zst`.

## 3. Run records

A build's runs are `$B/runs/<head>.<stage>.json.zst` (`stack/runs.py` `RunRecord`). What a run did: `status`,
`error_class`; `scope_complete` (false: it stopped short and the next run continues); `counts` (stage-specific
counters); `substages` (`{name: {ran, scope_complete}}`; real `acquire` has `carry, inband, history, fetch, convert,
quality, point`, `workflows/acquire/main.py`); `inputs` (family to file id) and `in_files`; `outputs` (family to
`{path, kind, row_count, file_digest, ...}`); `spend` (`usd_micro` per `openrouter/<model>`, `x`, `openalex`;
`reserved_open_usd_micro`; `batch_ids`, `abandoned_batch_ids`).

```bash
uv run --no-sync python -c 'import sys, json; from stack.runs import read_run_record as r
for k, v in sorted(r(sys.argv[1]).to_json().items()): print(k, json.dumps(v))' $B/runs/*.annotate.screen.json.zst
# counts {"asked": 6, "overridden": 0, "prior_evidence": 0, "rows": 7, "scope.filtered_out": 0}
# scope_complete false
# substages {"ask": {"ran": true, "scope_complete": false}, "select": {"ran": true, "scope_complete": true}}
```

All runs of a build as one table (wall seconds, output rows, settled and open dollars, non-zero counts):

```bash
uv run --no-sync python - $B <<'EOF'
import glob, sys
from stack.runs import read_run_record
recs = sorted((read_run_record(p) for p in glob.glob(sys.argv[1] + "/runs/*.json.zst")), key=lambda r: r.started_at)
for r in recs:
    wall = (r.finished_at - r.started_at).total_seconds() if r.finished_at else float("nan")
    sp = r.spend or {}
    usd, open_ = sum((sp.get("usd_micro") or {}).values()) / 1e6, (sp.get("reserved_open_usd_micro") or 0) / 1e6
    rows = sum(o["row_count"] for o in r.outputs.values())
    cnt = " ".join(f"{k}={v}" for k, v in sorted(r.counts.items()) if v)
    print(f"{r.run_id:13} {r.stage:16} {r.status:8} {str(r.scope_complete):5} {wall:7.1f} {rows:6} {usd:8.2f} {open_:6.2f}  {cnt}")
EOF
# rg7hevywxjv08 acquire          ok       False     0.4      1     0.00   0.00  consecutive_run=1
# r7qna4cqknccc acquire          ok       True      0.4      1     0.00   0.00  consecutive_run=2
# rrhnvbpsv1n4d annotate.screen  ok       False     0.2     13     0.00   0.00  asked=6 rows=7
```

## 4. Documents

The `documents` head is a version of several files (a full and parts): read it through the chain reader, which
keeps each document's newest row. A corpus file is one full file that duckdb reads directly. The macros are those of
`docs/CONSUME.md`, "The helper macros". Structs: `stack/schemas/blocks.py`, `structs.py` (`MEMBER`, `ROLE`, `SNAPSHOT`).

```bash
uv run --no-sync python - $S <series> $B <<'EOF'
import sys, duckdb
from stack.query import register_macros
from stack.runs import read_build_record
from stack.store.chain import resolve, read_view
store, series, build = sys.argv[1:4]
con = register_macros(duckdb.connect())
con.register("docs", read_view(resolve(store, f"data/documents/{series}/")))
corpus = read_build_record(build + "/build.json.zst")["files"]["corpus"]["path"]
con.sql(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{store}/{corpus}')")
con.sql("SELECT state, content.current_text_status AS text, count(*) AS n FROM docs GROUP BY ALL ORDER BY ALL").show()
con.sql("SELECT excluded_reason(d) AS why, count(*) AS n FROM docs d GROUP BY ALL ORDER BY n DESC").show()
con.sql("SELECT annotations.screen.status AS screen, count(*) AS n FROM corpus GROUP BY ALL").show()
con.sql("SET VARIABLE id = 's000000000001'")   # a doc_id
con.sql("""SELECT m.source_id, m.channel, m.canonical_url, m.is_live
           FROM (SELECT unnest(identity.members) AS m FROM docs WHERE doc_id = getvariable('id'))""").show()
con.sql("""SELECT r.content_role AS role, r.source_id, r.status, r.attempts, r.lane, s.raw.media_type AS media_type,
                  s.via, s.raw.sha256 AS body, s.text.sha256 AS text
           FROM (SELECT unnest(content.roles) AS r, content.snapshots AS ss FROM docs WHERE doc_id = getvariable('id')),
                (SELECT list_filter(ss, x -> x.snapshot_id = r.snapshot_id)[1] AS s)""").show()
EOF
```

The output, trimmed: by state and text status `acquired`/`ok` 8, `acquired`/`convert_failed` 1, `acquired`/`no_raw`
1, `identified` 1, `retired` 1, `triaged` 3; `excluded_reason` NULL 7, `triage:skip` 3, and 1 each of
`text:convert_failed`, `text:no_raw`, `media:txt`, `state:retired`, `masked:operator`; the corpus `screen` `ok` 6,
`not_attempted` 1; the document's one member (`s000000000001 arxiv https://arxiv.org/abs/2410.00001 true`); its roles (sha256s cut to
12 characters here; the query prints all 64, which §6 needs):

```
role       source_id      status  attempts  lane    media_type       via     body          text
full_text  s000000000001  ok             1  arxiv   application/pdf  live    ca4704882e99  9235725b2311
xml        s000000000001  ok             1  arxiv   application/xml  live    854f83056dc1  NULL
abstract   s000000000001  ok             1  inband  text/plain       inband  a4276b4772a0  a4276b4772a0
```

A role is one fetch target of one source (`status`, `attempts`, `error_class`, `retry_after`, `snapshot_id`); a
snapshot (`content.snapshots`; the query shows the one each role points at) is one stored body (`raw`: `sha256`,
`bytes`, `media_type`) and its converted text (`text`, the same three fields, and `text_status`). Documents without text: `WHERE state = 'acquired' AND NOT
has_text(d)`, with `excluded_reason(d)`, `acquire_pending(d)`, `acquire_failed(d)`; `docs/howto/why-missing.md`.

## 5. Sources and coverage

Each collector writes `data/sources.<p>/<series>/` (a row per source: `identity.channel`, `state`,
`identity.canonical_url`, `metadata`) and `data/coverage.<p>/<series>/` (a row per query window: `channel`, `query`,
`window_start_at`, `window_end_at`, `seen`, `kept`, `is_exhausted`, `status` (`ok`, `partial`, `failed_transient`,
`failed_permanent`), `error_class`, `retry_after`; `stack/schemas/families/sources.py`, `coverage.py`). A gap
between windows, or a window that is not `ok`, is where a channel's collection is missing.

```bash
uv run --no-sync python - $S <series> <<'EOF'
import sys, duckdb, pyarrow as pa
from stack.store.chain import resolve, read_view
from stack.store.index import store_index
store, series = sys.argv[1:3]
con = duckdb.connect()
dirs = [d for d in store_index(store).series_dirs() if d.endswith(f"/{series}")]
for fam in ("sources", "coverage"):
    con.register(fam, pa.concat_tables([read_view(resolve(store, d + "/")) for d in dirs if d.startswith(f"data/{fam}.")]))
con.sql("SELECT identity.channel AS channel, state, count(*) AS n FROM sources GROUP BY ALL ORDER BY ALL").show()
con.sql("""SELECT channel, status, count(*) AS windows, min(window_start_at) AS first, max(window_end_at) AS last,
                  sum(seen) AS seen, sum(kept) AS kept FROM coverage GROUP BY ALL ORDER BY ALL""").show()
EOF
# sources, all observed: alignmentforum 1, arxiv 6, lesswrong 1, manual 3, openalex 4, x 1
# channel   status  windows  first                   last                    seen  kept
# arxiv     ok            1  2026-10-11 01:30:00+00  2026-10-12 01:30:00+00    10     3
# openalex  ok            1  2026-10-10 01:30:00+00  2026-10-11 01:30:00+00    10     3
```

## 6. Bodies and texts

A snapshot's `raw.sha256` (the fetched body) and `text.sha256` (the converted markdown; the full 64-hex value from the
query in §4) name files `blobs/<sha256>`, of `raw.bytes` and `text.bytes` bytes (sizes recorded when written, D690):
flat, uncompressed, the bytes as stored. In a local store read them directly; in a mirror an absent blob downloads
on read, checked against its sha256 (`stack/store/blobs.py`). There is no `stack` verb for blobs. In the synthetic
world a blob is placeholder text (its `application/pdf` body is not a PDF).

```bash
P=$(uv run --no-sync python -c 'import sys; from stack.store.blobs import open_blobs; print(open_blobs(sys.argv[1]).path(sys.argv[2]))' $S <text.sha256>)
head -c 400 $P
# synthetic blob 9235725b2311ad912c13596d96dc2922bfdf1b2308034010e6560682de64cad0
# A synthetic paragraph standing in for the converted text of this document in the test world. ...
```

## 7. Spend

`stack status` shows each job's spend (a build sums its runs'); `--json` gives `spend_usd_micro` and
`open_usd_micro` per job (a total over a period: `docs/OPERATE.md`, "Spend in run records and per-run caps"). Per
provider or model (a key per `openrouter/<model>`, `x`, `openalex`), sum the run records of a build; the total is
the build's `stack status` figure plus its open amount:

```bash
uv run --no-sync python - $B <<'EOF'
import collections, glob, sys
from stack.runs import read_run_record
by = collections.Counter()
for p in glob.glob(sys.argv[1] + "/runs/*.json.zst"):
    sp = read_run_record(p).spend or {}
    by.update({**(sp.get("usd_micro") or {}), "(open)": sp.get("reserved_open_usd_micro") or 0})
print({k: v / 1e6 for k, v in sorted(by.items())}, "total", sum(by.values()) / 1e6)
EOF
# {'(open)': 0.0} total 0.0
```
