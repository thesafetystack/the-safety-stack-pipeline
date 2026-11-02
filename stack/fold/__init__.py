"""The fold (§3.2, §7.4; D14, D15, D18): five steps over pyarrow tables and one driver.

1. :func:`union` the ``documents`` chain by chain position (§7.3, :func:`stack.store.chain.read_view`);
   ``build`` reads one ``corpus`` file (a ``fold.corpus`` output) as is;
2. :func:`join` the ``<wf>`` files into ``annotations`` (``build`` only): a registered workflow
   into its typed ``annotations.<wf>`` = ``ANN<P>`` (item grain: the doc's item rows, ordered by
   ``item_key``, into ``payload.items[]``), any other into ``annotations.other[]`` as
   ``ANN<JSON>``; then ``is_basis_current`` is recomputed for every ``ANN`` present (``triage``
   and ``other[]`` included) and **mapping M** applied: a basis that differs from the row's
   current one (``content.current_text_sha256`` for ``text``, ``metadata.fields_digest`` for
   ``fields``) makes the ``ANN`` ``skipped[stale_basis]``, payload kept (C15). A NULL
   ``basis_digest`` or NULL current basis leaves ``is_basis_current`` NULL and turns only an
   ``ok`` ``ANN`` into ``skipped[stale_basis]``. A ``chained`` ``ANN`` (D235; registry ``reads``)
   is mapped in a second pass, after every other one, in registry order: its current basis
   (:func:`chained_basis`) reads the row's already-mapped ``ANN`` of each read;
3. :func:`filter` with a named filter or any SQL predicate over the row (duckdb, after the join so a
   build filter may read ``annotations.screen``; :mod:`stack.query` holds the filters and macros);
4. release rules: :mod:`stack.fold.release` (``fold.release`` writes ``releases/<tag>/``, not a
   family file, so it has its own driver :func:`stack.fold.release.run_release`);
5. :func:`write` one full file with the fold header keys (``fold_config``, ``filter`` = the
   resolved SQL predicate or ``'none'``, ``annotations_folded`` = JSON list of the joined
   workflows, ``release_config``), ``blocks_written = '*'`` (D15), ``stage = fold.<config>``,
   ``writer = fold``.

:func:`run_fold` drives ``corpus``, ``build`` and ``compact`` of
:data:`stack.fold.configs.FOLD_CONFIGS`:

* ``corpus``: union → filter (default ``has_text``) → a full ``corpus`` file without a base into
  the destination directory (D372).
* ``build``: a ``fold.corpus`` file → join → filter (default ``has_text``) → a ``corpus`` chain
  of one; header ``inputs`` = ``{corpus: <file id>, <wf>: <file id>…}``.
* ``compact``: union → a ``documents`` full file whose base is the superseded head (§7.4, V12): no
  filter; rows keep ``created_at``, ``updated_at``, ``run_id`` and every block stamp byte for byte
  (the compaction's run id is in the header only); tombstones survive.

``stack fold`` (T1.6) calls :func:`run_fold` inside :func:`stack.stage_runner.run_recorded`;
the stage adapter :mod:`stack.fold.stage` (``stack run fold.<config>``) runs steps 1-3 itself and
emits the table, so the runner publishes it. Both write the same header and run-record ``fold``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from stack.core.hashes import jcs
from stack.fold.configs import FoldConfigSpec, fold_config
from stack.query import one_statement, resolve_filter, sql_connection
from stack.runs import FoldInfo
from stack.schemas.blocks import ANN_COMMON, ANNOTATIONS
from stack.schemas.families import CHAIN_WORKFLOWS
from stack.schemas.families.wf import SEL
from stack.schemas.payloads.registry import REGISTRY
from stack.schemas.structs import T2
from stack.store.chain import (
    Chain,
    ChainError,
    HeaderFields,
    HeadMovedError,
    Published,
    head_of,
    read_view,
    resolve,
    write_file,
)
from stack.store.index import store_index
from stack.store.layout import (
    DATA,
    NONE,
    WILDCARD,
    DataFile,
    family_of_dir,
    make_data_file,
    tie_guard,
)
from stack.store.parquet import (
    FileFormat,
    SchemaMismatch,
    file_format,
    read_family,
    read_header,
)

#: ``writer`` of every fold file (the file name's writer part).
WRITER = "fold"
#: Header fields a caller (the runner) may set through ``header_extra``.
HEADER_EXTRA_KEYS: frozenset[str] = frozenset(
    {"config_digest", "code_digest", "prior", "only", "scope_complete"})
_ROW = "__fold_row"
_CODE_FILES = (Path(__file__), Path(__file__).with_name("configs.py"),
               Path(__file__).parents[1] / "query.py")


class FoldError(ValueError):
    """A fold call that cannot run as asked (bad inputs, options, or filter)."""


# ---------------------------------------------------------------- 1. union
def union(store_root: str | Path, ref: str) -> pa.Table:
    """The view of ``ref`` (a series directory or an exact file): per key the row of the newest
    file of its version (§7.3)."""
    return read_view(resolve(store_root, ref))


def _resolve_documents_input(store_root: Path, ref: str) -> Chain:
    """``ref`` is a ``documents`` series directory or an exact ``documents`` file (D373)."""
    chain = resolve(store_root, ref)
    if chain.family != "documents":
        raise FoldError(f"input documents={ref}: {chain.dir or ref} holds {chain.family} files")
    return chain


# ---------------------------------------------------------------- 2. join
#: ANN common field names (A.4), in order.
ANN_FIELDS: tuple[str, ...] = tuple(f.name for f in ANN_COMMON)
#: Columns of a ``<wf>`` file that are not payload: SEL, ANN common fields, T2 (D48).
_NON_PAYLOAD: frozenset[str] = frozenset(f.name for f in (*SEL, *ANN_COMMON, *T2))
#: ANN common fields the item rows of one document must agree on (item grain).
_ITEM_AGREE: tuple[str, ...] = ("workflow", "version", "status", "error_class", "basis_kind",
                                "basis_digest", "config_digest", "origin")
_ANN_TYPED: tuple[str, ...] = tuple(f.name for f in ANNOTATIONS if f.name != "other")


def _json_default(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.UTC).isoformat()
    if isinstance(v, bytes):
        return v.hex()
    raise TypeError(f"payload_json: {type(v).__name__} is not JSON-serialisable")


def payload_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON of an untyped payload (sorted keys, no whitespace, UTF-8; timestamps as
    ISO 8601 UTC)."""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default)


def _block_field(table: pa.Table, block: str, name: str) -> list[Any]:
    """``block.name`` per row, NULL where the block is NULL."""
    arr = table.column(block).combine_chunks()
    vals = arr.field(name).to_pylist()
    return [v if ok else None for v, ok in zip(vals, arr.is_valid().to_pylist(), strict=True)]


def _ann_from_rows(workflow: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The typed ``ANN<P>`` of one document from its ``<wf>`` rows (one row at document grain,
    the item rows at item grain)."""
    spec = REGISTRY[workflow]
    if spec.grain == "document":
        if len(rows) != 1:
            raise FoldError(f"join {workflow}: {len(rows)} rows for doc {rows[0]['doc_id']} at document grain")
        (row,) = rows
        payload = {f.name: row[f.name] for f in spec.payload_schema}
    else:
        rows = sorted(rows, key=lambda r: r["item_key"])
        row = rows[0]
        for r in rows[1:]:
            diff = [k for k in _ITEM_AGREE if r[k] != row[k]]
            if diff:
                raise FoldError(f"join {workflow}: item rows of doc {row['doc_id']} disagree on {diff}")
        (items_field,) = list(spec.payload_schema)
        item_names = [f.name for f in items_field.type.value_type]
        # An item_key '' row at item grain carries the document's status only (no items).
        payload = {items_field.name: [{n: r[n] for n in item_names} for r in rows if r["item_key"] != ""]}
    return {**{k: row[k] for k in ANN_FIELDS}, "payload": payload}


def _anns_other(workflow: str, rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    """``ANN<JSON>`` entries (one per row, ordered by ``item_key``) of an unregistered workflow."""
    payload_cols = [c for c in columns if c not in _NON_PAYLOAD]
    return [{**{k: r[k] for k in ANN_FIELDS},
             "payload": {"item_key": r.get("item_key") or "",
                         "payload_json": payload_json({c: r[c] for c in payload_cols})}}
            for r in sorted(rows, key=lambda r: r.get("item_key") or "")]


def _plain(v: Any) -> Any:
    """A payload as JCS input: timestamps as UTC ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (D50's rendering)."""
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            raise FoldError(f"chained basis: naive timestamp {v!r} in a payload")
        return v.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _H(value: Any) -> str:
    """``H`` of D19 (sha256 of the canonical JSON; :func:`stack.checks.digests.H`)."""
    return hashlib.sha256(jcs(value).encode("utf-8")).hexdigest()


#: Statuses of a read that give a ``chained`` basis (a decided result).
CHAINED_READ_STATUSES: frozenset[str] = frozenset({"ok", "empty"})


def chained_basis(text_sha256: str | None, annotations: Mapping[str, Any] | None,
                  reads: tuple[str, ...] | list[str]) -> str | None:
    """The current ``chained`` basis of a row (D235): ``H([text_sha256, [[name, H([status,
    payload])] for name in reads]])`` over the row's typed ``annotations.<name>`` (as mapping M
    left it). None when the text digest is None or any read is absent or not ``ok``/``empty``."""
    if text_sha256 is None:
        return None
    parts: list[Any] = []
    for name in reads:
        a = (annotations or {}).get(name)
        if a is None or a.get("status") not in CHAINED_READ_STATUSES:
            return None
        parts.append([name, _H([a["status"], _plain(a.get("payload"))])])
    return _H([text_sha256, parts])


def _chained_reads(ann: Mapping[str, Any]) -> tuple[str, ...]:
    spec = REGISTRY.get(ann["workflow"])
    if spec is None or not spec.reads:
        raise FoldError(f"annotation {ann['workflow']}: basis_kind 'chained' but the registry names no "
                        f"reads for it")
    return spec.reads


def current_basis(ann: Mapping[str, Any], text_sha256: str | None, fields_digest: str | None,
                  annotations: Mapping[str, Any] | None = None) -> str | None:
    """The row's current basis for ``ann``: ``content.current_text_sha256`` (``text``),
    ``metadata.fields_digest`` (``fields``) or :func:`chained_basis` over the row's
    ``annotations`` and the registry ``reads`` of ``ann``'s workflow (``chained``; the block is
    required then)."""
    kind = ann["basis_kind"]
    if kind == "text":
        return text_sha256
    if kind == "fields":
        return fields_digest
    if kind == "chained":
        if annotations is None:
            raise FoldError(f"annotation {ann['workflow']}: a chained basis needs the row's annotations")
        return chained_basis(text_sha256, annotations, _chained_reads(ann))
    raise FoldError(f"annotation {ann['workflow']}: basis_kind {kind!r} not in ('text', 'fields', 'chained')")


def basis_current(ann: Mapping[str, Any], text_sha256: str | None, fields_digest: str | None,
                  annotations: Mapping[str, Any] | None = None) -> bool | None:
    """``is_basis_current`` recomputed: NULL when ``basis_digest`` or the current basis is NULL."""
    cur = current_basis(ann, text_sha256, fields_digest, annotations)
    if ann["basis_digest"] is None or cur is None:
        return None
    return bool(ann["basis_digest"] == cur)


def mapping_m(ann: dict[str, Any], text_sha256: str | None, fields_digest: str | None,
              annotations: Mapping[str, Any] | None = None) -> bool:
    """Recompute ``is_basis_current`` and apply mapping M in place; True when it made the ``ANN``
    ``skipped[stale_basis]`` (a differing basis; or a NULL basis or NULL current basis on an
    ``ok``). The payload is kept (C15). ``annotations`` = the row's block (``chained`` only)."""
    cur = basis_current(ann, text_sha256, fields_digest, annotations)
    ann["is_basis_current"] = cur
    if cur is False or (cur is None and ann["status"] == "ok"):
        ann["status"], ann["error_class"] = "skipped", "stale_basis"
        return True
    return False


def join(table: pa.Table, wf_tables: Mapping[str, pa.Table] | None,
         counts: dict[str, int] | None = None) -> tuple[pa.Table, list[str]]:
    """Join each ``<wf>`` table on ``doc_id`` into ``annotations`` and apply mapping M (module
    docstring). Returns ``(table, annotations_folded)`` = the joined workflows in the given
    order. ``wf_tables=None`` (a config without a join step) is a pass-through with ``[]``;
    ``{}`` still recomputes ``is_basis_current`` and mapping M over the carried annotations.
    ``counts`` (when given) gains ``joined.<wf>`` (documents matched) and ``stale_basis.<wf>``."""
    if wf_tables is None:
        return table, []
    counts = counts if counts is not None else {}
    doc_ids = table.column("doc_id").to_pylist()
    index = {d: i for i, d in enumerate(doc_ids)}
    anns: list[dict[str, Any]] = table.column("annotations").to_pylist()
    for wf, wt in wf_tables.items():
        if wf in CHAIN_WORKFLOWS or wf == "other" or not wf:
            raise FoldError(f"join {wf!r}: not a <wf> workflow ({sorted(CHAIN_WORKFLOWS)} are carried "
                            f"from the documents chain)")
        rows = wt.to_pylist()
        bad = sorted({str(r.get("workflow")) for r in rows} - {wf})
        if bad:
            raise FoldError(f"join {wf}: the file holds rows of workflow(s) {bad}")
        missing = [c for c in ("doc_id", *ANN_FIELDS) if c not in wt.column_names]
        if missing:
            raise FoldError(f"join {wf}: columns {missing} missing (a <wf> file has SEL and ANN common fields)")
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_doc.setdefault(r["doc_id"], []).append(r)
        matched = 0
        for doc, drows in by_doc.items():
            i = index.get(doc)
            if i is None:
                continue
            matched += 1
            if wf in REGISTRY:
                anns[i][wf] = _ann_from_rows(wf, drows)
            else:
                kept = [o for o in anns[i]["other"] if o["workflow"] != wf]
                anns[i]["other"] = kept + _anns_other(wf, drows, wt.column_names)
        counts[f"joined.{wf}"] = matched
    text = _block_field(table, "content", "current_text_sha256")
    fields = _block_field(table, "metadata", "fields_digest")
    for i, a in enumerate(anns):
        chained = [n for n in _ANN_TYPED if a.get(n) is not None and a[n]["basis_kind"] == "chained"]
        for name in _ANN_TYPED:
            if name in chained:
                continue
            if a.get(name) is not None and mapping_m(a[name], text[i], fields[i]):
                counts[f"stale_basis.{name}"] = counts.get(f"stale_basis.{name}", 0) + 1
        for o in a["other"]:
            if mapping_m(o, text[i], fields[i]):
                counts[f"stale_basis.{o['workflow']}"] = counts.get(f"stale_basis.{o['workflow']}", 0) + 1
        for name in chained:  # second pass (D235): over the reads as mapped above, registry order
            if mapping_m(a[name], text[i], fields[i], a):
                counts[f"stale_basis.{name}"] = counts.get(f"stale_basis.{name}", 0) + 1
    col = pa.array(anns, type=table.schema.field("annotations").type)
    out = table.set_column(table.schema.get_field_index("annotations"),
                           table.schema.field("annotations"), col)
    return out, list(wf_tables)


# ---------------------------------------------------------------- 3. filter
def filter_rows(table: pa.Table, predicate: str) -> pa.Table:
    """Rows of ``table`` satisfying ``predicate`` (a named filter or a SQL predicate over the
    row; the row is also visible as the struct ``d``, so ``excluded_reason(d) IS NULL`` works),
    in input order. duckdb evaluates the predicate and returns row numbers only; the rows are
    taken with pyarrow so they stay byte-identical (D59's reason)."""
    sql = resolve_filter(predicate)
    indexed = table.append_column(_ROW, pa.array(range(table.num_rows), pa.int64()))
    con = sql_connection()  # the predicate may come from a release bundle (D647): no file access
    try:
        con.register("fold_input", indexed)
        try:
            sel = con.execute(one_statement(
                f"SELECT {_ROW} FROM fold_input d WHERE ({sql}) ORDER BY {_ROW}")).to_arrow_table()
        except duckdb.Error as exc:
            raise FoldError(f"fold filter {sql!r} does not evaluate: {exc}") from exc
    finally:
        con.close()
    if sel.num_rows == table.num_rows:  # every row, in order (ORDER BY the row number): no copy
        return table.combine_chunks()
    return table.take(sel.column(_ROW)).combine_chunks()


#: The design's name for step 3 (shadows the builtin inside this module's namespace only).
filter = filter_rows


# ---------------------------------------------------------------- 5. write
def _code_digest() -> str:
    h = hashlib.sha256()
    for p in _CODE_FILES:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def fold_config_digest(fold: FoldInfo) -> str:
    """Default ``config_digest`` of a fold file: sha256 of the JCS of its fold record."""
    return hashlib.sha256(jcs(dict(fold)).encode("utf-8")).hexdigest()


def write(store_root: str | Path, table: pa.Table, spec: FoldConfigSpec, fold: FoldInfo, *,
          run_id: str, plan_name: str, inputs: Mapping[str, str], dest: DataFile, base: str = "",
          after: str = "", header_extra: Mapping[str, Any] | None = None,
          finished_at: dt.datetime | None = None,
          on_published: Callable[[list[Published]], None] | None = None) -> Published:
    """Write ``table`` as one full ``spec.output_family`` file ``dest`` with the fold header (D15)."""
    extra = dict(header_extra or {})
    unknown = set(extra) - HEADER_EXTRA_KEYS
    if unknown:
        raise FoldError(f"header_extra keys {sorted(unknown)} not in {sorted(HEADER_EXTRA_KEYS)}")
    fields = HeaderFields(
        stage=spec.stage, writer=WRITER, run_id=run_id, plan=plan_name, inputs=dict(inputs),
        config_digest=extra.pop("config_digest", None) or fold_config_digest(fold),
        code_digest=extra.pop("code_digest", None) or _code_digest(),
        blocks_written=WILDCARD, fold_config=fold["config"], filter=fold["filter"],
        annotations_folded=list(fold["annotations_folded"]),
        release_config=fold["release_config"], **extra)
    return write_file(store_root, dest, table, fields, base=base, after=after, finished_at=finished_at,
                      on_published=on_published)


# ---------------------------------------------------------------- inputs
def data_file(store_root: Path, ref: str | Path, what: str) -> Path:
    """The ``data/`` file ``ref`` names: a file id (``<run_id>.<family>``) or a path (absolute,
    relative to the store root, or relative to the current directory) under ``data/``. Either way
    an ``ok`` run record must name it (D345: a file no record names is invisible); it is
    downloaded when absent locally (pull on read)."""
    s = str(ref)
    idx = store_index(store_root)
    if "/" in s or s.endswith(".parquet"):
        p = Path(s)
        cands = [p] if p.is_absolute() else [store_root / p, p]
        hit = next((c for c in cands if c.exists()), cands[0])
        try:
            rel = hit.resolve().relative_to((store_root / DATA).resolve())
        except ValueError:
            raise FoldError(f"{what}={s}: not a file under {store_root / DATA}") from None
        key = f"{DATA}/{rel.as_posix()}"
        found = [o for _, _, o in idx.outputs() if o["path"] == key]
        if not found:
            raise FoldError(f"{what}={s}: no ok run record names {key}")
        return idx.fetch_output(found[0])
    out = idx.output(s)
    if out is None:
        raise FoldError(f"{what}={s}: no ok run record names that file id")
    return idx.fetch_output(out[2])


def corpus_format(path: Path, header: Mapping[str, str] | None = None) -> FileFormat:
    """The format version of a ``data/`` family file (:func:`stack.store.parquet.file_format`, from
    the footer only; R1-2 under D295): an older version reads upcast on read, as every reader does;
    a file that does not read (newer, unknown, behind an ``Incompatible`` step) is a
    :class:`FoldError` with the reader's message and the operator's next step, never a traceback
    (R8-2)."""
    h = dict(header) if header is not None else read_header(path)
    try:
        return file_format(path, h)
    except SchemaMismatch as exc:
        raise FoldError(f"{type(exc).__name__}: {exc}; corpus files are per-build outputs: run a new "
                        f"`stack build`") from None


def read_corpus(path: Path) -> tuple[pa.Table, dict[str, str]]:
    """``(table, header)`` of a ``corpus`` (or any registered) ``data/`` file at its family's
    current format version (:func:`stack.store.parquet.read_family`: an older version upcast); a
    file that does not read is :func:`corpus_format`'s :class:`FoldError`."""
    corpus_format(path)
    return read_family(path)


def corpus_input(store_root: Path, ref: str | Path, *, fold_config: str) -> tuple[pa.Table, dict[str, str], Path]:
    """``(table, header, path)`` of the ``corpus`` file ``ref`` whose header ``fold_config`` is
    ``fold_config`` (``fold.build`` reads a ``fold.corpus`` output; ``fold.release`` a build's).
    A corpus of an older format version is read upcast (:func:`read_corpus`; R1-2, D295), one
    that does not read is refused with a :class:`FoldError`."""
    path = data_file(store_root, ref, "corpus")
    fam = read_header(path).get("family")
    if fam != "corpus":
        raise FoldError(f"input corpus={ref}: {path.name} is a {fam} file")
    tbl, h = read_corpus(path)
    if h["family"] != "corpus":
        raise FoldError(f"input corpus={ref}: {path.name} is a {h['family']} file")
    if h.get("fold_config") != fold_config:
        raise FoldError(f"input corpus={ref}: fold_config {h.get('fold_config')!r}, expected {fold_config!r} "
                        f"(fold.build reads a fold.corpus file, fold.release a fold.build file)")
    return tbl, h, path


def wf_input(store_root: Path, workflow: str, ref: str | Path) -> tuple[pa.Table, str]:
    """``(table, file id)`` of a ``<wf>`` file to join. A registered workflow's file is a
    ``data/`` file of family ``<wf>`` (read through the registry schema); an unregistered
    workflow's is any parquet file with SEL's ``doc_id`` and the ANN common fields as columns
    (its header ``file_id`` when it has one, else its path)."""
    if workflow in CHAIN_WORKFLOWS or workflow == "other" or not workflow:
        raise FoldError(f"join {workflow!r}: not a <wf> workflow ({sorted(CHAIN_WORKFLOWS)} are carried "
                        f"from the documents chain)")
    if workflow in REGISTRY:
        path = data_file(store_root, ref, workflow)
        tbl, h = read_family(path)
        if h["family"] != workflow:
            raise FoldError(f"join {workflow}={ref}: {path.name} is a {h['family']} file")
        return tbl, h["file_id"]
    p = Path(str(ref))
    path = p if p.is_absolute() or p.exists() else store_root / p
    if not path.exists():
        raise FoldError(f"join {workflow}={ref}: no such file (an unregistered workflow is joined by path)")
    tbl = pq.read_table(path)
    meta = tbl.schema.metadata or {}
    fid = (meta.get(b"stack.file_id") or str(path).encode()).decode()
    return tbl.replace_schema_metadata(None), fid


# ---------------------------------------------------------------- driver
@dataclass(frozen=True)
class FoldResult:
    """What a fold wrote: the published file, the run record's ``fold`` entry, row counts."""

    published: Published
    fold: FoldInfo
    inputs: dict[str, str]
    rows_in: int
    rows_out: int
    counts: dict[str, int]

    @property
    def file_id(self) -> str:
        return self.published.file_id

    @property
    def path(self) -> Path:
        return self.published.path


_INPUT_KEYS: dict[str, frozenset[str]] = {
    "corpus": frozenset({"documents"}), "compact": frozenset({"documents"}),
    "build": frozenset({"corpus"}), "release": frozenset({"corpus"}),
}


def check_restart(store_root: str | Path, config: str, out_dir: str, *, restart: bool) -> None:
    """The restart guard (D373, D380) of a fold writing a full without a base into ``out_dir``,
    the one rule for ``stack fold`` and ``stack run fold.*`` (conductor ruling 2026-10-14): a
    non-empty series directory of a family the stage reads by default
    (:func:`stack.stage_runner.restart_guarded`) needs ``restart``; fold.corpus and fold.build
    never read their output family and are exempt. :class:`FoldError` otherwise (a usage error,
    raised before the run starts by the CLI and the runner)."""
    from stack.plan import StageLine
    from stack.stage_runner import restart_guarded

    parts = [x for x in out_dir.strip("/").split("/") if x]
    if restart or len(parts) != 3 or not head_of(store_root, "/".join(parts)):
        return
    stage = f"fold.{config}"
    if family_of_dir(parts[1]) in restart_guarded(stage, StageLine.model_validate({"stage": stage})):
        raise FoldError(f"{stage}: {'/'.join(parts)}/ is not empty and the fold does not read it: pass --restart "
                        "to write a full without a base")


def run_fold(store_root: str | Path, config: str, *, run_id: str, plan_name: str,
             inputs: Mapping[str, str], filter: str | None = None,
             joins: Mapping[str, str | Path] | None = None, release: str | None = None,
             header_extra: Mapping[str, Any] | None = None,
             finished_at: dt.datetime | None = None, run_at: dt.datetime | str | None = None,
             out: str | None = None, comment: str | None = None, restart: bool = False,
             on_published: Callable[[list[Published]], None] | None = None) -> FoldResult:
    """Run fold ``config`` (``corpus``, ``build``, ``compact``) and publish its file;
    ``release`` writes ``releases/<tag>/`` and goes through
    :func:`stack.fold.release.run_release` (``FoldError`` here).

    ``inputs``: ``{'documents': <series dir or exact file>}`` for ``corpus``/``compact``,
    ``{'corpus': <file id or data/ path>}`` (a ``fold.corpus`` output) for ``build``.
    ``out``: the destination directory (``data/<family>/<series>`` or ``data/<family>`` for a
    file without a series; ``compact`` defaults to its input's directory, the others need it);
    ``comment``: the file's label; ``restart``: allow a base-less full into a non-empty directory
    that the fold reads its input from (D380). ``run_at``: the run's start (the name's
    stamp, bumped past the newest input, :func:`stack.store.layout.tie_guard`).
    ``filter``: a named filter or a SQL predicate (default per config; ``compact`` takes none).
    ``joins``: ``{<wf>: <file id or path>}`` (``build`` only; :func:`wf_input`).
    ``header_extra``: runner header fields (:data:`HEADER_EXTRA_KEYS`). ``on_published``: the
    runner's final-record hook, called once the files are in place
    (:func:`stack.store.chain.publish`, D38). Inputs are read through the run records (D345): a
    chain resolves from them, a file read as is must be named by one."""
    root = Path(store_root)
    spec = fold_config(config)
    got = set(inputs)
    if got != _INPUT_KEYS[config]:
        raise FoldError(f"fold.{config} takes inputs {sorted(_INPUT_KEYS[config])}, got {sorted(got)}")
    if joins and not spec.joins:
        raise FoldError(f"fold.{config} joins no <wf> files (got {sorted(joins)})")
    if spec.output == "release_dir":
        raise FoldError("fold.release writes releases/<tag>/: use stack.fold.release.run_release "
                        "(stack fold release, stack release)")
    if release not in (None, NONE):
        raise FoldError(f"fold.{config} applies no release config (got {release!r})")
    if filter is not None and spec.default_filter is None:
        raise FoldError(f"fold.{config} has no filter step (got {filter!r})")
    predicate = resolve_filter(filter if filter is not None else spec.default_filter) \
        if spec.default_filter is not None else None
    if not spec.implemented:  # pragma: no cover - every config is implemented since M5
        raise NotImplementedError(f"fold.{config} is not implemented")

    counts: dict[str, int] = {}
    joined: dict[str, str] = {}
    wf_tables: dict[str, pa.Table] | None = None
    if spec.input_kind == "documents":
        chain = _resolve_documents_input(root, inputs["documents"])
        table = read_view(chain)  # 1. union
        used_inputs = {"documents": chain.head}
    else:  # build: one fold.corpus file, read as is
        table, h, cpath = corpus_input(root, inputs["corpus"], fold_config="corpus")
        used_inputs = {"corpus": h["file_id"]}
        wf_tables = {}
        for wf, ref in (joins or {}).items():
            wf_tables[wf], joined[wf] = wf_input(root, wf, ref)
        used_inputs.update(joined)
    rows_in = table.num_rows
    table, folded = join(table, wf_tables, counts)  # 2. join (build only)
    if predicate is not None:
        table = filter_rows(table, predicate)  # 3. filter
    fold: FoldInfo = {"config": config, "filter": predicate if predicate is not None else NONE,
                      "joined": joined, "annotations_folded": folded, "release_config": NONE}
    newest = cpath.name[:16] if spec.input_kind != "documents" else None
    if spec.input_kind == "documents":
        newest = chain.head_name[:16]
        if out is None and config == "compact":
            out = chain.dir
        if spec.output != "chain_of_one" and table.num_rows != rows_in:
            raise ChainError(f"fold.{config} must keep every row ({table.num_rows} of {rows_in})")
    if out is None:
        raise FoldError(f"fold.{config}: name the destination directory (out: data/{spec.output_family}/<series>)")
    parts = [x for x in out.strip("/").split("/") if x]
    if len(parts) not in (2, 3) or parts[0] != DATA or family_of_dir(parts[1]) != spec.output_family:
        raise FoldError(f"fold.{config}: out {out!r} is not data/{spec.output_family}[.<x>][/<series>]")
    series = parts[2] if len(parts) == 3 else None
    out_dir = "/".join(parts)
    after = head_of(root, out_dir) if series is not None else ""
    base = ""
    if spec.input_kind == "documents" and chain.dir == out_dir:
        if not chain.is_dir_head:
            raise HeadMovedError(f"fold.{config}: {inputs['documents']} is not the last file of {out_dir}/")
        base = after
    else:
        check_restart(root, config, out_dir, restart=restart)
    at = run_at if isinstance(run_at, dt.datetime) else (finished_at or dt.datetime.now(dt.UTC))
    if after and (newest is None or after[:16] > newest):  # also after the destination's last file (D381)
        newest = after[:16]
    stamp = tie_guard(at, newest) if not isinstance(run_at, str) else run_at
    dest = make_data_file(parts[1], series, stamp, run_id, "full", comment=comment)
    published = write(root, table, spec, fold, run_id=run_id, plan_name=plan_name,
                      inputs=used_inputs, dest=dest, base=base, after=after,
                      header_extra=header_extra, finished_at=finished_at,
                      on_published=on_published)  # 5. write
    return FoldResult(published, fold, used_inputs, rows_in, table.num_rows, counts)


__all__ = [
    "ANN_FIELDS",
    "CHAINED_READ_STATUSES",
    "HEADER_EXTRA_KEYS",
    "WRITER",
    "FoldError",
    "FoldResult",
    "basis_current",
    "chained_basis",
    "check_restart",
    "corpus_format",
    "corpus_input",
    "current_basis",
    "data_file",
    "filter",
    "filter_rows",
    "fold_config_digest",
    "join",
    "mapping_m",
    "payload_json",
    "read_corpus",
    "run_fold",
    "union",
    "wf_input",
    "write",
]
