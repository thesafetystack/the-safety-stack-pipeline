"""``collect.v3_import`` (§9 "v3 import", §2.3, D13, D34, D54, D92, D139): the v3 provider record
history as a ``sources`` part, from CSV or parquet dumps of four v3 tables. **A stub (D34)**:
it reads dumps, never a database; the real one-off import is a later session.

Plan line::

    - {stage: collect.v3_import, prior: [sources, coverage], options: {dump_dir: <path>}}

**Input.** ``<dump_dir>/<table>.parquet`` or ``<dump_dir>/<table>.csv`` (parquet wins when both
exist) for each of ``metadata``, ``sources``, ``snapshots``, ``annotations``; all four are
required. Column names are v3's DDL (``db/0001_init.sql`` of the 9312c8c tree); the columns
:data:`REQUIRED` lists must be present (extra columns are ignored). JSON columns (``payload``,
``discovery``, ``fetch_meta``, ``stamps``) may be JSON text (CSV, a text dump) or nested values
(parquet); timestamps ISO-8601 text (``2026-03-01 12:00:00+00`` as Postgres prints it) or
parquet timestamps; naive = UTC.

**What is converted: the ``sources`` part only** (§9: "``metadata`` rows → ``collect.v3-import``
→ ``sources`` part, one row per row"). Each v3 ``metadata`` row (the raw table, not
``metadata_canon``) becomes one ``sources`` row of the own chain ``sources.v3_import`` (D54,
D92): ``source_id`` = the v3 ``source_id`` (kept, never minted); ``identity`` from the v3
``sources`` row (``channel``, ``external_id``, ``canonical_url``; ``keys`` typed by
:func:`stack.scholarly_records.keys_for_source` from the channel's group in
``configs/channels.yaml``); ``state = 'observed'``, ``record_format = 'record'``, ``status =
'ok'``; ``observed_at`` = the metadata row's ``created_at``; ``record_digest`` = the harness's
:func:`~stack.collect.base.record_digest` over ``{workflow, workflow_version, payload}``;
the identity key ``v3_meta_id = meta_id`` (D524; non-merging, was ``metadata.record_ref``),
``metadata.origin = 'import'`` (D13), ``provider_version`` =
``workflow_version``, ``license_class``/``doc_type``/``sensitivity`` from the v3 ``sources`` row
(the payload's ``doc_type`` first), ``title``/``abstract``/``authors``/``published_at``
(``pub_date``) from the payload; ``run_id`` = this run. The v3 ``origin`` (``auto``,
``human``, …), ``workflow`` and ``overrides`` are not carried (no column; the payload digest
covers them only through ``record_digest``).

**One observation per metadata row, idempotent (D139).** ``sources`` is keyed ``source_id``
within a file, so one run writes at most one row per source: per v3 source the **oldest**
(``created_at``, ``meta_id``) metadata row whose ``meta_id`` is not yet a ``v3_meta_id`` key of the
own chain (every file of it, not only the view). Rows already imported are skipped
(``metadata_already_imported``); later rows of a source that got one this run wait
(``metadata_pending``; the run is ``scope_complete = false`` and the next run imports the next
generation, so a plan line may carry ``repeat: {until: scope_complete, max_runs: N}``). A re-run
over an unchanged, fully imported dump therefore writes no ``sources`` file (D105 c) and
re-stamps the coverage rows.

**Coverage (pinned, D139):** one row per table read, channel ``v3_import`` (a toy channel of this
module, not in ``channels.yaml``), ``query = {"table": <name>}``, window ``[min, max]`` of the
table's time column (``created_at``; ``discovered_at`` for ``sources``; ``fetched_at`` for
``snapshots``; the run's clock for an empty table), ``seen`` = rows read, ``kept`` = metadata
rows imported this run (``metadata``), sources rows used (``sources``), 0 for the validated
tables. ``metadata`` is ``partial`` while rows are pending, every other row ``ok``.

**Read, validated, not converted:** ``snapshots`` and ``annotations``. §9 routes them through
``gather`` (``content.snapshots[]``, the pointer check against ``snapshots_latest``),
``acquire`` and ``<wf>_evidence`` / ``<wf>`` (``annotations_canon`` for selection); that is the
real import's work (D34). Here they are counted and checked: id shapes (v3 ``id_ok``: ``n…``
snapshots, ``a…`` annotations, ``m…`` metadata, ``s…`` sources), every ``source_id`` in the
``sources`` table, ``(source_id, seq)`` unique, every annotation's ``snapshot_id`` in
``snapshots`` with the same ``source_id`` (v3's denorm FK). Any violation, a missing table or
column, a channel not in ``channels.yaml`` fails the run (``CollectError``) naming the first
offending rows; nothing is written.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from stack.collect.base import RECORD, CollectError, Collector, record_digest, utc
from stack.config import ChannelDef
from stack.core.ids import is_valid
from stack.scholarly_records import keys_for_source, normalize_registry_id
from stack.stage_runner import StageContext, StageResult
from stack.store.parquet import read_family
from stack.urls import fetch_url

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"dump_dir"})

Row = dict[str, Any]

TABLES: tuple[str, ...] = ("metadata", "sources", "snapshots", "annotations")
#: v3 DDL columns the import reads or validates, per table (``NOT NULL`` ones and the identity).
REQUIRED: dict[str, tuple[str, ...]] = {
    "metadata": ("meta_id", "source_id", "workflow", "workflow_version", "payload", "origin", "run_id",
                 "created_at"),
    "sources": ("source_id", "channel", "external_id", "canonical_url", "doc_type", "license_class",
                "sensitivity", "discovered_at"),
    "snapshots": ("snapshot_id", "source_id", "seq", "run_id", "fetched_at", "observed_at"),
    "annotations": ("ann_id", "snapshot_id", "source_id", "workflow", "workflow_version", "payload", "origin",
                    "run_id", "created_at"),
}
#: The time column of each table's coverage window.
TIME_COLUMN = {"metadata": "created_at", "sources": "discovered_at", "snapshots": "fetched_at",
               "annotations": "created_at"}
#: v3 id letters (``id_ok``) of each table's primary key and of ``source_id``.
ID_LETTER = {"meta_id": "m", "source_id": "s", "snapshot_id": "n", "ann_id": "a"}
JSON_COLUMNS = ("payload", "discovery", "fetch_meta", "stamps")
#: The coverage rows' channel: a toy channel of this module (``Collector(channels=…)``).
COVERAGE_CHANNEL = ChannelDef(id="v3_import", kind="import", license_class="restricted",
                              channel_group="record")
SHOW = 5  # offending rows named in an error
#: The identity key of the v3 metadata row an imported row came from (D524; non-merging).
META_KEY = "v3_meta_id"


# ---------------------------------------------------------------- reading
def read_table(dump_dir: Path, name: str) -> list[Row]:
    """``<dump_dir>/<name>.parquet`` (preferred) or ``.csv`` as rows; JSON text columns parsed.
    A missing file or a missing :data:`REQUIRED` column is a :class:`CollectError`."""
    pq_path, csv_path = dump_dir / f"{name}.parquet", dump_dir / f"{name}.csv"
    if pq_path.exists():
        tbl = pq.read_table(pq_path)
    elif csv_path.exists():
        # every column as text: the import parses what it reads (no type guessing)
        with csv_path.open(newline="", encoding="utf-8") as f:
            cols = next(csv.reader(f), [])
        tbl = pacsv.read_csv(csv_path, convert_options=pacsv.ConvertOptions(
            column_types=dict.fromkeys(cols, pa.string()), strings_can_be_null=True))
    else:
        raise CollectError(f"v3 dump: no {name}.parquet or {name}.csv under {dump_dir}")
    missing = [c for c in REQUIRED[name] if c not in tbl.column_names]
    if missing:
        raise CollectError(f"v3 dump {name}: columns {missing} missing (v3 DDL names)")
    rows = tbl.to_pylist()
    for r in rows:
        for c in JSON_COLUMNS:
            if isinstance(r.get(c), str):
                try:
                    r[c] = json.loads(r[c])
                except ValueError as exc:
                    raise CollectError(f"v3 dump {name}: column {c} is not JSON ({exc})") from None
    return rows


def _bad(table: str, what: str, items: list[str]) -> None:
    if items:
        raise CollectError(f"v3 dump {table}: {len(items)} row(s) {what}: {', '.join(items[:SHOW])}"
                           + (" …" if len(items) > SHOW else ""))


def validate(t: Mapping[str, list[Row]]) -> None:
    """The checks of the module docstring; raises :class:`CollectError` on the first table that
    fails one."""
    for name, rows in t.items():
        for col, letter in ID_LETTER.items():
            if col in REQUIRED[name]:
                _bad(name, f"with a malformed {col}", [str(r[col]) for r in rows
                                                       if not isinstance(r[col], str) or not is_valid(r[col], letter)])
        key = {"metadata": "meta_id", "sources": "source_id", "snapshots": "snapshot_id", "annotations": "ann_id"}[name]
        n = Counter(r[key] for r in rows)
        _bad(name, f"repeating {key}", sorted(k for k, v in n.items() if v > 1))
        _bad(name, f"without {TIME_COLUMN[name]}", [r[key] for r in rows if r[TIME_COLUMN[name]] in (None, "")])
    sources = {r["source_id"] for r in t["sources"]}
    for name in ("metadata", "snapshots", "annotations"):
        _bad(name, "whose source_id is not in sources", [r["source_id"] for r in t[name] if r["source_id"] not in sources])
    _bad("sources", "without external_id and canonical_url",
         [r["source_id"] for r in t["sources"] if not r["external_id"] and not r["canonical_url"]])
    pairs: set[tuple[str, int]] = set()
    dup = []
    for r in t["snapshots"]:
        p = (r["source_id"], int(r["seq"]))
        if p in pairs:
            dup.append(f"{p[0]}#{p[1]}")
        pairs.add(p)
    _bad("snapshots", "repeating (source_id, seq)", dup)
    snap_src = {r["snapshot_id"]: r["source_id"] for r in t["snapshots"]}
    _bad("annotations", "whose snapshot_id is not in snapshots, or of another source",
         [r["ann_id"] for r in t["annotations"] if snap_src.get(r["snapshot_id"]) != r["source_id"]])


def imported_refs(ctx: StageContext) -> set[str]:
    """Every ``v3_meta_id`` key of the own ``sources`` chain (all its files, not the view: the view
    keeps one row per source; D524)."""
    chain = ctx.prior.get("sources") or ctx.inputs.get("sources")
    refs: set[str] = set()
    for f in chain.files if chain is not None else ():
        ident = read_family(f.path)[0].column("identity").to_pylist()
        refs |= {k["value"] for i in ident if i for k in i["keys"] or [] if k["kind"] == META_KEY}
    return refs


# ---------------------------------------------------------------- the stage
class Importer(Collector):
    """The harness with v3's identities: ``source_id`` comes from the dump (never minted) and
    ``observed_at`` from the metadata row."""

    def pub_date(self, value: Any) -> dt.datetime | None:
        """v3 ``pub_date`` as a timestamp: a full ISO date or datetime; a partial one (``2024``,
        ``2024-05``) is not widened to a day: ``None``, counted ``pub_date_unparsed``."""
        try:
            return utc(value)
        except (ValueError, TypeError):
            self.counts["pub_date_unparsed"] = self.counts.get("pub_date_unparsed", 0) + 1
            return None

    def import_row(self, meta: Row, src: Row) -> Row:
        sid, channel = str(src["source_id"]), str(src["channel"])
        ext = src["external_id"] or None
        url = fetch_url(src["canonical_url"]) or src["canonical_url"] or None  # the fetch form (url-normalisation.md §2)
        ch = self.channel(channel)
        prior = self.prior(channel, ext)  # type: ignore[arg-type]
        if prior is not None and prior["source_id"] != sid:
            raise CollectError(f"v3 source {sid} ({channel}, {ext}) is {prior['source_id']} in "
                               f"{self.sources_chain}: the dump changed a v3 id")
        if sid in self._rows:
            raise CollectError(f"v3 source {sid}: two metadata rows in one run")
        payload = meta["payload"] if isinstance(meta["payload"], Mapping) else {}
        sens = src.get("sensitivity")
        try:
            keys = [(k["kind"], k["value"]) for k in keys_for_source(ch.channel_group, channel, ext, url)]
        except ValueError as exc:
            raise CollectError(f"v3 source {sid}: no keys ({exc})") from None
        md = self._metadata(ch, {
            "title": payload.get("title"),
            "abstract": payload.get("abstract") if isinstance(payload.get("abstract"), str) else None,
            "authors": payload.get("authors"),
            "published_at": self.pub_date(payload.get("pub_date")),
            "doc_type": payload.get("doc_type") or src.get("doc_type"),
            "license_class": src["license_class"],
            "sensitivity": [sens] if sens else [],
            "origin": "import",
            "provider_version": int(meta["workflow_version"]),
        }, miss_count=0)
        keys.append((META_KEY, normalize_registry_id(str(meta["meta_id"]))))
        digest = record_digest({"workflow": meta["workflow"], "workflow_version": int(meta["workflow_version"]),
                                "payload": meta["payload"]})
        row = self._row(sid, channel, ext, state="observed", digest=digest,  # type: ignore[arg-type]
                        record_format=RECORD, status="ok", canonical_url=url, keys=keys, metadata=md,
                        prior=prior)
        row["observed_at"] = utc(meta["created_at"])
        self._rows[sid] = row
        return row


def _window(c: Collector, name: str, rows: Iterable[Row], kept: int, *, pending: bool) -> None:
    times = [utc(r[TIME_COLUMN[name]]) for r in rows]
    start, end = (min(times), max(times)) if times else (c.now, c.now)  # type: ignore[type-var]
    with c.window(COVERAGE_CHANNEL.id, {"table": name}, start, end) as w:
        w.seen, w.kept = len(times), kept
        if pending:
            w.truncated(cap=False)  # the next slice, not a cap: counted metadata_pending (D492)
        else:
            w.complete()


def _channels() -> dict[str, ChannelDef]:
    from stack.config import load

    out = {c.id: c for c in load().channels.channels}
    out[COVERAGE_CHANNEL.id] = COVERAGE_CHANNEL
    return out


def run(ctx: StageContext) -> StageResult:
    c = Importer(ctx, channels=_channels())
    raw = c.option("dump_dir", None)
    if not raw:
        raise CollectError(f"{ctx.stage}: options.dump_dir is required (the v3 dump directory)")
    dump_dir = Path(str(raw))
    if not dump_dir.is_absolute() and not dump_dir.exists():
        dump_dir = ctx.store_root / dump_dir
    t = {name: read_table(dump_dir, name) for name in TABLES}
    validate(t)

    done = imported_refs(ctx)
    src_by_id = {r["source_id"]: r for r in t["sources"]}
    by_source: dict[str, list[Row]] = defaultdict(list)
    for m in t["metadata"]:
        by_source[m["source_id"]].append(m)
    imported = already = pending = 0
    used: set[str] = set()
    for sid in sorted(by_source):
        todo = sorted((m for m in by_source[sid] if m["meta_id"] not in done),
                      key=lambda m: (utc(m["created_at"]), m["meta_id"]))
        already += len(by_source[sid]) - len(todo)
        if not todo:
            continue
        c.import_row(todo[0], src_by_id[sid])
        used.add(sid)
        imported += 1
        pending += len(todo) - 1

    _window(c, "metadata", t["metadata"], imported, pending=pending > 0)
    _window(c, "sources", t["sources"], len(used), pending=False)
    for name in ("snapshots", "annotations"):
        _window(c, name, t[name], 0, pending=False)
    for name in TABLES:
        c.counts[f"{name}_rows"] = len(t[name])
    c.counts.update(metadata_imported=imported, metadata_already_imported=already,
                    metadata_pending=pending)
    return c.finish(scope_complete=pending == 0)


__all__ = ["COVERAGE_CHANNEL", "REQUIRED", "TABLES", "Importer", "read_table", "run", "validate"]
