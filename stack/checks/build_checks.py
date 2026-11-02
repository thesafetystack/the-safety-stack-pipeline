"""Cross-build checks over a build's set (Appendix B; T5.1b; D20, D76, readings in D-rows of
track D): X2 ``lineage_equality``, X3 ``expected_scope``, X5 ``blob_resolution``, X9
``fold_recompute``, X11 ``dead_runs_seen`` (warn), X12 ``state_propagation``, X18
``tombstone_closure``, X21 ``carry_regrouped``, X25 ``same_raw_reported``.

**Scope.** The reference build is :attr:`StoreView.build` (the build passed to ``run_checks``,
else ``latest``, the newest ``ok`` or ``incomplete`` build, D703). Its ``documents`` head
(``files.documents``) gives the **build view** (union-and-select up to that head); its ``corpus``, ``corpus_input`` and
``corpus_everything`` entries are the **build's corpus files**; ``runs`` its run records. Without
a build: the ``documents`` chain's current head and every ``corpus`` file under ``data/`` (X5,
X12, X18, X21, X25); X2, X3, X9 need the build's records and report ``skipped[no_builds]``; X11
reads every run record whatever the build.

* **X2** (conductor ruling 2026-10-14, D380) every run of ``runs`` pinned published files:
  each ``in_files`` path is listed by an ``ok`` record, not excluded, with the record's digest, and
  each pinned (``inputs``, ``prior``) chained file id is published. Lineage only for the series
  directories the build wrote (``files``): such a pin is on the walk of the build's last file there,
  and a run that read a directory an earlier run of the same build wrote pinned that output or a
  descendant of it. Pins of directories the build did not write are never a lineage fault. An
  ``annotate.<wf>`` or ``fold.build`` run that records a ``corpus`` input read
  ``files.corpus_input``; a
  ``fold.build`` run joined the build's ``<wf>`` files. The same over the headers (``inputs``) of
  the build's chain-of-one files, so a record that names files its runs did not read fails too.
* **X3** ``require_complete``: per stage of the build that wrote a listed family (outputs, or the
  phase's writes, :func:`stack.build.stage_writes`), its last run ended ``scope_complete``, or the
  build's status is ``incomplete`` and its record lists that stage and run under ``incomplete``
  (round 5, R6-4), or the record lists them under ``incomplete_allowed`` (a plan line with
  ``require_complete: false``, R6 item 1); every stage either list names did end its last run short; and no
  lost component: every ``doc_id`` of a ``documents`` view a ``gather`` run of the build pinned is
  in the build view (live or ``retired``).
* **X5** blob references resolve in the store's flat ``blobs/<sha256>`` (D360), each at the size it
  records (D690): every ``BLOB`` of a row (snapshot and in-band ``raw``/``text``,
  :func:`stack.schemas.structs.blob_refs`) is a blob of ``bytes`` bytes, and ``current_text_sha256``
  resolves; and **at the pin**: an ``ok`` current text whose snapshot was converted (``convert_pin``
  set) was converted at the block's pin (``content.convert_pin``). The bytes are not re-hashed. The store's blobs answer
  (:meth:`stack.store.blobs.BlobStore.held`, presence and size): one LIST of ``blobs/`` on an S3
  store (the remote is the store), the local files on a local-only one. The rows are read projected
  to the fields named here (:data:`X5_FIELDS`).
* **X9** the build's ``fold.corpus`` files recompute from their header ``inputs.documents`` (the
  chain view at that head) and ``filter`` through :func:`stack.fold.filter_rows`; its
  ``fold.build`` file from ``inputs.corpus`` joined (:func:`stack.fold.join`) with the other
  ``inputs`` in ``annotations_folded`` order, then filtered; rows compared by ``doc_id``, whole.
* **X11** (warn) ``repeated_inflight``: one ``inflight`` key on two or more dead run records of
  one stage; dead = the store index's liveness ``dead`` (a ``running`` record whose marker is
  older than :data:`stack.store.index.DEAD_AFTER`, D344) or final ``failed[crashed]``.
* **X12** no masked or ``retired`` row in a ``corpus`` file whose recorded filter is not
  ``everything`` (the build's ``corpus_everything`` is exempt, D20), and none passes ``has_text``
  or ``passed_triage`` (:mod:`stack.query`) in the build view.
* **X18** in the build view: no ``retired`` id is a live row's id; ``merged_into`` is NULL (a
  split) or names a row of the view whose forward chain (``merged_into`` of retired rows) ends at a
  live row; every live row's ``former_doc_ids`` are rows of the view. A retired anchor may be a
  survivor's member (no check).
* **X21** after the last ``acquire`` (no ``gather`` file after the last ``acquire`` file on the
  walk to the head): every unmasked ``triaged``/``acquired`` row with ``content`` has
  ``content.members_digest = identity.members_digest`` (D5's regroup arm); and the merge carry
  (D6): a retired row with ``merged_into`` and ``content`` has every ``snapshot_id`` of its
  ``snapshots[]`` in its survivor's.
* **X25** among non-retired rows with ``content``: each ``acquired`` row lists in
  ``same_raw_doc_ids`` every other row holding a ``raw`` snapshot with one of its raw digests
  (acquire's ``_same_raw`` reading; a superset is allowed between a merge and the next acquire).

Each check reads the build view and the corpus files projected to the columns it reads, and a
one-pass check converts rows to Python :data:`~stack.checks.chain.BATCH_ROWS` at a time.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import Any

import pyarrow as pa

from stack.checks import Finding, register
from stack.checks.chain import BATCH_ROWS, CHAINED_KINDS, DataFile, SkipCheck, StoreView
from stack.query import EVERYTHING, one_statement, resolve_filter, sql_connection
from stack.runs import RunRecord, find_run_record
from stack.schemas.families import FAMILIES
from stack.schemas.structs import blob_refs
from stack.store import chain as store_chain
from stack.store.blobs import blob_key, open_blobs
from stack.store.index import store_index
from stack.store.layout import NONE

Row = dict[str, Any]
_EXAMPLES = 5
#: Build-record keys of the build's corpus files (D17, D67).
CORPUS_KEYS: tuple[str, ...] = ("corpus_input", "corpus_everything", "corpus")


def _finding(check: str, message: str, bad: list[str], level: str = "hard") -> list[Finding]:
    return [Finding(check, level, message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


# ---------------------------------------------------------------- the build's set
def _files(view: StoreView) -> dict[str, dict[str, Any]]:
    return dict((view.build or {}).get("files") or {})


def _need_build(view: StoreView) -> dict[str, Any]:
    if view.build is None:
        raise SkipCheck("no_builds")
    return view.build


def _documents_head(view: StoreView) -> str | None:
    """The build's ``documents`` head, else (no build) the head of the store's one ``documents``
    series directory; ``None`` when there is none, several (nothing is selected, D379) or
    the directory does not resolve (V11's)."""
    if view.families is not None and "documents" not in view.families:
        return None
    if view.build is not None:
        entry = _files(view).get("documents")
        return str(entry["file_id"]) if entry else None
    dirs = [d for d in view.chains() if d.split("/")[1] == "documents"]
    if len(dirs) != 1:
        return None
    res = view.resolve(dirs[0])
    return None if isinstance(res, store_chain.ChainError) else res.head


def _build_view(view: StoreView, columns: list[str]) -> pa.Table | None:
    """The build view in ``columns`` (``None`` when there is no ``documents`` head or it has no
    view)."""
    head = _documents_head(view)
    return view.view_at(head, columns) if head is not None else None


def _batches(tbl: pa.Table | None, columns: list[str] | None = None) -> Iterator[Row]:
    """``tbl``'s rows (``columns`` of them), converted :data:`BATCH_ROWS` at a time."""
    if tbl is None:
        return
    for start in range(0, tbl.num_rows, BATCH_ROWS):
        part = tbl.slice(start, BATCH_ROWS)
        yield from (part.select(columns) if columns is not None else part).to_pylist()


def _build_rows(view: StoreView, columns: list[str]) -> Iterator[Row]:
    """The build view's rows in ``columns`` (none when there is no ``documents`` head or it has
    no view), converted :data:`BATCH_ROWS` at a time."""
    yield from _batches(_build_view(view, columns))


def _corpus_files(view: StoreView) -> list[tuple[str | None, DataFile]]:
    """``(build key, file)`` of the build's corpus files; without a build every selected
    ``corpus`` file (key ``None``)."""
    if view.build is None:
        return [(None, df) for df in view.files if df.family == "corpus"]
    out = []
    for key in CORPUS_KEYS:
        entry = _files(view).get(key)
        df = view.file(str(entry["file_id"])) if entry else None
        if df is not None and view.selected(df):
            out.append((key, df))
    return out


def _inputs(df: DataFile) -> dict[str, str]:
    try:
        v = json.loads(df.header.get("inputs") or "{}")
    except ValueError:
        return {}
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def _runs(view: StoreView) -> Iterator[tuple[str, RunRecord | None]]:
    for run_id in (view.build or {}).get("runs") or []:
        yield str(run_id), find_run_record(view.root, str(run_id))


def _descends(view: StoreView, head: str, ref: str) -> bool | None:
    """Whether ``ref`` is ``head`` or on its base walk (over the recorded files)."""
    seen: set[str] = set()
    cur = head
    while cur != NONE and cur not in seen:
        if cur == ref:
            return True
        seen.add(cur)
        df = view.file(cur)
        if df is None:
            return False
        cur = df.base_file_id
    return False


# ---------------------------------------------------------------- X2
@register("X2", "lineage_equality", scope="store")
def lineage_equality(view: StoreView) -> list[Finding]:
    """X2 (module docstring)."""
    _need_build(view)
    files = _files(view)
    heads = view.build_heads()
    corpus_input = (files.get("corpus_input") or {}).get("file_id")
    bad: list[str] = []

    written: dict[str, str] = {}  # dir -> the newest file an earlier run of this build wrote there

    def on_walk(who: str, fid: str, ordered: bool = True) -> None:
        df = view.file(fid)
        if df is None:  # a file id is ``<run_id>.<family>``
            spec = FAMILIES.get(fid.rsplit(".", 1)[-1])
            if spec is not None and spec.kind in CHAINED_KINDS:
                bad.append(f"{who}: pinned {fid}, which no ok run record names (or it is excluded)")
            return
        if df.is_chain_of_one or df.spec is None or df.spec.kind not in CHAINED_KINDS:
            return
        if not view.selected(df):
            return
        d = view.dir_of(df)
        head = heads.get(d)
        if head is None:
            return  # a directory the build did not write: published is all X2 asks
        if _descends(view, head, fid) is False:
            bad.append(f"{who}: pinned {fid}, not on the walk of the build's head {head} ({d}/)")
        if ordered and d in written and _descends(view, fid, written[d]) is False:
            bad.append(f"{who}: pinned {fid}, not {written[d]} (written to {d}/ earlier in this build) "
                       "nor a descendant of it")

    def pinned_files(who: str, rec: RunRecord) -> None:
        for f in rec.in_files:
            got = view.published.get(f["path"])
            if got is None:
                bad.append(f"{who}: read {f['path']}, which no ok run record lists (or it is excluded)")
            elif got != f["file_digest"]:
                bad.append(f"{who}: read {f['path']} at digest {f['file_digest'][:12]}…, the record's is {got[:12]}…")

    def reads_corpus(who: str, stage: str, got: str | None) -> None:
        if got is None or not (stage.startswith("annotate.") or stage == "fold.build"):
            return  # no corpus input recorded (a stage refuses to run without one)
        if corpus_input is None:
            bad.append(f"{who}: {stage} in a build with no files.corpus_input")
        elif got != corpus_input:
            bad.append(f"{who}: {stage} read corpus {got}, not the build's corpus_input {corpus_input}")

    def joins(who: str, joined: dict[str, str]) -> None:
        for wf, fid in sorted(joined.items()):
            entry = files.get(wf)
            if entry is not None and str(entry["file_id"]) != fid:
                bad.append(f"{who}: joined {wf}={fid}, not the build's {entry['file_id']}")

    for run_id, rec in _runs(view):
        if rec is None:
            bad.append(f"run {run_id}: in the build's runs, no run record")
            continue
        who = f"run {run_id} ({rec.stage})"
        for fid in [*rec.inputs.values(), *rec.prior]:
            on_walk(who, fid)
        pinned_files(who, rec)
        reads_corpus(who, rec.stage, rec.inputs.get("corpus"))
        if rec.stage == "fold.build":
            joins(who, dict((rec.fold or {}).get("joined") or {}))
        if rec.status == "ok":
            for o in rec.outputs.values():
                if o.get("series") and o.get("path"):
                    written[str(o["path"]).rsplit("/", 1)[0]] = str(o["file_id"])
    for key, entry in sorted(files.items()):
        df = view.file(str(entry.get("file_id")))
        if df is None or not df.is_chain_of_one:
            continue  # chained heads: X23; a missing chain-of-one file: X1 over its record
        who = f"files.{key} {df.file_id}"
        ins = _inputs(df)
        for fid in ins.values():
            on_walk(who, fid, ordered=False)  # a header's inputs: the runs' order is checked above
        stage = df.header.get("stage", "")
        reads_corpus(who, stage, ins.get("corpus"))
        if stage == "fold.build":
            joins(who, {k: v for k, v in ins.items() if k != "corpus"})
    return _finding("lineage_equality", "{n} input(s) outside the build's lineage", bad)


# ---------------------------------------------------------------- X3
@register("X3", "expected_scope", scope="store")
def expected_scope(view: StoreView) -> list[Finding]:
    """X3 (module docstring)."""
    # stack.build imports the checks: import at call time
    from stack.build import stage_writes

    build = _need_build(view)
    bad: list[str] = []
    last: dict[str, RunRecord] = {}
    wrote: dict[str, set[str]] = defaultdict(set)
    gathers: list[RunRecord] = []
    for _, rec in _runs(view):
        if rec is None:
            continue  # X2 reports it
        last[rec.stage] = rec
        for key in rec.outputs:
            wrote[key.split("/", 1)[0]].add(rec.stage)
        if rec.stage == "gather":
            gathers.append(rec)
    listed = ({str(e.get("stage")): str(e.get("run_id")) for e in build.get("incomplete") or []}
              if build.get("status") == "incomplete" else {})
    listed.update({str(e.get("stage")): str(e.get("run_id")) for e in build.get("incomplete_allowed") or []})
    short: set[str] = set()
    for fam in build.get("require_complete") or []:
        stages = wrote.get(fam, set()) | {s for s in last if fam in stage_writes(s)}
        for s in sorted(stages):
            if last[s].scope_complete:
                continue
            short.add(s)
            if listed.get(s) != last[s].run_id:
                bad.append(f"require_complete {fam}: {s} ended run {last[s].run_id} scope_complete = false"
                           + (f" (the build's incomplete list names run {listed[s]})" if s in listed else ""))
    bad += [f"incomplete(_allowed) lists {s} run {rid}, but that stage's last run did not end short of a "
            "require_complete family" for s, rid in sorted(listed.items()) if s not in short]
    head = _build_view(view, ["doc_id"])
    final = set(head.column("doc_id").to_pylist()) if head is not None else set()
    for rec in gathers:
        pinned = rec.inputs.get("documents")
        tbl = view.view_at(pinned, ["doc_id"]) if pinned is not None else None
        if tbl is None or not final:
            continue
        lost = sorted(set(tbl.column("doc_id").to_pylist()) - final)
        bad += [f"run {rec.run_id} (gather) pinned {pinned}: {d} not in the build's documents view"
                for d in lost]
    return _finding("expected_scope", "{n} scope fault(s)", bad)


# ---------------------------------------------------------------- X5
def _is_digest(digest: str) -> bool:
    try:
        blob_key(digest)
    except ValueError:
        return False
    return True


#: What X5 reads of a ``documents`` / ``corpus`` row (dotted paths into the blocks).
X5_FIELDS: list[str] = [
    "doc_id", "inband.entries.content_role", "inband.entries.raw", "inband.entries.text",
    "content.snapshots.snapshot_id", "content.snapshots.raw", "content.snapshots.text",
    "content.snapshots.convert_pin", "content.current_snapshot_id",
    "content.current_text_sha256", "content.current_text_status", "content.convert_pin"]


@register("X5", "blob_resolution", scope="store")
def blob_resolution(view: StoreView) -> list[Finding]:
    """X5 (module docstring)."""
    out: list[str | tuple[str, str, str, str, int | None]] = []  # findings and blob needs, in order
    seen: set[tuple[str, str, str, int | None]] = set()
    sets: list[tuple[str, Callable[[], Iterator[Row]]]] = [
        (f"documents@{_documents_head(view)}", lambda: _build_rows(view, X5_FIELDS))]
    for _, df in _corpus_files(view):
        if view.readable(df):
            sets.append((df.file_id, lambda df=df: _batches(view.read(df, X5_FIELDS))))

    def need(where: str, d: str, digest: str | None, what: str, size: int | None = None) -> None:
        if digest is None or (d, what, digest, size) in seen:
            return
        seen.add((d, what, digest, size))
        out.append((where, d, digest, what, size))

    for where, rows in sets:
        for r in rows():
            d = r["doc_id"]
            c = r.get("content")
            for holder, i, part, b in blob_refs(r):
                h = c["snapshots"][i]["snapshot_id"] if holder == "snapshot" else \
                    r["inband"]["entries"][i]["content_role"]
                need(where, d, b["sha256"], f"{holder} {h} {part}", b["bytes"])
            if c is None:
                continue
            snaps = {s["snapshot_id"]: s for s in c["snapshots"] or []}
            need(where, d, c["current_text_sha256"], "current_text_sha256")
            cur = snaps.get(c["current_snapshot_id"] or "")
            cfg = cur.get("convert_pin") if cur is not None else None
            if c["current_text_status"] == "ok" and cfg is not None and cfg != c["convert_pin"]:
                key = (d, "pin", cfg, None)
                if key not in seen:
                    seen.add(key)
                    out.append(f"{where}: {d} current text of {cur['snapshot_id']} converted under "
                               f"{cfg[:12]}…, not the block's pin {str(c['convert_pin'])[:12]}…")
    # one batched question (an s3 store: one LIST of blobs/)
    want = {n[2] for n in out if not isinstance(n, str)}
    held = open_blobs(view.root).held({x for x in want if _is_digest(x)})  # a malformed digest resolves nowhere
    bad: list[str] = []
    for n in out:
        if isinstance(n, str):
            bad.append(n)
            continue
        where, d, digest, what, size = n
        if digest not in held:
            bad.append(f"{where}: {d} {what} {digest[:12]}… not under blobs/")
        elif size is not None and held[digest] != size:
            bad.append(f"{where}: {d} {what} {digest[:12]}… is {held[digest]} bytes under blobs/, the row "
                       f"says {size}")
    return _finding("blob_resolution", "{n} reference(s) not resolving, of the wrong size or not at the pin", bad)


# ---------------------------------------------------------------- X9
def _compare(where: str, got: pa.Table, want: pa.Table, bad: list[str]) -> None:
    """Rows by ``doc_id`` (the last row of an id, as a dict over the rows keeps it), whole,
    :data:`BATCH_ROWS` ids at a time in id order. A batch whose two sides are equal as Arrow
    tables (same schema, values and NULLs) is equal as Python rows too and is not converted; any
    other batch is compared row by row in Python, which names the differing columns."""
    g = {d: i for i, d in enumerate(got.column("doc_id").to_pylist())}
    w = {d: i for i, d in enumerate(want.column("doc_id").to_pylist())}
    bad += [f"{where}: {d} missing (the recomputation has it)" for d in sorted(set(w) - set(g))]
    bad += [f"{where}: {d} not in the recomputation" for d in sorted(set(g) - set(w))]
    common = sorted(set(g) & set(w))
    same_schema = got.schema.equals(want.schema, check_metadata=False)
    for start in range(0, len(common), BATCH_ROWS):
        ds = common[start:start + BATCH_ROWS]
        gt, wt = got.take([g[d] for d in ds]), want.take([w[d] for d in ds])
        if same_schema and gt.equals(wt):
            continue
        gs, ws = gt.to_pylist(), wt.to_pylist()
        for d, gr, wr in zip(ds, gs, ws, strict=True):
            if gr != wr:
                cols = [c for c in gr if gr[c] != wr.get(c)]
                bad.append(f"{where}: {d} differs from the recomputation in {cols}")


def _recompute(view: StoreView, df: DataFile, where: str, bad: list[str]) -> pa.Table | None:
    """The fold ``df`` recomputed from its header ``inputs`` (module docstring), or ``None`` with
    the reason in ``bad``. Its inputs leave the view's cache before it returns."""
    from stack.fold import FoldError, filter_rows, join

    ins, h = _inputs(df), df.header
    pred = h.get("filter") or NONE
    try:
        if h.get("fold_config") == "corpus":
            src = ins.get("documents")
            want = view.view_at(src) if src is not None else None
            if want is None:
                bad.append(f"{where}: input documents={src} has no chain view in data/")
                return None
        elif h.get("fold_config") == "build":
            cdf = view.file(ins.get("corpus", ""))
            base = view.table(cdf) if cdf is not None else None
            if base is None:
                bad.append(f"{where}: input corpus={ins.get('corpus')} not readable in data/")
                return None
            order = json.loads(h.get("annotations_folded") or "[]")
            names = [*order, *sorted(set(ins) - set(order) - {"corpus"})]
            wdfs = {wf: view.file(ins.get(wf, "")) for wf in names}
            wfs = {wf: view.table(wdf) for wf, wdf in wdfs.items() if wdf is not None}
            unread = [f"{wf}={ins.get(wf)}" for wf in names if wfs.get(wf) is None]
            if unread:
                bad.append(f"{where}: joined {unread} not readable in data/")
                return None
            want, _ = join(base, {wf: t for wf, t in wfs.items() if t is not None})
            del base, wfs
        else:
            bad.append(f"{where}: fold_config {h.get('fold_config')!r} is not a corpus fold")
            return None
        view.drop_cache()  # the inputs: only the recomputation is needed from here
        if pred != NONE:
            want = filter_rows(want, pred)
    except (FoldError, ValueError, KeyError) as exc:
        bad.append(f"{where}: does not recompute: {type(exc).__name__}: {exc}")
        return None
    return want


@register("X9", "fold_recompute", scope="store")
def fold_recompute(view: StoreView) -> list[Finding]:
    """X9 (module docstring). One fold at a time: its recomputation, then the stored file, then
    the comparison; nothing of a fold is held when the next one starts."""
    _need_build(view)
    bad: list[str] = []
    for key, df in _corpus_files(view):
        where = f"files.{key} {df.file_id}"
        view.drop_cache()
        if not view.readable(df):
            continue  # S1's finding
        mine: list[str] = []
        want = _recompute(view, df, where, mine)
        got = view.read(df) if want is not None else None
        if want is not None and got is not None:
            _compare(where, got, want, mine)
        bad += mine
        del want, got
    return _finding("fold_recompute", "{n} fold row(s) not recomputing from inputs", bad)


# ---------------------------------------------------------------- X11
@register("X11", "dead_runs_seen", "warn", scope="store")
def dead_runs_seen(view: StoreView) -> list[Finding]:
    """X11 (module docstring); warn level, never fails a build. Dead = ``failed[crashed]`` records
    and ``running`` records the store index sees as ``dead`` (a marker older than
    :data:`stack.store.index.DEAD_AFTER`, D344)."""
    idx = store_index(view.root)
    dead = [e.record for e in idx.runs
            if (e.record.status == "failed" and e.record.error_class == "crashed") or e.liveness == "dead"]
    seen: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rec in dead:
        for key in dict.fromkeys(rec.inflight):
            seen[(rec.stage, key)].append(rec.run_id)
    bad = [f"{stage}: key {key!r} inflight in dead runs {', '.join(ids)}"
           for (stage, key), ids in sorted(seen.items()) if len(ids) >= 2]
    return _finding("dead_runs_seen", "repeated_inflight: {n} key(s) on two or more dead runs", bad, "warn")


# ---------------------------------------------------------------- X12
def _passing(rows: pa.Table, predicate: str) -> list[str]:
    con = sql_connection()
    try:
        con.register("x12_rows", rows)
        return [d for (d,) in con.execute(one_statement(
            f"SELECT doc_id FROM x12_rows d WHERE coalesce(({resolve_filter(predicate)}), false) "
            "ORDER BY doc_id")).fetchall()]
    finally:
        con.close()


@register("X12", "state_propagation", scope="store")
def state_propagation(view: StoreView) -> list[Finding]:
    """X12 (module docstring)."""
    bad: list[str] = []
    everything = (_files(view).get("corpus_everything") or {}).get("file_id")
    for df in view.files:
        if df.family != "corpus" or df.file_id == everything:
            continue
        if resolve_filter(df.header.get("filter") or NONE) == EVERYTHING:
            continue
        tbl = view.read(df, ["doc_id", "state", "identity"])
        if tbl is None:
            continue
        for r in _batches(tbl, ["doc_id", "state", "identity"]):
            if r["state"] == "retired" or r["identity"]["is_masked"]:
                why = "retired" if r["state"] == "retired" else "masked"
                bad.append(f"{df.file_id}: {why} {r['doc_id']} in a corpus filtered "
                           f"{df.header.get('filter')!r}")
    head = _documents_head(view)
    # has_text reads state, identity, content; passed_triage state, identity, annotations
    tbl = view.view_at(head, ["doc_id", "state", "identity", "content", "annotations"]) \
        if head is not None else None
    if tbl is not None:
        held = {r["doc_id"] for r in _batches(tbl, ["doc_id", "state", "identity"])
                if r["state"] == "retired" or r["identity"]["is_masked"]}
        for name in ("has_text", "passed_triage"):
            bad += [f"documents@{head}: masked/retired {d} passes {name}"
                    for d in _passing(tbl, name) if d in held]
    return _finding("state_propagation", "{n} masked or retired row(s) propagated", bad)


# ---------------------------------------------------------------- X18
@register("X18", "tombstone_closure", scope="store")
def tombstone_closure(view: StoreView) -> list[Finding]:
    """X18 (module docstring)."""
    rows = list(_build_rows(view, ["doc_id", "state", "identity"]))
    by = {r["doc_id"]: r for r in rows}
    live = {d for d, r in by.items() if r["state"] != "retired"}
    retired = [r for r in rows if r["state"] == "retired"]
    bad: list[str] = []
    bad += [f"retired {r['doc_id']} is also a live row's doc_id" for r in retired if r["doc_id"] in live]
    for r in retired:
        target = r["identity"]["merged_into"]
        seen = {r["doc_id"]}
        while target is not None and target in by and target not in live and target not in seen:
            seen.add(target)
            target = by[target]["identity"]["merged_into"]
        if target is None:
            continue  # a split tombstone, or forwarded to one
        if target not in by:
            bad.append(f"retired {r['doc_id']}: merged_into {target} is not a document of the view")
        elif target in seen:
            bad.append(f"retired {r['doc_id']}: merged_into cycles at {target}")
    for d in sorted(live):
        bad += [f"{d}: former_doc_ids lists {f}, not a document of the view"
                for f in by[d]["identity"]["former_doc_ids"] or [] if f not in by]
    return _finding("tombstone_closure", "{n} tombstone(s) not closed", bad)


# ---------------------------------------------------------------- X21
def _after_last_acquire(view: StoreView, head: str) -> bool:
    """No ``gather`` file between the last ``acquire`` file and ``head`` on the base walk."""
    seen: set[str] = set()
    cur = head
    while cur != NONE and cur not in seen:
        seen.add(cur)
        df = view.file(cur)
        if df is None:
            return False
        stage = df.header.get("stage", "")
        if stage == "acquire":
            return True
        if stage == "gather":
            return False
        cur = df.base_file_id
    return False


@register("X21", "carry_regrouped", scope="store")
def carry_regrouped(view: StoreView) -> list[Finding]:
    """X21 (module docstring)."""
    head = _documents_head(view)
    tbl = _build_view(view, ["doc_id", "state", "identity", "content"])
    bad: list[str] = []
    if head is not None and _after_last_acquire(view, head):
        for r in _batches(tbl):
            c, ident = r["content"], r["identity"]
            if c is None or r["state"] not in ("triaged", "acquired") or ident["is_masked"]:
                continue
            if c["members_digest"] != ident["members_digest"]:
                bad.append(f"{r['doc_id']}: content.members_digest is not identity's after the last acquire")
    # the merge carry: retired rows with content and a target first, then their targets' snapshots
    ids: set[str] = set()
    carried: list[tuple[str, str, set[str]]] = []
    for r in _batches(tbl):
        ids.add(r["doc_id"])
        c, target = r["content"], r["identity"]["merged_into"]
        if r["state"] == "retired" and c is not None and target is not None:
            carried.append((r["doc_id"], target, {s["snapshot_id"] for s in c["snapshots"] or []}))
    targets = {t for _, t, _ in carried if t in ids}
    snaps: dict[str, set[str]] = {}
    for r in _batches(tbl) if targets else ():
        if r["doc_id"] in targets:  # the last row of an id, as a dict over the rows keeps it
            snaps[r["doc_id"]] = {s["snapshot_id"] for s in (r["content"] or {}).get("snapshots") or []}
    for d, target, mine in carried:
        if target not in ids:
            continue
        missing = sorted(mine - snaps[target])
        if missing:
            bad.append(f"{d} merged into {target}: survivor lacks snapshot(s) {missing[:3]}")
    return _finding("carry_regrouped", "{n} carry fault(s)", bad)


# ---------------------------------------------------------------- X25
@register("X25", "same_raw_reported", scope="store")
def same_raw_reported(view: StoreView) -> list[Finding]:
    """X25 (module docstring)."""
    tbl = _build_view(view, ["doc_id", "state", "content"])

    def rows() -> Iterator[Row]:
        return (r for r in _batches(tbl) if r["state"] != "retired" and r["content"] is not None)

    holders: dict[str, set[str]] = defaultdict(set)
    for r in rows():
        for s in r["content"]["snapshots"] or []:
            if s["kind"] == "raw":
                holders[s["raw"]["sha256"]].add(r["doc_id"])
    bad: list[str] = []
    for r in rows():
        if r["state"] != "acquired":
            continue
        d = r["doc_id"]
        dups = {o for s in r["content"]["snapshots"] or [] if s["kind"] == "raw"
                for o in holders[s["raw"]["sha256"]] if o != d}
        unlisted = sorted(dups - set(r["content"]["same_raw_doc_ids"] or []))
        if unlisted:
            bad.append(f"{d}: shares a raw digest with {unlisted[:3]}, not in same_raw_doc_ids")
    return _finding("same_raw_reported", "{n} unreported same-raw document(s)", bad)


__all__ = ["CORPUS_KEYS"]
