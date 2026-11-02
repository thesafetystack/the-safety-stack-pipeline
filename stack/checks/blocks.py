"""Row and block checks across files: X4, X15, X17, X24 (Appendix B, §2.4, C6).

X15, X17 and X24 compare a ``documents`` chain file with its base view
(:meth:`~stack.checks.chain.StoreView.base_view`): "consecutive files" in Appendix B are a file
and the view it was written over, so the whole chain is covered file by file. A file whose base
walk leaves the recorded files is a finding (:func:`~stack.checks.chain.base_gap`).
Each reads only the columns it compares, the base view restricted to the file's keys
(:meth:`~stack.checks.chain.StoreView.base_pairs`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack import duck
from stack.checks import Finding, register
from stack.checks.chain import BATCH_ROWS, StoreView, base_gap
from stack.schemas.blocks import BLOCK_NAMES
from stack.store import chain as store_chain

_EXAMPLES = 5
#: Blocks that carry their own ``run_id``/``updated_at`` stamps (A.3); ``annotations`` holds
#: per-workflow ``ANN`` entries instead.
STAMPED_BLOCKS: tuple[str, ...] = ("identity", "metadata", "inband", "content")
_STAMPS = ("run_id", "updated_at")


def _finding(check: str, message: str, bad: list[str]) -> list[Finding]:
    return [Finding(check, "hard", message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- X4
#: What X4 reads of a view: the state and each member's id and liveness, out of ``identity``.
X4_FIELDS: tuple[str, ...] = ("doc_id", "state", "identity.members.source_id", "identity.members.is_live")


def _shared_live_sources(tbl: pa.Table, name: str) -> list[str]:
    """X4's second clause over one view, columnar: the live members of the not-``retired`` rows,
    flattened, grouped by ``source_id``; only a source in more than one document reaches Python."""
    ident = tbl.column("identity")
    if ident.null_count:  # the row-wise check raised on a NULL identity; so does this one (a check_error)
        raise TypeError(f"{name}: {ident.null_count} row(s) with a NULL identity block")
    keep = pc.invert(pc.fill_null(pc.equal(tbl.column("state"), "retired"), False))
    tbl = tbl.filter(keep)
    members = pc.struct_field(tbl.column("identity"), "members").combine_chunks()
    flat = pc.list_flatten(members)
    owner = pc.take(tbl.column("doc_id"), pc.list_parent_indices(members))
    live = pa.table({"source_id": pc.struct_field(flat, "source_id"), "doc_id": owner}).filter(
        pc.struct_field(flat, "is_live"))
    per = live.group_by("source_id").aggregate([("doc_id", "count_distinct")])
    shared = per.filter(pc.greater(per.column("doc_id_count_distinct"), 1)).column("source_id")
    if len(shared) == 0:
        return []
    hits = live.filter(pc.is_in(live.column("source_id"), value_set=shared))
    docs: dict[str, set[str]] = defaultdict(set)
    for sid, d in zip(hits.column("source_id").to_pylist(), hits.column("doc_id").to_pylist(), strict=True):
        docs[sid].add(d)
    return [f"{name}: live source {sid} in {len(ds)} live documents {sorted(ds)}" for sid, ds in sorted(docs.items())]


@register("X4", "cardinalities", scope="store")
def cardinalities(view: StoreView) -> list[Finding]:
    """X4: ``doc_id`` at most once per ``documents``/``corpus`` file; a live ``source_id``
    (``MEMBER.is_live``) in at most one live (not ``retired``) document of a chain view — each
    resolvable ``documents`` chain's view and each ``corpus`` file (its own view). Each view is read
    projected to :data:`X4_FIELDS` and counted columnar (:func:`_shared_live_sources`)."""
    bad: list[str] = []
    corpora = []
    for df in view.files:
        spec = df.spec
        if spec is None or spec.kind not in ("documents", "corpus"):
            continue
        tbl = view.read(df, ["doc_id"])
        if tbl is None:
            continue
        ids = tbl.column("doc_id")
        if len(pc.unique(ids)) != len(ids):
            bad += [f"{df.file_id}: doc_id {d} more than once"
                    for d, c in Counter(ids.to_pylist()).items() if c > 1]
        if spec.kind == "corpus":
            corpora.append(df)
    views: list[tuple[str, Callable[[], pa.Table | None]]] = []
    for df in corpora:
        views.append((df.file_id, lambda df=df: view.read(df, X4_FIELDS)))  # type: ignore[misc]
    for chain_id, files in view.chains().items():
        if files[0].spec is None or files[0].spec.kind != "documents":
            continue
        res = view.resolve(chain_id)
        if isinstance(res, store_chain.ChainError):
            continue  # V11
        views.append((chain_id, lambda res=res: view.chain_view(res, X4_FIELDS)))  # type: ignore[misc]
    for name, read in views:  # one view in memory at a time
        tbl = read()
        if tbl is None:
            continue  # a file whose data does not read (S1's finding)
        bad += _shared_live_sources(tbl, name)
    return _finding("cardinalities", "{n} cardinality violation(s)", bad)


# ---------------------------------------------------------------- X15
@register("X15", "block_ownership", scope="store")
def block_ownership(view: StoreView) -> list[Finding]:
    """X15 (§2.4): in a chain-stage file (``blocks_written`` a list), every block outside
    ``blocks_written`` ``IS NOT DISTINCT FROM`` the base view's for each ``doc_id`` in both
    (null-safe: a write into a NULL block is caught, which plain ``=`` misses). duckdb over the
    file and its base view. Rows new to the chain and chains of one (D14) are not compared."""
    bad: list[str] = []
    for df in view.pair_files("X15"):
        written = df.blocks_written
        if written is None:
            continue  # '*': a non-chain writer (fold.compact) owns every block
        others = [b for b in BLOCK_NAMES if b not in written]
        if others and view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        if not others or not view.readable(df) or not view.has_base(df):
            continue
        keys = view.read(df, ["doc_id"])
        assert keys is not None
        ids = keys.column("doc_id").unique()
        for blk in others:  # one block of the file and of its base view in memory at a time
            tbl = view.read(df, ["doc_id", blk])
            base = view.base_view(df, ["doc_id", blk], keys=ids)
            if tbl is None or base is None:
                break
            con = duck.connect()
            try:
                con.register("f", tbl)
                con.register("b", base)
                c = _ident(blk)
                rows = con.execute(
                    f"SELECT f.doc_id FROM f JOIN b USING (doc_id) "
                    f"WHERE f.{c} IS DISTINCT FROM b.{c} ORDER BY f.doc_id").fetchall()
            finally:
                con.close()
            bad += [f"{df.file_id}: {r[0]} changes {blk} (writes {written})" for r in rows]
            del tbl, base
    return _finding("block_ownership", "{n} block(s) changed outside blocks_written", bad)


# ---------------------------------------------------------------- X17
def _unstamped(block: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if k not in _STAMPS}


@register("X17", "created_preserved", scope="store")
def created_preserved(view: StoreView) -> list[Finding]:
    """X17 (C6): per ``doc_id``, between a file and its base view: ``created_at`` unchanged;
    ``updated_at`` not earlier; a stamped block (``identity``, ``metadata``, ``inband``,
    ``content``) whose content is unchanged keeps its ``run_id`` and ``updated_at``.

    Columnar first (:meth:`StoreView.base_join`): the two row stamps and each block's two stamps
    only. A block can be "unchanged but restamped" only on a row whose block stamps differ, so the
    blocks themselves are compared (as Python values, as before) on those rows alone, one block
    column at a time; the findings and their order are the row-wise check's."""
    bad: list[str] = []
    stamp_paths = [f"{blk}.{s}" for blk in STAMPED_BLOCKS for s in _STAMPS]
    for df in view.pair_files("X17"):
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        j = view.base_join(df, ["created_at", "updated_at", *stamp_paths])
        if j is None or j.cur.num_rows == 0:
            continue
        ids = j.cur.column("doc_id").to_pylist()
        per_row: list[list[str]] = [[] for _ in ids]
        c_cr, p_cr = j.cur.column("created_at").to_pylist(), j.base.column("created_at").to_pylist()
        c_up, p_up = j.cur.column("updated_at").to_pylist(), j.base.column("updated_at").to_pylist()
        for i, d in enumerate(ids):
            if c_cr[i] != p_cr[i]:
                per_row[i].append(f"{df.file_id}: {d} created_at {p_cr[i]} -> {c_cr[i]}")
            if c_up[i] < p_up[i]:
                per_row[i].append(f"{df.file_id}: {d} updated_at goes back")
        for blk in STAMPED_BLOCKS:
            restamped = None
            for s in _STAMPS:
                a, b = (pc.struct_field(t.column(blk), s) for t in (j.base, j.cur))
                diff = pc.fill_null(pc.not_equal(a, b), pc.xor(pc.is_null(a), pc.is_null(b)))
                restamped = diff if restamped is None else pc.or_(restamped, diff)
            both = pc.and_(pc.is_valid(j.cur.column(blk)), pc.is_valid(j.base.column(blk)))
            at = pc.indices_nonzero(pc.and_(restamped, both)).to_pylist()
            if not at:
                continue
            keys = pa.array([ids[i] for i in at], pa.string())
            cur = view.read(df, ["doc_id", blk])
            base = view.base_view(df, ["doc_id", blk], keys=keys)
            assert cur is not None and base is not None  # base_join read both
            cur_rows = cur.take(pc.index_in(keys, value_set=cur.column("doc_id"))).column(blk).to_pylist()
            base_rows = base.take(pc.index_in(keys, value_set=base.column("doc_id"))).column(blk).to_pylist()
            del cur, base
            for i, a, b in zip(at, base_rows, cur_rows, strict=True):
                if _unstamped(a) != _unstamped(b):
                    continue
                if any(a[s] != b[s] for s in _STAMPS):
                    per_row[i].append(f"{df.file_id}: {ids[i]} untouched {blk} restamped "
                                      f"{a['run_id']} -> {b['run_id']}")
        bad += [m for msgs in per_row for m in msgs]
    return _finding("created_preserved", "{n} stamp violation(s)", bad)


# ---------------------------------------------------------------- X24
#: What X24 reads of ``content`` (projected, :meth:`StoreView.base_join`): the two digests, and per
#: role and snapshot the identity, status and attempt counter it compares.
X24_FIELDS: tuple[str, ...] = (
    "content.convert_pin", "content.members_digest",
    "content.roles.source_id", "content.roles.content_role", "content.roles.status", "content.roles.attempts",
    "content.snapshots.snapshot_id", "content.snapshots.text_status", "content.snapshots.convert_pin",
    "content.snapshots.convert_attempts",
)


@register("X24", "attempts_monotone", scope="store")
def attempts_monotone(view: StoreView) -> list[Finding]:
    """X24: between a file and its base view, a role's ``attempts`` (per ``(source_id,
    content_role)``) and a snapshot's ``convert_attempts`` (per ``snapshot_id``) never decrease,
    except when either side is ``ok`` or the basis/config changed (role: ``content.convert_pin``
    or ``content.members_digest``; snapshot: ``convert_pin``)."""
    bad: list[str] = []
    for df in view.pair_files("X24"):
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        for d, cur, prev in view.base_pairs(df, X24_FIELDS):
            a, b = prev["content"], cur["content"]
            if a is None or b is None:
                continue
            if a["convert_pin"] == b["convert_pin"] and a["members_digest"] == b["members_digest"]:
                before = {(r["source_id"], r["content_role"]): r for r in a["roles"]}
                for r in b["roles"]:
                    p = before.get((r["source_id"], r["content_role"]))
                    if p is None or "ok" in (p["status"], r["status"]):
                        continue
                    if r["attempts"] < p["attempts"]:
                        bad.append(f"{df.file_id}: {d} role {r['content_role']}/{r['source_id']} "
                                   f"attempts {p['attempts']} -> {r['attempts']}")
            snaps = {s["snapshot_id"]: s for s in a["snapshots"]}
            for s in b["snapshots"]:
                p = snaps.get(s["snapshot_id"])
                if p is None or "ok" in (p["text_status"], s["text_status"]):
                    continue
                if p["convert_pin"] != s["convert_pin"]:
                    continue
                if s["convert_attempts"] < p["convert_attempts"]:
                    bad.append(f"{df.file_id}: {d} snapshot {s['snapshot_id']} convert_attempts "
                               f"{p['convert_attempts']} -> {s['convert_attempts']}")
    return _finding("attempts_monotone", "{n} attempt counter(s) decreased", bad)
