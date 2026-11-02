"""Materialising format upcasts of stored files (D208, amended by D295), and URL re-keys.

Every reader reads a family file at its family's current format version on its own
(:func:`stack.store.parquet.read_family`, the version table :mod:`stack.schemas.upcasters`; a chain
may mix versions), so **a migration is optional**: run one to drop the per-read upcast cost of a
large older version, or to hand a duckdb consumer current files (a raw read sees a file's own
version). The older files stay (there is no data gc, D356). The additive conform
(:func:`upcast_table`, :func:`schema_delta`, :class:`UpcastError`) lives in
:mod:`stack.schemas.upcasters` and is re-exported here.

* **Chains** (``documents``; any chained family): :func:`upcast_chain` resolves the chain, reads
  its view (each file at the current version) and writes it as a ``fold.compact`` full file on top
  of the head: ``fold_config = compact``, ``base_file_id`` = the superseded head, a run record of
  stage ``fold.compact`` whose ``counts`` carry ``upcast_from.<schema_digest>`` (files of the
  version under that digest), ``rows`` and ``fields_added``. V12/X22/X17/X24/X15 compare it with
  the base view, read the same way (:class:`stack.checks.chain.StoreView`).
  **A ``sources`` compaction loses no observation** (D264): where the view's row for a source is a
  miss (``state = 'missing'``) and the chain holds an earlier non-miss row for it, the full file
  carries that non-miss row (the last in chain order, :func:`observed_under_misses`) and a second
  recorded run, stage and writer ``migrate.restate``, writes the miss rows as a part file on top
  (rows stamped with its run id, everything else kept). The chain's view is then the view before
  the compaction and its resolved version still holds the observation that gather speaks from
  (D262). V12 allows exactly this difference and requires the restate part. If the restate run
  fails, :func:`upcast_chain` returns its failed record; until a collector misses the source
  again the view shows the observed row (a miss is lost, never an observation).
* **Chains of one** (``<wf>``, ``<wf>_evidence``): :func:`upcast_file` writes a new full file of
  the family from one old file (new run id, stage and writer ``migrate.upcast``, an ``ok`` run
  record; header ``inputs = {<family>: <old file id>}``, ``plan``, ``config_digest``,
  ``scope_complete`` and ``basis_kind`` copied from the old file); the old file stays.
  ``corpus`` files are per-build outputs and are not migrated (refused; they read upcast).

``stack migrate upcast`` (:mod:`stack.cli.migrate`) is the verb; :func:`plan_upcast` is its dry
run (per file its version ``v<k> → v<N>`` and the step kinds, the digests, the added field paths of
the additive steps, row counts; nothing written).

**Re-key** (track U, round 4; url-normalisation.md §3): ``stack migrate rekey`` re-types the identity of every
``sources`` row under the current URL and key rules (:mod:`stack.urls`,
:mod:`stack.scholarly_records`). Per chain the view's row per ``source_id`` (the latest) gets new
``identity.keys`` and ``identity.canonical_url`` (:func:`rekey_identity`), plus the keys of
:mod:`stack.identity`'s key adders from the row's record payload (added, never removed); everything else of the
row (``source_id``, ``external_id``, ``observed_at``, ``record_digest``, metadata, in-band entries)
is kept. Rows that change are written as **one part file per chain** on its head, one recorded
run of stage and writer ``migrate.rekey`` per chain (a file id is ``<run_id>.<family>``; header
``inputs = {sources: <head>}``, ``config_digest`` = ``H`` of the sources of :mod:`stack.urls` and
:mod:`stack.scholarly_records`, rows stamped with the run id). A
restated row keeps its ``observed_at``, so gather's observation stats do not count it
(``workflows/gather/normalise.py``). :func:`plan_rekey` is the dry run; :func:`rekey_report`
predicts the documents a ``gather`` over the re-keyed sources makes with gather's own pure
functions (no store copy) and lists the merges and splits against the ``documents`` head.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from stack import identity, urls
from stack import scholarly_records as sr
from stack.collect.inband import json_record
from stack.checks.digests import H
from stack.fold import WRITER as FOLD_WRITER
from stack.fold import fold_config_digest
from stack.fold import write as fold_write
from stack.fold.configs import fold_config
from stack.runs import FoldInfo, RunRecord
from stack.schemas.families import FAMILIES, FamilySpec, family
from stack.schemas.upcasters import SchemaDelta, UpcastError, schema_delta, upcast_table
from stack.stage_runner import run_recorded
from stack.store.chain import (
    CHAIN_OF_ONE_KINDS,
    Chain,
    EmptyChainError,
    HeaderFields,
    HeadMovedError,
    Published,
    head_of,
    publish,
    read_view,
    resolve,
    stage_file,
    write_file,
)
from stack.store.index import store_index
from stack.store.layout import (
    DATA,
    NONE,
    family_of_dir,
    make_data_file,
    parse_data_key,
    parse_dir_ref,
    tie_guard,
)
from stack.store.parquet import FileFormat, file_format, read_family, read_header

#: Stage and writer of a single-file upcast run.
STAGE = "migrate.upcast"
#: Stage and writer of the run that restates a ``sources`` compaction's miss rows (D264).
RESTATE_STAGE = "migrate.restate"


# ---------------------------------------------------------------- what to migrate
@dataclass(frozen=True)
class FilePlan:
    """One file of a migration target: its id, digest, format version and the step kinds from
    there to the current version (:attr:`stack.schemas.upcasters.Version.kind`)."""

    file_id: str
    digest: str
    version: int
    steps: tuple[str, ...]
    is_current: bool


@dataclass(frozen=True)
class UpcastPlan:
    """One migration target: a chain (``chain_id``, its ``head``) or one file (``file_id``), its
    ``files`` (:class:`FilePlan`, chain order), the family's current version ``new_version`` and
    digest ``new``, the additive delta (the union over the files whose steps are all additive)
    and the row count."""

    family: str
    files: tuple[FilePlan, ...]
    new_version: int
    new: str
    delta: SchemaDelta
    rows: int
    chain_id: str | None = None
    head: str | None = None
    file_id: str | None = None
    paths: tuple[Path, ...] = field(default=(), compare=False)

    @property
    def old(self) -> dict[str, list[str]]:
        """``schema_digest`` → the file ids under it (D208's run counts)."""
        out: dict[str, list[str]] = {}
        for f in self.files:
            out.setdefault(f.digest, []).append(f.file_id)
        return out

    @property
    def current(self) -> bool:
        """Nothing to do: every file is already at the current format."""
        return all(f.is_current for f in self.files)

    def as_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "family": self.family, "current_version": self.new_version, "new_schema_digest": self.new,
            "files": [{"file_id": f.file_id, "from": f"v{f.version}", "to": f"v{self.new_version}",
                       "steps": list(f.steps)} for f in self.files],
            "old_schema_digests": self.old, "added": list(self.delta.added), "relaxed": list(self.delta.relaxed),
            "rows": self.rows, "current": self.current}
        if self.chain_id is not None:
            out.update(chain_id=self.chain_id, head=self.head)
        else:
            out["file_id"] = self.file_id
        return out


def _spec(family_name: str) -> FamilySpec:
    try:
        return family(family_name)
    except KeyError as exc:
        raise UpcastError([str(exc.args[0])], family_name) from None


def _file_plan(fid: str, fmt: FileFormat) -> FilePlan:
    return FilePlan(fid, fmt.digest, fmt.version, tuple(v.kind for v in fmt.steps), fmt.is_current)


def _delta_over(paths: list[Path], fmts: list[FileFormat], spec: FamilySpec) -> SchemaDelta:
    """The union of the additive deltas to the current schema of the files whose steps are all
    additive (a function step's change is named by its kind, not a delta)."""
    added: dict[str, None] = {}
    relaxed: dict[str, None] = {}
    for p, fmt in zip(paths, fmts, strict=True):
        if fmt.is_current or not fmt.additive:
            continue
        d = schema_delta(pq.read_schema(p).remove_metadata(), spec.schema)
        added.update(dict.fromkeys(d.added))
        relaxed.update(dict.fromkeys(d.relaxed))
    return SchemaDelta(tuple(added), tuple(relaxed))


def chain_ids(store_root: str | Path, family_name: str) -> list[str]:
    """The series directories (``data/<family dir>/<series>``, the chains, D375) of
    ``family_name`` with published files, from the run records (D345)."""
    return [d for d in store_index(store_root).series_dirs() if family_of_dir(d.split("/")[1]) == family_name]


def _dest(chain: Chain, run_id: str, kind: str):
    """The file a migration run writes on ``chain``'s directory head (D372, D375: after the
    directory's last file, which must be the resolved head)."""
    if not chain.is_dir_head:
        raise HeadMovedError(f"{chain.dir}: resolved {chain.head_name} is not the directory's last file {chain.last}")
    stamp = tie_guard(dt.datetime.now(dt.UTC), chain.last[:16] or None)
    return make_data_file(chain.chain_id, chain.series, stamp, run_id, kind)


def plan_chain(store_root: str | Path, chain_id: str) -> UpcastPlan:
    """The dry run of :func:`upcast_chain` for ``chain_id`` (reads the view for the row count).
    A version file that does not read refuses here (:class:`stack.store.chain.FileFormatError`)."""
    chain = resolve(Path(store_root), chain_id)
    spec = chain.spec
    fmts = [file_format(cf.path, cf.header) for cf in chain.files]
    files = tuple(_file_plan(cf.file_id, f) for cf, f in zip(chain.files, fmts, strict=True))
    delta = _delta_over(list(chain.paths), fmts, spec)
    rows = read_view(chain).num_rows
    return UpcastPlan(spec.name, files, spec.version, spec.schema_digest, delta, rows, chain_id=chain.dir or chain.chain_id,
                      head=chain.head, paths=chain.paths)


def _data_file(store_root: Path, fid: str) -> Path:
    """The file ``fid`` from the run record naming it (downloaded when absent; D345)."""
    idx = store_index(store_root)
    hit = idx.output(fid)
    if hit is None:
        raise UpcastError([f"no ok run record names file id {fid!r}"], fid)
    return idx.fetch_output(hit[2])


def _pair_files(root: Path, family_name: str, fid: str) -> list[tuple[str, str]]:
    """``(family, file id)`` of ``fid`` and, for a pair family, its sibling: the same run's file
    of the other family when an ``ok`` record names it (an unpaired file is upcast alone)."""
    sib = pair_sibling(family_name)
    sib_fid = f"{fid.rsplit('.', 1)[0]}.{sib}" if sib is not None else None
    if sib is None or sib_fid is None or store_index(root).output(sib_fid) is None:
        return [(family_name, fid)]
    return [(family_name, fid), (sib, sib_fid)]


def _refuse_not_head(root: Path, todo: list[tuple[str, str]]) -> None:
    """Each file must be its directory's current last published file and not excluded: the new
    full file goes after that head, so an older or orphaned file would publish its old rows as
    the series' newest (S25-13). There is no ``--out`` into another series yet."""
    idx = store_index(root)
    ex = idx.excluded()
    for _fam, f in todo:
        hit = idx.output(f)
        if hit is None:
            raise UpcastError([f"no ok run record names file id {f!r}"], f)
        if f in ex:
            raise UpcastError([f"{f} is excluded (chain orphan); only a directory's current head is upcast"], f)
        df = parse_data_key(str(hit[2]["path"]))
        if df.series is not None and (last := head_of(root, df.dir)) != df.name:
            msg = (f"{f} is not the last published file of {df.dir} ({last or 'empty'}); "
                   "only a directory's current head is upcast")
            raise UpcastError([msg], f)


def plan_file(store_root: str | Path, family_name: str, fid: str) -> UpcastPlan:
    """The dry run of :func:`upcast_file`: ``fid`` must be a ``data/`` file of ``family_name``,
    a chain of one other than ``corpus``, whose header digest is its own schema's, and (with its
    pair sibling) its directory's current non-excluded head (:func:`_refuse_not_head`)."""
    root = Path(store_root)
    spec = _spec(family_name)
    if spec.kind not in CHAIN_OF_ONE_KINDS:
        raise UpcastError([f"{family_name} is a chained family: migrate the chain (no --file)"], fid)
    if spec.kind == "corpus":
        raise UpcastError(["corpus files are per-build outputs and are not migrated; run a new build"], fid)
    path = _data_file(root, fid)
    h = read_header(path)
    if h.get("family") != family_name:
        raise UpcastError([f"{path.name} is a {h.get('family')!r} file, not {family_name!r}"], fid)
    _refuse_not_head(root, _pair_files(root, family_name, fid))
    fmt = file_format(path, h)  # a file that does not read: SchemaMismatch
    delta = _delta_over([path], [fmt], spec)
    return UpcastPlan(family_name, (_file_plan(fid, fmt),), spec.version, spec.schema_digest, delta,
                      int(h["row_count"]), file_id=fid, paths=(path,))


def plan_upcast(store_root: str | Path, family_name: str, fid: str | None = None,
                dirs: Sequence[str] = ()) -> list[UpcastPlan]:
    """Every migration target of ``family_name``: the one file ``fid`` (chains of one), or the
    chains of a chained family (``fid`` refused): every one, or only the series directories
    ``dirs`` (``data/<family dir>/<series>/``, ``--in``; S25-38), in the order given. A directory
    of another family, or one with no published file, is refused, as is ``dirs`` for a chain of
    one. Raises :class:`UpcastError` (the request), :class:`stack.store.chain.ChainError` or
    :class:`stack.store.parquet.SchemaMismatch` (a file that does not read)."""
    spec = _spec(family_name)
    if spec.kind in CHAIN_OF_ONE_KINDS:
        if fid is None:
            raise UpcastError([f"{family_name} files are chains of one: name the file (--file)"], family_name)
        if dirs:
            raise UpcastError([f"{family_name} files are chains of one: --file names the file (no --in)"],
                              family_name)
        return [plan_file(store_root, family_name, fid)]
    if fid is not None:
        msg = f"{family_name} is a chained family: its chains are migrated whole (no --file)"
        raise UpcastError([msg], family_name)
    ids = chain_ids(store_root, family_name)
    if not ids:
        raise UpcastError([f"no {family_name} chain under data/"], family_name)
    if dirs:
        picked: list[str] = []
        for ref in dirs:
            try:
                family_dir, series = parse_dir_ref(ref)
            except ValueError as exc:
                raise UpcastError([f"--in {ref}: {exc}"], family_name) from None
            d = f"{DATA}/{family_dir}/{series}"
            if family_of_dir(family_dir) != family_name:
                raise UpcastError([f"--in {ref}: not a {family_name} directory"], family_name)
            if d not in ids:
                raise UpcastError([(f"--in {ref}: no published {family_name} file there (chains: "
                                    f"{', '.join(ids)})")], family_name)
            if d not in picked:
                picked.append(d)
        ids = picked
    return [plan_chain(store_root, c) for c in ids]


# ---------------------------------------------------------------- write
def _counts(plan: UpcastPlan) -> dict[str, int]:
    return {"rows": plan.rows, "fields_added": len(plan.delta.added),
            **{f"upcast_from.{d}": len(fids) for d, fids in sorted(plan.old.items())}}


def _refuse_current(plan: UpcastPlan) -> None:
    if plan.current:
        what = f"chain {plan.chain_id!r}" if plan.chain_id else plan.file_id
        raise UpcastError([(f"already at the current format {plan.family} v{plan.new_version} "
                            f"({plan.new[:12]}…); nothing to upcast")], str(what))


def observed_under_misses(chain: Chain, view: pa.Table) -> tuple[pa.Table, pa.Table]:
    """For a ``sources`` view of ``chain``: ``(the view with each miss row replaced by its
    source's last non-miss row in the chain's files (chain order, each file at the current
    format), the miss
    rows so replaced)``. A source with no non-miss row keeps its miss. Nothing to replace: the
    view itself (the same object) and an empty table."""
    misses = view.filter(pc.equal(view["state"], "missing"))
    if not len(misses):
        return view, misses
    wanted = misses["source_id"].combine_chunks()
    last: dict[str, Row] = {}
    for cf in chain.files:
        tbl = read_family(cf.path)[0]
        keep = pc.and_(pc.is_in(tbl["source_id"], value_set=wanted), pc.not_equal(tbl["state"], "missing"))
        for r in tbl.filter(keep).to_pylist():
            last[r["source_id"]] = r
    if not last:
        return view, misses.slice(0, 0)
    rows, replaced = [], []
    for r in view.to_pylist():
        if r["state"] == "missing" and r["source_id"] in last:
            rows.append(last[r["source_id"]])
            replaced.append(r)
        else:
            rows.append(r)
    return pa.Table.from_pylist(rows, schema=view.schema), pa.Table.from_pylist(replaced, schema=view.schema)


def upcast_chain(store_root: str | Path, chain_id: str) -> RunRecord:
    """Migrate one chain: its view (at the current format) as a ``fold.compact`` full file on its
    head, as one recorded run (module docstring). Refuses (:class:`UpcastError`) a chain already
    current, (:class:`stack.store.chain.FileFormatError`) one with a file that does not read,
    before any run exists; a moved head ends the run ``failed[head_moved]``. A ``sources`` chain
    whose view holds misses over earlier observations gets the ``migrate.restate`` run after it
    (D264); its record is returned instead when it fails."""
    root = Path(store_root)
    plan = plan_chain(root, chain_id)
    _refuse_current(plan)
    spec = dataclasses.replace(fold_config("compact"), output_family=plan.family)
    fold: FoldInfo = {"config": "compact", "filter": NONE, "joined": {}, "annotations_folded": [],
                      "release_config": NONE}
    head_plan = read_header(plan.paths[-1]).get("plan", NONE)
    restate: list[pa.Table] = []

    def body(rec: RunRecord, on_published: Callable[[list[Published]], None]) -> None:
        chain = resolve(root, chain_id)
        if chain.head != plan.head:
            raise HeadMovedError(f"chain {chain_id!r}: head moved from {plan.head} to {chain.head}")
        table = read_view(chain)
        rec.counts = _counts(plan)
        if plan.family == "sources":
            table, misses = observed_under_misses(chain, table)
            restate.append(misses)
            rec.counts["observed_under_misses"] = len(misses)
        fold_write(root, table, spec, fold, run_id=rec.run_id, plan_name=head_plan,
                   inputs={plan.family: chain.head}, dest=_dest(chain, rec.run_id, "full"), base=chain.last,
                   after=chain.last, header_extra={"code_digest": rec.code_digest}, on_published=on_published)

    rec = run_recorded(root, stage=spec.stage, writer=FOLD_WRITER, plan_name=head_plan,
                       inputs={plan.family: chain_id}, config_digest=fold_config_digest(fold), body=body)
    if rec.status != "ok" or not restate or not len(restate[0]):
        return rec
    compaction = next(iter(rec.outputs.values()))["file_id"]
    again = _restate_misses(root, chain_id, restate[0], compaction, head_plan)
    return rec if again.status == "ok" else again


def _restate_misses(root: Path, chain_id: str, misses: pa.Table, expected: str, head_plan: str) -> RunRecord:
    """The ``migrate.restate`` run (D264): ``misses`` as one part file on the compaction
    ``expected``, rows stamped with the run id."""
    cfg = H({"restate": "misses", "compaction": expected})

    def body(rec: RunRecord, on_published: Callable[[list[Published]], None]) -> None:
        chain = resolve(root, chain_id)
        if chain.head != expected:
            raise HeadMovedError(f"chain {chain_id!r}: head moved from {expected} to {chain.head}")
        rows = [{**r, "run_id": rec.run_id} for r in misses.to_pylist()]
        fields = HeaderFields(stage=RESTATE_STAGE, run_id=rec.run_id, config_digest=cfg,
                              code_digest=rec.code_digest, writer=RESTATE_STAGE, plan=head_plan,
                              inputs={"sources": expected})
        rec.counts = {"rows": len(rows)}
        write_file(root, _dest(chain, rec.run_id, "part"), pa.Table.from_pylist(rows, schema=misses.schema),
                   fields, base=chain.last, after=chain.last, on_published=on_published)

    return run_recorded(root, stage=RESTATE_STAGE, writer=RESTATE_STAGE, plan_name=head_plan,
                        inputs={"sources": chain_id}, config_digest=cfg, body=body)


def pair_sibling(family_name: str) -> str | None:
    """The other family of an answers/evidence pair (``<wf>`` + ``<wf>_evidence``), ``None`` for
    a family outside a pair (D380)."""
    if family_name.endswith("_evidence"):
        wf = family_name.removesuffix("_evidence")
        return wf if wf in FAMILIES and family(wf).kind == "wf" else None
    return f"{family_name}_evidence" if family(family_name).kind == "wf" else None


def upcast_file(store_root: str | Path, family_name: str, fid: str) -> RunRecord:
    """Migrate one chain-of-one file: a new full file of ``family_name`` with the upcast rows,
    as one recorded run of stage ``migrate.upcast`` (module docstring). The old file stays. A file
    of an answers/evidence pair is upcast together with its sibling (the same old run's file of
    the other family) in the same run, so the pair stays from one run (conductor
    ruling 2026-10-14, D380); both publish atomically under the one record. Each file must be its
    directory's current non-excluded head, checked before the run and again in it (D421)."""
    root = Path(store_root)
    plan = plan_file(root, family_name, fid)
    _refuse_current(plan)
    (path,) = plan.paths
    old = read_header(path)
    todo = _pair_files(root, family_name, fid)
    inputs = dict(todo)
    fields = HeaderFields(stage=STAGE, run_id="", config_digest=old["config_digest"], code_digest="",
                          writer=STAGE, plan=old.get("plan", NONE), inputs=inputs,
                          scope_complete=old.get("scope_complete") == "true",
                          basis_kind=old.get("basis_kind", NONE))

    def body(rec: RunRecord, on_published: Callable[[list[Published]], None]) -> None:
        rec.counts = _counts(plan)
        at = dt.datetime.now(dt.UTC)
        staged = []
        for fam, f in todo:
            hit = store_index(root).output(f)
            if hit is None:  # pragma: no cover - read a moment ago
                raise UpcastError([f"no ok run record names file id {f!r}"], f)
            src = _data_file(root, f)
            old_file = parse_data_key(str(hit[2]["path"]))  # the new full goes into the old file's directory
            last = head_of(root, old_file.dir) if old_file.series is not None else ""
            if old_file.series is not None and last != old_file.name:
                raise HeadMovedError(f"{old_file.dir}: {f} is no longer the last file ({last or 'empty'})")
            dest = make_data_file(old_file.family_dir, old_file.series, tie_guard(at, last[:16] or None),
                                  rec.run_id, "full")
            h = read_header(src)  # each file keeps its own header fields (basis_kind differs within a pair)
            own = dataclasses.replace(fields, run_id=rec.run_id, code_digest=rec.code_digest,
                                      config_digest=h["config_digest"], basis_kind=h.get("basis_kind", NONE),
                                      scope_complete=h.get("scope_complete") == "true")
            staged.append(stage_file(root, dest, read_family(src)[0], own, after=last))
        publish(staged, on_published)

    return run_recorded(root, stage=STAGE, writer=STAGE, plan_name=fields.plan,
                        inputs=inputs, config_digest=fields.config_digest, body=body)


# ---------------------------------------------------------------- re-key (url-normalisation.md §3)
#: Stage and writer of the re-key run.
REKEY_STAGE = "migrate.rekey"
Row = dict[str, Any]
#: Record payload fields that may hold a ``url``-group row's original link (first wins).
RECORD_URL_FIELDS: tuple[str, ...] = ("url", "href", "link", "pageUrl", "canonical_url")


class RekeyError(ValueError):
    """The store has no ``sources`` chain, or a chain cannot be read."""


def _pairs(keys: Any) -> list[tuple[str, str]]:
    return [(k["kind"], k["value"]) for k in keys or []]


def _renormalise(pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    """Each key in its kind's current normal form (bridged); a value that no longer normalises is
    kept as it was (counted), never dropped."""
    out: list[tuple[str, str]] = []
    kept = 0
    for kind, value in pairs:
        typed = _pairs(sr.keys_for([(kind, value)]))
        if not typed:
            typed, kept = [(kind, value)], kept + 1
        out += [t for t in typed if t not in out]
    rank = {k: i for i, k in enumerate(sr.KEY_KINDS)}
    return sorted(out, key=lambda kv: (rank.get(kv[0], len(rank)), kv[1])), kept


@dataclass(frozen=True)
class Rekeyed:
    """One ``sources`` row's identity before and after (:func:`rekey_identity`)."""

    chain_id: str
    source_id: str
    channel: str
    group: str
    state: str
    old_keys: tuple[tuple[str, str], ...]
    new_keys: tuple[tuple[str, str], ...]
    old_url: str | None
    new_url: str | None
    url_from: str          # record | canonical_url | external_id | none (url group); keys (others)
    collapsed: bool        # a url-group row whose only URL is v3's collapsed key form
    unnormalisable: int

    @property
    def changed(self) -> bool:
        return set(self.old_keys) != set(self.new_keys) or self.old_url != self.new_url


def rekey_identity(group: str, ident: Mapping[str, Any], *, record_url: str | None = None,
                   chain_id: str = "", source_id: str = "", state: str = "observed",
                   record: Mapping[str, Any] | None = None) -> Rekeyed:
    """The identity of one ``sources`` row under the current rules (module docstring).

    ``url`` group: the ``url`` key from the best original URL (``record_url`` from the record
    payload, else ``canonical_url``, else an ``external_id`` that is a URL; else the stored ``url``
    keys re-normalised); other kinds (``related``, ``relay``) re-normalised. Other groups: every
    key re-normalised. Then the key adders' keys (:func:`stack.identity.added_keys` over ``record``,
    the row's record payload). ``canonical_url``: the fetch form (:func:`stack.urls.fetch_url`) of the
    record URL or the stored one; a value that is not a web URL stays."""
    old = _pairs(ident.get("keys"))
    canon = ident.get("canonical_url")
    ext = ident.get("external_id") or ""
    if group == "url":
        if record_url and urls.url_key(record_url):
            src, origin = record_url, "record"
        elif canon and urls.url_key(canon):
            src, origin = canon, "canonical_url"
        elif urls.url_key(ext):
            src, origin = ext, "external_id"
        else:
            src, origin = None, "none"
        others = [kv for kv in old if kv[0] != "url"]
        base = [("url", src)] if src is not None else [kv for kv in old if kv[0] == "url"]
        new, bad = _renormalise(base + others)
        # v3's rule dropped the query and kept the rest: a stored URL without a query or fragment is
        # (or may be) that collapsed form, and the original's identity parameters are unknown
        collapsed = origin != "record" and src is not None and "?" not in src and "#" not in src
    else:
        new, bad = _renormalise(old)
        origin, collapsed = "keys", False
    extra = identity.added_keys(str(ident.get("channel")), group, record, new)
    if extra:
        new, _ = _renormalise(new + extra)
    url_src = record_url if (group == "url" and record_url) else canon
    new_url = (urls.fetch_url(url_src) or canon) if url_src else canon
    return Rekeyed(chain_id, source_id, str(ident.get("channel")), group, state, tuple(old), tuple(new),
                   canon, new_url, origin, collapsed, bad)


def _record_url(store_root: Path, row: Row) -> str | None:
    """The original link in the row's provider record (its ``provider_record`` in-band entry as JSON,
    D524), else ``None`` (no record, no blob, not JSON, no URL field)."""
    from stack.store.blobs import open_blobs

    data = json_record(open_blobs(store_root), row)
    if not isinstance(data, Mapping):
        return None
    for f in RECORD_URL_FIELDS:
        v = data.get(f)
        if isinstance(v, str) and urls.url_key(v):
            return v
    return None


def _record_payload(blobs: Any, row: Row) -> Mapping[str, Any] | None:
    """The row's provider record (its ``provider_record`` in-band entry as JSON, D524) for the key adders,
    else ``None`` (no record, no blob, not a JSON object)."""
    data = json_record(blobs, row)
    return data if isinstance(data, Mapping) else None


def _adder_channel(channel: str, group: str) -> bool:
    """Whether some key adder reads records of ``channel`` (so its record payload is worth reading)."""
    return any(group in a.groups and (a.channels is None or channel in a.channels) for a in identity.KEY_ADDERS)


@dataclass
class RekeyPlan:
    """The dry run: per chain its resolved :class:`Chain`, view and re-keyed rows."""

    store_root: Path
    chains: dict[str, Chain]
    views: dict[str, pa.Table]
    rows: list[Rekeyed]

    def changed(self) -> dict[str, list[Rekeyed]]:
        out: dict[str, list[Rekeyed]] = defaultdict(list)
        for r in self.rows:
            if r.changed:
                out[r.chain_id].append(r)
        return dict(out)

    def new_views(self) -> dict[str, pa.Table]:
        """The chain views as they read after :func:`apply_rekey` (rows re-keyed in place)."""
        out: dict[str, pa.Table] = {}
        by = {(r.chain_id, r.source_id): r for r in self.rows if r.changed}
        for cid, view in self.views.items():
            rows = view.to_pylist()
            for row in rows:
                r = by.get((cid, row["source_id"]))
                if r is not None:
                    row["identity"] = {**row["identity"], "canonical_url": r.new_url,
                                       "keys": [{"kind": k, "value": v} for k, v in r.new_keys]}
            out[cid] = pa.Table.from_pylist(rows, schema=view.schema)
        return out


def plan_rekey(store_root: str | Path) -> RekeyPlan:
    """Every ``sources`` chain's view and each row's re-keyed identity; nothing written."""
    from workflows.gather.normalise import channel_index

    root = Path(store_root)
    ids = chain_ids(root, "sources")
    if not ids:
        raise RekeyError(f"{root}: no sources chain under data/")
    from stack.store.blobs import open_blobs

    groups = channel_index()
    chains = {c: resolve(root, c) for c in ids}
    views = {c: read_view(ch) for c, ch in chains.items()}
    blobs = open_blobs(root)
    rows: list[Rekeyed] = []
    for cid, view in views.items():
        for row in view.to_pylist():
            ident = row["identity"] or {}
            channel = str(ident.get("channel"))
            group = groups.group_of(channel)
            rec_url = _record_url(root, row) if group == "url" else None
            record = _record_payload(blobs, row) if _adder_channel(channel, group) else None
            rows.append(rekey_identity(group, ident, record_url=rec_url, chain_id=cid,
                                       source_id=row["source_id"], state=row["state"], record=record))
    return RekeyPlan(root, chains, views, rows)


def rekey_config_digest() -> str:
    """``H`` of :mod:`stack.urls`'s, :mod:`stack.scholarly_records`'s and :mod:`stack.identity`'s sources:
    the rules a re-key applied."""
    return H({m.__name__: hashlib.sha256(Path(str(m.__file__)).read_bytes()).hexdigest()
              for m in (urls, sr, identity)})


def apply_rekey(plan: RekeyPlan) -> list[RunRecord]:
    """Write the changed rows of each chain as one part file on its head, one recorded run per chain
    (a file id is ``<run_id>.<family>``, so one run writes one ``sources`` file; module docstring).
    A chain whose head moved since :func:`plan_rekey` ends its run ``failed[head_moved]``; the
    runs of the other chains are independent."""
    root = plan.store_root
    cfg = rekey_config_digest()
    new_views = plan.new_views()
    out: list[RunRecord] = []
    for cid, rows in sorted(plan.changed().items()):
        expected = plan.chains[cid].head
        head_plan = read_header(plan.chains[cid].paths[-1]).get("plan", NONE)
        want = {r.source_id for r in rows}

        def body(rec: RunRecord, on_published: Callable[[list[Published]], None], *, cid: str = cid,
                 expected: str = expected, want: set[str] = want, head_plan: str = head_plan) -> None:
            chain = resolve(root, cid)
            if chain.head != expected:
                raise HeadMovedError(f"chain {cid!r}: head moved from {expected} to {chain.head}")
            rows_ = [{**row, "run_id": rec.run_id} for row in new_views[cid].to_pylist() if row["source_id"] in want]
            table = pa.Table.from_pylist(rows_, schema=plan.views[cid].schema)
            fields = HeaderFields(stage=REKEY_STAGE, run_id=rec.run_id, config_digest=cfg,
                                  code_digest=rec.code_digest, writer=REKEY_STAGE, plan=head_plan,
                                  inputs={"sources": chain.head})
            rec.counts = {"rows": len(rows_)}
            write_file(root, _dest(chain, rec.run_id, "part"), table, fields, base=chain.last, after=chain.last,
                       on_published=on_published)

        out.append(run_recorded(root, stage=REKEY_STAGE, writer=REKEY_STAGE, plan_name=head_plan,
                                inputs={"sources": cid}, config_digest=cfg, body=body))
    return out


# ---------------------------------------------------------------- re-key report
def predict_documents(store_root: str | Path, views: Mapping[str, pa.Table],
                      chains: Mapping[str, Chain], series: str | None = None) -> dict[str, Any]:
    """What a ``gather`` over ``views`` would make, with gather's pure functions (union,
    anchors, continuity against the ``documents`` head): ``docs`` (doc_id → member ids),
    ``obs`` (source_id → observation), ``edges``, ``merged_into`` and ``head``."""
    from stack.overrides import CHAIN_ID as OVERRIDES_CHAIN
    from stack.overrides import rows_of
    from workflows.gather import overrides as identity_overrides
    from workflows.gather.anchor import anchor_of, continuity
    from workflows.gather.normalise import channel_index, observations
    from workflows.gather.union import components, edges

    root = Path(store_root)
    head: dict[str, Row] = {}
    if series is None:  # the sources' own series (one, else name it)
        got = {c.series for c in chains.values()}
        if len(got) != 1 or None in got:
            raise ValueError(f"predict_documents: the sources span series {sorted(map(str, got))}; pass series")
        series = got.pop()
    try:
        doc_chain = resolve(root, f"data/documents/{series}/")
    except EmptyChainError:
        doc_chain = None
    if doc_chain is not None:
        head = {r["doc_id"]: r for r in read_view(doc_chain).to_pylist()}
    obs, _ = observations(views, chains, channel_index(), head)  # stats carried from the head, as gather
    first = {s: o.first_observed_at for s, o in obs.items()}

    def order(s: str) -> tuple[dt.datetime, str]:
        return (first[s], s)

    try:  # the identity overrides at the overrides head, as the next gather would pin them
        ov_rows = rows_of(read_view(resolve(root, f"data/{OVERRIDES_CHAIN}/{series}/")))
    except EmptyChainError:
        ov_rows = []
    ops = identity_overrides.from_rows(ov_rows)
    es = edges(obs, ops, order)
    docs = {anchor_of(c, order): sorted(c) for c in components(list(obs), es, order)}
    cont = continuity(docs, head)
    return {"docs": docs, "obs": obs, "edges": es, "merged_into": cont.merged_into, "head": head}


def _title(row: Row | None, obs: Any = None) -> str:
    if row is not None:
        return str((row.get("metadata") or {}).get("title") or "")
    return str(((obs.row.get("metadata") or {}).get("title")) or "") if obs is not None else ""


def rekey_report(plan: RekeyPlan) -> dict[str, Any]:
    """The dry run's report: rows re-keyed per channel and key-kind change; the documents a gather
    would make before (the current sources, a baseline) and after the re-key against the
    ``documents`` head; merges (current live doc ids now one document, with titles and the keys
    that join them), splits (one current document now several, with the old keys that no longer
    join); url-group rows whose original URL could not be recovered."""
    before = predict_documents(plan.store_root, plan.views, plan.chains)
    after = predict_documents(plan.store_root, plan.new_views(), plan.chains)
    head = after["head"]
    live_head = {d: sorted(m["source_id"] for m in r["identity"]["members"])
                 for d, r in head.items() if r["state"] != "retired"}
    owner_head = {s: d for d, ms in live_head.items() for s in ms}

    def docs_vs_head(pred: dict[str, Any]) -> dict[str, Any]:
        owner = {s: d for d, ms in pred["docs"].items() for s in ms}
        merges, splits = [], []
        for d, ms in sorted(pred["docs"].items()):
            olds = sorted({owner_head[s] for s in ms if s in owner_head})
            if len(olds) > 1:
                keys = sorted({f"{e.key[0]}={e.key[1]}" for e in pred["edges"]
                               if e.key is not None and owner_head.get(e.a) != owner_head.get(e.b)
                               and owner.get(e.a) == d and owner.get(e.b) == d})
                merges.append({"doc_id": d, "from": olds,
                               "titles": {o: _title(head[o]) for o in olds}, "joined_by": keys})
        for d, ms in sorted(live_head.items()):
            parts = sorted({owner[s] for s in ms if s in owner})
            if len(parts) > 1:
                hk = sorted({f"{k['kind']}={k['value']}" for k in head[d]["identity"]["keys"]})
                new_keys = {p: sorted({f"{k}={v}" for s in pred["docs"][p] for k, v in pred["obs"][s].keys})
                            for p in parts}
                splits.append({"doc_id": d, "title": _title(head[d]), "into": parts,
                               "head_keys": hk, "part_keys": new_keys})
        return {"documents": len(pred["docs"]), "merges": merges, "splits": splits}

    changed = [r for r in plan.rows if r.changed]
    per_channel: Counter[str] = Counter(r.channel for r in changed)
    kind_changes: Counter[str] = Counter()
    for r in changed:
        o, n = set(r.old_keys), set(r.new_keys)
        for k in sorted({k for k, _ in o | n}):
            ov = sorted(v for kk, v in o if kk == k)
            nv = sorted(v for kk, v in n if kk == k)
            if ov != nv:
                kind_changes[f"{r.channel}:{k}:" + ("added" if not ov else "dropped" if not nv else "changed")] += 1
        if r.old_url != r.new_url:
            kind_changes[f"{r.channel}:canonical_url:changed"] += 1
    examples = [{"chain": r.chain_id, "source_id": r.source_id, "channel": r.channel,
                 "old_keys": [f"{k}={v}" for k, v in r.old_keys], "new_keys": [f"{k}={v}" for k, v in r.new_keys],
                 "old_url": r.old_url, "new_url": r.new_url} for r in changed]
    collapsed = Counter(r.channel for r in plan.rows if r.group == "url" and r.collapsed)
    unrecovered = Counter(r.channel for r in plan.rows if r.group == "url" and r.url_from == "none")
    # of the collapsed rows, those whose host keeps identity parameters in the key form (a lost
    # query would matter there)
    risky = sorted(r.old_url or "" for r in plan.rows if r.group == "url" and r.collapsed
                   and _keeps_query(r.old_url or ""))
    return {
        "store": str(plan.store_root),
        "sources_rows": len(plan.rows), "rows_rekeyed": len(changed),
        "rows_rekeyed_per_channel": dict(sorted(per_channel.items())),
        "key_changes": dict(sorted(kind_changes.items())),
        "unnormalisable_keys_kept": sum(r.unnormalisable for r in plan.rows),
        "url_from": dict(Counter(r.url_from for r in plan.rows if r.group == "url")),
        "url_rows_collapsed_form": dict(sorted(collapsed.items())),
        "url_rows_collapsed_on_query_hosts": risky,
        "url_rows_without_url": dict(sorted(unrecovered.items())),
        "head_documents_live": len(live_head),
        "baseline": docs_vs_head(before),
        "rekeyed": docs_vs_head(after),
        "predicted_docs": {d: ms for d, ms in sorted(after["docs"].items())},
        "changed_rows": examples,
    }


def _keeps_query(u: str) -> bool:
    """Whether ``u``'s host keeps identity parameters in the key form (:data:`stack.urls.KEY_PARAMS`)."""
    import urllib.parse

    host = urls._key_host(urllib.parse.urlsplit(u).hostname or "")
    return urls._covered(host, urls.KEY_PARAMS) is not None


__all__ = ["REKEY_STAGE", "RESTATE_STAGE", "STAGE", "FilePlan", "RekeyError", "RekeyPlan", "Rekeyed",
           "SchemaDelta", "UpcastError", "UpcastPlan", "apply_rekey", "chain_ids", "observed_under_misses", "plan_chain", "plan_file",
           "plan_rekey", "plan_upcast", "predict_documents", "rekey_identity", "rekey_report", "schema_delta",
           "upcast_chain", "upcast_file", "upcast_table"]
