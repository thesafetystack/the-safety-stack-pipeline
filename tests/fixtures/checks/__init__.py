"""One failing store per implemented check (Appendix B), built from ``tests/synth/rows.py``.

Form (uniform, as the v4 design mock ``v4e``): :func:`clean_world` describes a small valid store as a
list of :class:`FileSpec` (rows + header keywords per file, in write order) plus a build record;
each fixture in :data:`FIXTURES` is a function that edits that description so that exactly its
check fails; :func:`write_world` writes it into a store root: every file through the family
writer (:func:`stack.store.parquet.write_family`), one ``ok`` run record per run with the real
file digests, and the build record at its D16 path. A world with a ``gate`` (:func:`id_gate`)
has it stored in the store's blobs as the triage stage stores it
(:func:`stack.gate.store_gate`) and the build record's ``gate_digest`` set to its digest, so X14
recomputes the stand-in triage rows against a real gate; a build record naming a
``gate_digest`` the store does not hold is refused (X14 would only say ``skipped[no_gate]``).

S-check fixtures cannot pass the writer: :func:`write_world` records the writer's refusal
(``World.refusals``) and, only when the fixture set ``World.raw_ok``, writes the file by hand
with its header, the way a bug or a hand edit would put it under ``data/``.

The clean store: a ``documents`` chain F1 gather full (docs 0-5; doc 5 merged into doc 4) → F2
triage part (docs 0-3) → F3 acquire part (docs 0-2; doc 2 failed twice) → F4 ``fold.compact``
full (the union of F1-F3) → F5 gather part (doc 6); ``sources.arxiv`` and ``coverage.arxiv``
chains of one full file each; a ``screen`` file with its evidence; a ``corpus`` file (the
acquired rows of the view at F5); a build record pinning F5 and the collector heads.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

from stack.annotate.evidence import with_digests as with_evidence_digests
from stack.checks.chain import StoreView
from stack.fold import WRITER as FOLD_WRITER
from stack.gate import load_gate, store_gate
from stack.plan import Gate
from stack.runs import RunRecord, file_digest, run_record_path, write_json_zst
from stack.schemas.families import family
from stack.schemas.payloads.registry import payload
from stack.store.blobs import open_blobs
from stack.store.layout import HEADER_PREFIX, NONE, StoreLayout, build_name, make_head
from stack.store.parquet import SchemaRefused, write_family
from tests.synth.rows import (
    AT,
    _ann,
    _content,
    coverage_row,
    data_file,
    dg,
    document_row,
    header,
    mint,
    rid,
    screen_evidence_row,
    screen_row,
    sid,
    source_row,
    table,
    with_digests,
)

Row = dict[str, Any]
GATHER = ["identity", "metadata", "inband"]


def T(run: int) -> dt.datetime:
    """The clock of run ``run``: one hour per run after ``AT``."""
    return AT + dt.timedelta(hours=run)


def fid(run: int, fam: str = "documents") -> str:
    return f"{rid(run)}.{fam}"


@dataclass
class FileSpec:
    """One file to write. ``rows`` is a list, or ``derive(store_root) -> rows`` for files made
    from the written store (compaction, corpus); ``edits`` run on the rows after derivation;
    ``table_fn`` replaces the table (a wrong schema, S1)."""

    key: str
    family: str
    run: int
    header: dict[str, Any]
    rows: list[Row] | None = None
    derive: Callable[[Path], list[Row]] | None = None
    edits: list[Callable[[list[Row]], None]] = field(default_factory=list)
    table_fn: Callable[[list[Row]], pa.Table] | None = None
    raw_table_fn: Callable[[list[Row]], pa.Table] | None = None
    raw_header: dict[str, str] = field(default_factory=dict)


@dataclass
class World:
    files: list[FileSpec]
    build: dict[str, Any] | None
    failed_runs: set[int] = field(default_factory=set)
    raw_ok: bool = False
    refusals: list[tuple[str, set[str]]] = field(default_factory=list)
    #: The gate the stand-in triage rows were decided under (stored; the build's ``gate_digest``).
    gate: Gate | None = None

    def file(self, key: str) -> FileSpec:
        return next(f for f in self.files if f.key == key)

    def rows(self, key: str) -> list[Row]:
        rows = self.file(key).rows
        assert rows is not None, f"{key} is derived; edit it with FileSpec.edits"
        return rows

    def doc(self, key: str, i: int) -> Row:
        return next(r for r in self.rows(key) if r["doc_id"] == sid(i))


# ---------------------------------------------------------------- rows of the clean store
def _stamp(row: Row, run: int, *blocks: str) -> Row:
    row["run_id"], row["updated_at"] = rid(run), T(run)
    for b in blocks:
        row[b]["run_id"], row[b]["updated_at"] = rid(run), T(run)
    return row


def _gathered(i: int, run: int) -> Row:
    row = document_row(i, run)
    row["created_at"] = T(run)
    return _stamp(with_digests(row), run, *GATHER)


def _triaged(prev: Row, run: int) -> Row:
    row = copy.deepcopy(prev)
    i = int(row["identity"]["members"][0]["key_value"].split(".")[1])
    row["state"] = "triaged"
    row["annotations"]["triage"] = document_row(i, run, state="triaged")["annotations"]["triage"]
    row["annotations"]["triage"]["basis_digest"] = row["metadata"]["fields_digest"]
    return _stamp(row, run)


def _acquired(prev: Row, run: int, *, failed_attempts: int | None = None) -> Row:
    row = copy.deepcopy(prev)
    i = int(row["identity"]["members"][0]["key_value"].split(".")[1])
    row["state"] = "acquired"
    c = _content(i, run)
    c["members_digest"] = row["identity"]["members_digest"]
    if failed_attempts is not None:
        c["snapshots"] = []
        c["roles"][0].update(status="failed_transient", error_class="http_5xx", error="503",
                             attempts=failed_attempts, last_outcome="failed_transient",
                             snapshot_id=None)
        c.update(current_snapshot_id=None, current_source_id=None, current_role=None,
                 current_text_sha256=None, current_text_status="no_raw",
                 current_text_media=None, current_revision=None)
    row["content"] = c
    return _stamp(row, run, "content")


def view_rows(root: Path, at: str) -> list[Row]:
    """The chain view as of file ``at`` (union-and-select), as rows."""
    tbl = StoreView(root, build={}).view_at(at)
    assert tbl is not None, f"no view at {at}"
    return tbl.to_pylist()


def _doc_header(stage: str, run: int, kind: str, base: str, **kw: Any) -> dict[str, Any]:
    h: dict[str, Any] = {"stage": stage, "chain_id": "documents", "kind": kind,
                         "base_file_id": base, **kw}
    if stage in ("gather", "triage", "acquire"):
        h.setdefault("blocks_written", {"gather": GATHER, "triage": ["annotations"],
                                        "acquire": ["content"]}[stage])
    else:
        h["blocks_written"] = "*"
    return h


def clean_world() -> World:
    f1 = [_gathered(i, 1) for i in range(6)]
    # doc 5 merged into doc 4: a tombstone; the survivor takes its member and lists it.
    d4, d5 = f1[4], f1[5]
    d5["state"], d5["identity"]["merged_into"] = "retired", sid(4)
    d5["identity"]["members"][0]["is_live"] = False
    m = copy.deepcopy(d5["identity"]["members"][0])
    m.update(edge_kind="key:doi", edge_to_source_id=sid(4), is_live=True)
    d4["identity"]["members"].append(m)
    d4["identity"]["member_ids"].append(sid(5))
    d4["identity"]["former_doc_ids"] = [sid(5)]
    with_digests(d4)
    with_digests(d5)
    f2 = [_triaged(r, 2) for r in f1[:4]]
    f3 = [_acquired(f2[0], 3), _acquired(f2[1], 3), _acquired(f2[2], 3, failed_attempts=2)]
    f5 = [_gathered(6, 5)]
    files = [
        FileSpec("F1", "documents", 1, _doc_header("gather", 1, "full", NONE), f1),
        FileSpec("F2", "documents", 2, _doc_header("triage", 2, "part", fid(1)), f2),
        FileSpec("F3", "documents", 3, _doc_header("acquire", 3, "part", fid(2)), f3),
        FileSpec("F4", "documents", 4, _doc_header(
            "fold.compact", 4, "full", fid(3), writer=FOLD_WRITER, fold_config="compact",
            filter="none", annotations_folded=[]), derive=lambda root: view_rows(root, fid(3))),
        FileSpec("F5", "documents", 5, _doc_header("gather", 5, "part", fid(4)), f5),
        FileSpec("sources", "sources", 6, {"stage": "collect.arxiv", "chain_id": "sources.arxiv",
                                           "kind": "full", "base_file_id": NONE},
                 [source_row(i, 6) for i in range(3)]),
        FileSpec("coverage", "coverage", 6, {"stage": "collect.arxiv",
                                             "chain_id": "coverage.arxiv"}, [coverage_row(0, 6)]),
        FileSpec("screen", "screen", 7, {"stage": "annotate.screen", "basis_kind": "text"},
                 [screen_row(i, 7) for i in range(2)]),
        FileSpec("screen_evidence", "screen_evidence", 7, {"stage": "annotate.screen"},
                 [with_evidence_digests(screen_evidence_row(i, 7), payload("screen")) for i in range(2)]),
        FileSpec("corpus", "corpus", 8, {"stage": "fold.corpus", "writer": FOLD_WRITER,
                                         "fold_config": "corpus", "filter": "has_text",
                                         "annotations_folded": ["triage"]},
                 derive=lambda root: [r for r in view_rows(root, fid(5))
                                      if r["state"] == "acquired"]),
    ]
    build = {"build_id": mint("b", 1), "plan": "dev-v2", "status": "ok",
             "version_chain": [fid(4), fid(5), fid(6, "sources"), fid(6, "coverage")],
             "files": {"documents": {"file_id": fid(5)},
                       "sources/sources.arxiv": {"file_id": fid(6, "sources")},
                       "coverage/coverage.arxiv": {"file_id": fid(6, "coverage")}}}
    return World(files, build)


def id_gate(selected: Mapping[str, Iterable[str]]) -> Gate:
    """A sql-only gate with one clause per consumer selecting exactly the given ``doc_id``s
    (``FALSE`` for none): the gate stand-in triage rows that say ``fetch``/``wanted_by`` for
    those docs and ``skip`` for the rest recompute under (X14)."""
    def sql(ids: Iterable[str]) -> str:
        quoted = ", ".join(f"'{d}'" for d in sorted(ids))
        return f"doc_id IN ({quoted})" if quoted else "FALSE"
    return Gate.model_validate({c: [{"sql": sql(ids)}] for c, ids in selected.items()})


# ---------------------------------------------------------------- writing
def _write_raw(path: Path, tbl: pa.Table, h: dict[str, str]) -> None:
    meta = {f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in h.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl.replace_schema_metadata(meta), path, compression="zstd")


#: Placeholder body after the label line: one paragraph of a paper body's length, so acquire's
#: quality gate (round 3, Q; a paper body is flagged below 8,000 chars) passes a placeholder text.
_FILLER = ("A synthetic paragraph standing in for the converted text of this document in the test world. " * 100).encode()


def write_blobs(root: Path, rows: list[Row]) -> None:
    """T5.1b (X5): a placeholder blob under ``blobs/`` for every ``BLOB`` the ``documents``/``corpus``
    ``rows`` reference, of the size the reference records, as a store holding its content has (the
    synthetic digests are labels, not hashes of these bytes; X5 checks resolution and sizes, not
    content)."""
    want: dict[str, int] = {}
    for r in rows:
        c = r.get("content") or {}
        for h in [*((r.get("inband") or {}).get("entries") or []), *(c.get("snapshots") or [])]:
            for b in (h.get("raw"), h.get("text")):
                if b and b.get("sha256"):
                    want[b["sha256"]] = b["bytes"]
    for digest, size in want.items():
        p = root / "blobs" / digest
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            body = f"synthetic blob {digest}\n".encode() + _FILLER
            p.write_bytes(body[:size].ljust(size, b"."))


def write_world(world: World, root: Path) -> Path:
    """Write ``world`` into the store at ``root``; returns ``root``."""
    layout = StoreLayout(root).ensure()
    outputs: dict[int, dict[str, Any]] = {}
    stages: dict[int, tuple[str, str]] = {}
    names: dict[str, str] = {}  # file id -> file name, written so far

    def flush() -> None:
        """(Re)write every run's record so far: chains resolve from records (D345), and a derived
        file reads the view of the files before it. A fixture rewrites its final records."""
        for run, outs in sorted(outputs.items()):
            failed = run in world.failed_runs
            rec = RunRecord(
                run_id=rid(run), stage=stages[run][0], writer=stages[run][1], plan="dev-v2",
                status="failed" if failed else "ok", error_class="crashed" if failed else None,
                started_at=T(run), heartbeat_at=T(run), finished_at=T(run), code_digest=dg("code"),
                config_digest=dg("config"), scope_complete=True, outputs=outs)
            write_json_zst(run_record_path(root, rec), rec.to_json())

    for spec in world.files:
        if spec.derive is not None:
            flush()
        rows = spec.rows if spec.derive is None else spec.derive(root)
        assert rows is not None
        rows = copy.deepcopy(rows)
        for edit in spec.edits:
            edit(rows)
        if spec.family in ("documents", "corpus"):
            write_blobs(root, rows)
        tbl = spec.table_fn(rows) if spec.table_fn else table(spec.family, rows)
        hdr_tbl = table(spec.family, rows) if spec.table_fn else tbl
        kw = dict(spec.header)
        bfid = kw.get("base_file_id", NONE)
        if bfid in names and "base" not in kw:  # the shorthand base by file id -> its name (D372)
            del kw["base_file_id"]
            kw["base"] = kw["after"] = names[bfid]
        h = header(spec.family, hdr_tbl, run=spec.run, **kw)
        h.update(spec.raw_header)  # header strings header() fixes (T4.3a: `inputs`)
        path = layout.data_file(data_file(h))
        try:
            write_family(path, tbl, h)
        except SchemaRefused as exc:
            world.refusals.append((spec.key, exc.checks))
            if not world.raw_ok:
                raise
            _write_raw(path, spec.raw_table_fn(rows) if spec.raw_table_fn else tbl, h)
        stages[spec.run] = (h["stage"], h["writer"])
        outputs.setdefault(spec.run, {})[spec.family] = {
            "file_id": h["file_id"], "path": path.relative_to(root).as_posix(),
            "file_digest": file_digest(path), "row_count": tbl.num_rows,
            "schema_digest": h.get("schema_digest", family(spec.family).schema_digest),
            # the chain link (D322): chains resolve from these records (D345)
            "chain_id": h["chain_id"], "series": h["series"], "kind": h["kind"], "base": h["base"],
            "after": h["after"], "comment": h["comment"]}
        names[h["file_id"]] = path.name
    flush()
    if world.build is not None:
        b = world.build
        blobs = open_blobs(root)
        if world.gate is not None:
            b["gate_digest"] = store_gate(blobs, world.gate)
        if b.get("gate_digest") and load_gate(blobs, str(b["gate_digest"])) is None:
            raise ValueError(f"build record names gate_digest {b['gate_digest']} that the store "
                             "does not hold: give the World its gate (id_gate)")
        path = layout.build_json(build_name(make_head(T(10), b["build_id"], b.get("comment")), b["plan"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(b).encode()))
    return root


# ---------------------------------------------------------------- one failing fixture per check
def _raw(w: World) -> None:
    w.raw_ok = True


def s1(w: World) -> None:  # `attempts` int64 (Appendix B's example), in a chain of one
    _raw(w)
    spec = family("screen")
    bad = pa.schema([pa.field(f.name, pa.int64(), f.nullable) if f.name == "attempts" else f
                     for f in spec.schema])
    w.file("screen").table_fn = lambda rows: pa.Table.from_pylist(rows, schema=bad)


def _relaxed(t: pa.DataType) -> pa.DataType:
    if pa.types.is_struct(t):
        return pa.struct([pa.field(f.name, _relaxed(f.type), True) for f in t])
    if pa.types.is_list(t):
        return pa.list_(pa.field("item", _relaxed(t.value_type), True))
    return t


def s2(w: World) -> None:  # NULL in a non-nullable column
    # pyarrow itself refuses to write a NULL into a non-nullable parquet column, so a file with
    # one can only reach data/ through another writer that declared the column nullable: S1
    # reports the schema, and verify still runs S2 on a nullability-only difference.
    _raw(w)
    spec = family("screen")
    loose = pa.schema([pa.field(f.name, _relaxed(f.type), True) for f in spec.schema])
    w.rows("screen")[0]["config_digest"] = None
    w.file("screen").raw_table_fn = lambda rows: pa.Table.from_pylist(rows, schema=loose)


def s3(w: World) -> None:  # a key twice in one file
    _raw(w)
    w.rows("screen").append(copy.deepcopy(w.rows("screen")[0]))


def s4(w: World) -> None:  # `ok` with `dead`
    _raw(w)
    w.rows("screen")[0]["error_class"] = "dead"


def s5(w: World) -> None:  # a role twice
    _raw(w)
    c = w.doc("F3", 0)["content"]
    c["roles"] = c["roles"] * 2


def s6(w: World) -> None:  # a malformed digest
    _raw(w)
    w.rows("screen")[0]["result_digest"] = "abc"


def s7(w: World) -> None:  # an evidence file that is a part, not always full (D62, D372)
    _raw(w)
    w.file("screen_evidence").header.update(kind="part", base_file_id=fid(7, "screen"))


def s8(w: World) -> None:  # `triaged` without annotations.triage
    _raw(w)
    w.doc("F2", 3)["annotations"]["triage"] = None


def s9(w: World) -> None:  # `acquired` after a member joined (content on the old membership)
    _raw(w)
    w.doc("F3", 1)["content"]["members_digest"] = dg(("members", "before the join"))


def s10(w: World) -> None:  # `screen` both typed and in other[] (corpus row)
    _raw(w)

    def edit(rows: list[Row]) -> None:
        ann = rows[0]["annotations"]
        ann["screen"] = _ann("screen", {
            "verdict": "relevant", "basis_role": "full_text", "buckets": [], "quotes": [],
            "off_topic_reason": None, "confidence": None, "reasoning": None,
            "adjudication_tier": None, "tier1_verdict": None, "tier1_model": None},
            basis_kind="text")
        ann["other"] = [{**_ann("screen", None, basis_kind="text"),
                         "payload": {"item_key": "", "payload_json": "{}"}}]

    w.file("corpus").edits.append(edit)


def v1(w: World) -> None:  # a miss with `ok` (Appendix B's example)
    w.rows("sources")[0].update(state="missing", record_format="miss", status="ok")


def v2(w: World) -> None:  # an exhausted window with a cursor
    w.rows("coverage")[0].update(is_exhausted=True, next_cursor="page=3")


def v3(w: World) -> None:  # `retired` with no merged_into, not a split
    w.doc("F1", 5)["identity"]["merged_into"] = None
    w.doc("F1", 4)["identity"]["former_doc_ids"] = []


def v4(w: World) -> None:  # stale fields_digest
    w.doc("F5", 6)["metadata"]["fields_digest"] = dg("stale fields")


def v11(w: World) -> None:  # two files on one base (F5 and F5b both extend F4)
    w.files.append(FileSpec("F5b", "documents", 9, _doc_header("gather", 9, "part", fid(4)),
                            [_gathered(7, 9)]))


def v12(w: World) -> None:  # a compaction that is not the union (a value changed)
    def edit(rows: list[Row]) -> None:
        next(r for r in rows if r["doc_id"] == sid(1))["metadata"]["sensitivity"] = ["pii"]

    w.file("F4").edits.append(edit)


def x1(w: World) -> None:  # a failed run's files under data/
    w.failed_runs.add(7)


def x4(w: World) -> None:  # a live source in two live documents
    d6 = w.doc("F5", 6)
    m = copy.deepcopy(w.doc("F1", 0)["identity"]["members"][0])
    m.update(edge_kind="key:doi", edge_to_source_id=sid(6))
    d6["identity"]["members"].append(m)
    d6["identity"]["member_ids"].append(sid(0))
    with_digests(d6)


def x10(w: World) -> None:  # version_chain not in header order
    assert w.build is not None
    vc = w.build["version_chain"]
    vc[0], vc[1] = vc[1], vc[0]


def x15(w: World) -> None:  # a triage part changing `content`
    w.rows("F2")[0]["content"] = copy.deepcopy(w.doc("F3", 0)["content"])


def x17(w: World) -> None:  # `created_at` reset by gather (doc 1 rewritten by F5)
    f5 = w.file("F5")
    own = f5.rows or []
    f5.rows = None
    f5.derive = lambda root: [*copy.deepcopy(own), {
        **next(r for r in view_rows(root, fid(4)) if r["doc_id"] == sid(1)),
        "created_at": T(5), "run_id": rid(5), "updated_at": T(5)}]


def x22(w: World) -> None:  # a full gather file on F5 that drops doc 2
    w.files.append(FileSpec(
        "F6", "documents", 9, _doc_header("gather", 9, "full", fid(5)),
        derive=lambda root: [r for r in view_rows(root, fid(5)) if r["doc_id"] != sid(2)]))


def x23(w: World) -> None:  # F6's base is a published file of another directory (its `after` is F5: the walk holds)
    from stack.store.layout import make_data_file

    _raw(w)  # the writer refuses base != after (S7): X23 can only fire beside kv_complete (ALSO_RED)
    # documents/other/ starts with a full of F5's version, so every check walking F6's base sees F5's rows
    w.files.append(FileSpec("Fo", "documents", 33, _doc_header("gather", 33, "full", NONE, series="other"),
                            derive=lambda root: view_rows(root, fid(5))))

    def derive(root: Path) -> list[Row]:  # a part repeating one row of F5 unchanged; nothing derives from F6
        return [next(r for r in view_rows(root, fid(5)) if r["doc_id"] == sid(2))]

    w.files.append(FileSpec("F6", "documents", 9, _doc_header("acquire", 9, "part", fid(5)), derive=derive,
                            raw_header={"base": make_data_file("documents", "other", AT, rid(33), "full").name}))


def x24(w: World) -> None:  # attempts reset by a stale-head acquire part
    def derive(root: Path) -> list[Row]:
        row = next(r for r in view_rows(root, fid(5)) if r["doc_id"] == sid(2))
        row["content"]["roles"][0]["attempts"] = 1
        return [_stamp(row, 9, "content")]

    w.files.append(FileSpec("F6", "documents", 9, _doc_header("acquire", 9, "part", fid(5)),
                            derive=derive))


#: Check name → the edit of :func:`clean_world` that fails exactly that check.
FIXTURES: dict[str, Callable[[World], None]] = {
    "schema_equal": s1,
    "nonnull": s2,
    "key_unique": s3,
    "status_pairing": s4,
    "nested_unique": s5,
    "id_formats": s6,
    "kv_complete": s7,
    "state_blocks": s8,
    "digest_agreements": s9,
    "payload_typed": s10,
    "V-sources": v1,
    "V-coverage": v2,
    "V-state": v3,
    "V-digests": v4,
    "V-chain-linear": v11,
    "V-compaction-equal": v12,
    "finished_files": x1,
    "cardinalities": x4,
    "ordering_pinned": x10,
    "block_ownership": x15,
    "created_preserved": x17,
    "full_covers_base": x22,
    "head_descends": x23,
    "attempts_monotone": x24,
}


def build_store(root: Path, fixture: str | None = None) -> World:
    """Write the clean store, or the one failing ``fixture``, under ``root``."""
    world = clean_world()
    if fixture is not None:
        FIXTURES[fixture](world)
    write_world(world, root)
    return world


def read_only(root: Path) -> Path:
    """Make every file and directory under ``root`` read-only (a reader that writes fails loudly); returns ``root``."""
    for p in sorted(root.rglob("*"), reverse=True):
        p.chmod(0o555 if p.is_dir() else 0o444)
    root.chmod(0o555)
    return root


def shared_stores(tmp_path_factory: Any) -> Callable[[str | None], Path]:
    """``fixture -> root``: :func:`build_store` once per fixture name (``None``: the clean store) under
    ``tmp_path_factory``, read-only, for module-scoped fixtures of tests that only read the store (writing one
    costs ~1 s). A test that writes, or that patches what :func:`build_store` writes, builds its own."""
    roots: dict[str | None, Path] = {}

    def get(fixture: str | None = None) -> Path:
        if fixture not in roots:
            root = tmp_path_factory.mktemp(f"store-{fixture or 'clean'}")
            build_store(root, fixture)
            roots[fixture] = read_only(root)
        return roots[fixture]

    return get


FIXTURES.update(__import__("tests.fixtures.checks.track_d", fromlist=["_"]).FIXTURES_D)  # T5.2: V9, V10
FIXTURES.update(__import__("tests.fixtures.checks.content", fromlist=["_"]).FIXTURES_CONTENT)  # T3.1: V6
FIXTURES.update(__import__("tests.fixtures.checks.triage", fromlist=["_"]).FIXTURES_TRIAGE)  # T2.9: V5, X13, X14, X19, X20
FIXTURES.update(__import__("tests.fixtures.checks.annotate", fromlist=["_"]).FIXTURES_ANNOTATE)  # T4.3a: V7, V8, X6, X7, X8
FIXTURES.update(__import__("tests.fixtures.checks.lookup", fromlist=["_"]).FIXTURES_LOOKUP)  # T4.2: X16
