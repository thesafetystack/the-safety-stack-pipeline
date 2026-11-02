"""Run records (A.7, D16, D38, D55; T-STORE D344): the record shape, the record writers and
readers, and the X1-over-run-records basis (``files_without_ok_record``, ``records_without_file``).

```
builds/<head>.<plan>/runs/<head>.<stage>.json.zst    a build's run (zstd JSON)
runs/<head>.<stage>.json.zst                         a standalone run
builds/<head>.<plan>/build.json.zst                  a build record
```

``<head>`` = ``<utc>_<id>[_<comment>]`` with ``<utc>`` the job's ``started_at`` (D320,
:mod:`stack.store.layout`). A run's json is written at its start (``running``), rewritten by its
job's tick (:class:`stack.store.job.RunJob`, which also uploads it with the log and the marker),
and replaced once by the final record; a final record is never rewritten
(:func:`write_final_record` and :meth:`stack.store.job.Job.finish` refuse). A build record is
written the same way by :class:`stack.store.job.BuildJob`. Every write is atomic
(``.<name>.<pid>.<thread>.tmp`` beside the json, then ``os.replace``). Readers go through
:mod:`stack.store.index`, which also LISTs an S3 store; nothing globs ``runs/`` or ``builds/`` here.
Liveness is the index's (markers, :func:`stack.store.index.liveness`); there are no locks (D356).

``error_class`` on a ``failed`` run: §2.6's classes (``crashed`` in old records, ``code_bug`` for
an exception in a stage) plus the runner-only :data:`RUN_ERROR_CLASSES_EXTRA` (``head_moved``:
the publish was refused; ``environment``: the stage refused to run in this environment,
:class:`~stack.stage_runner.EnvironmentRefused`, R4-1; ``tick_failed``: the job's ticks failed
for 2.5 min and it cancelled itself, or its start upload failed, D354; ``s3_transient``: an S3
request failed transiently past the client's retries, S25-47; ``lock_held``,
``writer_running``, ``unrecorded_head``: no longer raised, kept so old records read).

**Forward compatibility** (R2-5). :meth:`RunRecord.from_json` keeps keys it does not know in
``extra`` (written back unchanged by :meth:`RunRecord.to_json`) and warns
(:class:`RunRecordWarning`), so a record written by newer code does not stop older readers; an
``error_class`` it does not know is kept as read, with the same warning (S25-37). Constructing a
record in code stays strict (an unknown class raises).

Times are aware UTC datetimes in memory and ISO 8601 strings in JSON. ``outputs`` paths are
relative to the store root (``data/<name>``); an output also carries its chain link
(``chain_id``, ``kind``, ``base``, ``after``; D322, D372), so chains resolve from records (D345).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import threading
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NotRequired, TypedDict

import zstandard

from stack.schemas.vocab import ALL_ERROR_CLASSES, RUN_STATUS
from stack.store.layout import (
    DATA,
    StoreLayout,
    build_name,
    make_head,
    run_stem,
)

#: ``inflight`` holds at most this many keys (D19); the rest are counted in
#: ``counts["inflight_dropped"]``.
INFLIGHT_CAP = 10_000
#: Run-level error classes beyond §2.6 (a refused publish, T1.3: ``head_moved``, ``name_order``
#: (D372); ``environment``: R4-1,
#: ``stack.stage_runner.EnvironmentRefused``; ``tick_failed``: D354; ``s3_transient``: S25-47; ``lock_held``,
#: ``unrecorded_head`` and ``writer_running`` only in old records).
RUN_ERROR_CLASSES_EXTRA: frozenset[str] = frozenset(
    {"head_moved", "name_order", "lock_held", "unrecorded_head", "environment", "writer_running", "tick_failed",
     "s3_transient"})
RUN_ERROR_CLASSES: frozenset[str] = ALL_ERROR_CLASSES | RUN_ERROR_CLASSES_EXTRA


class RunRecordWarning(UserWarning):
    """A run record carries keys this code does not know (R2-5): kept in ``extra``, not fatal; or
    an ``error_class`` it does not know (S25-37): kept as read, not fatal."""


class VersionInfo(TypedDict):
    """``version`` of a run record: the rows the run touched (D376; each output's kind, base and
    after are in ``outputs``)."""

    rows_touched: int


class FoldInfo(TypedDict, total=False):
    config: str
    filter: str
    joined: dict[str, str]
    annotations_folded: list[str]
    release_config: str


class OutputInfo(TypedDict):
    """One entry of ``outputs`` (keyed by family)."""

    file_id: str
    path: str  # relative to the store root
    file_digest: str
    row_count: int
    schema_digest: str
    family_version: NotRequired[int]  # D295; absent from records written before it
    # D322 (T-STORE): the file's chain link, so readers build chains from records alone; absent
    # from records written before it
    chain_id: NotRequired[str]  # D372: the family directory (documents, sources.af_graphql)
    # D372: the series ("" without one), kind (full | part), the base file's name
    # in the directory ("" for a full without a base), the directory's last file when the run
    # pinned its inputs ("" for an empty directory), the file's comment ("" without one)
    series: NotRequired[str]
    kind: NotRequired[str]
    base: NotRequired[str]
    after: NotRequired[str]
    comment: NotRequired[str]


class InputFile(TypedDict):
    """One entry of ``in_files``: a file the run read, pinned by path and digest (D376)."""

    path: str
    file_digest: str


_TIME_FIELDS = ("started_at", "heartbeat_at", "finished_at")


@dataclass
class RunRecord:
    """A.7's run record. Optional fields of A.7 (``error_class?``, ``filter?``, ...) are ``None``
    when absent."""

    run_id: str
    stage: str
    writer: str
    plan: str
    status: str  # RUN_STATUS
    started_at: dt.datetime
    heartbeat_at: dt.datetime
    code_digest: str
    config_digest: str
    error_class: str | None = None
    error: str | None = None
    finished_at: dt.datetime | None = None
    only: list[str] | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    prior: list[str] = field(default_factory=list)
    filter: str | None = None
    scope_complete: bool = False
    inflight: list[str] = field(default_factory=list)
    charged_from_dead_runs: list[str] | None = None
    substages: dict[str, dict[str, bool]] = field(default_factory=dict)
    refresh_pending: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    version: VersionInfo | None = None
    fold: FoldInfo | None = None
    outputs: dict[str, OutputInfo] = field(default_factory=dict)
    #: D322 (T-STORE): the job uploads to S3 (fixed at start, inherited by a build's runs)
    upload: bool = False
    #: D322: the operator's ``--comment`` (``[A-Za-z0-9_-]+``), part of the job's head
    comment: str | None = None
    #: D322: file ids a ``chain orphan`` record takes out of their chains
    excludes: list[str] = field(default_factory=list)
    #: D372, D376: every file the run read (inputs and priors, each version's newest file),
    #: by store path and sha256
    in_files: list[InputFile] = field(default_factory=list)
    #: D372: the operator's free-text ``--note`` (``comment`` is the label)
    note: str | None = None
    #: D344 (T-STORE): the build the run belongs to (``None``: a standalone run)
    build_id: str | None = None
    #: ``annotate.<wf>`` runs (round 5, R1-1): the resolved prompt pin the run asked under (the
    #: fields its ``config_digest`` hashes, plus ``produced``), so X6 judges the run's evidence
    #: against its own pin, not today's plan; omitted from JSON when None
    prompt_pin: dict[str, Any] | None = None
    #: ``annotate.<wf>`` and ``triage`` runs (D758, :mod:`stack.plan` "Accepted configs"): the line's selection as
    #: the run applied it, :meth:`stack.plan.StageLine.selection`: ``{"digest": <selection digest>, "target": <the
    #: run's config digest; triage: question -> digest>, "accepted": [{config, name, decision, note?, question?}]}``,
    #: so verification replays the run's list, never today's plan; omitted from JSON when None
    selection: dict[str, Any] | None = None
    #: the run's spend (T-STORE, :func:`stack.llm.ledger.run_spend`): ``{"usd_micro": {"openrouter/<model>"
    #: | "x" | "openalex": int}, "reserved_open_usd_micro": int, "batch_ids": [str], "abandoned_batch_ids": [str]}``,
    #: written at the end after the run's ledgers are closed; omitted from JSON when None
    spend: dict[str, Any] | None = None
    #: D553: the collector cap/sampling environment variables set when the run started
    #: (``STACK_*_LIMIT``, ``*_FRACTION``, ``*_MAX_QUERIES``; :func:`stack.collect.sampling.cap_env_set`),
    #: present only on a ``collect.*`` run allowed to start with them (``--allow-env-limits``);
    #: omitted from JSON when None
    env_limits: dict[str, str] | None = None
    #: keys a newer writer added (R2-5): written back unchanged, never interpreted
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUS:
            raise ValueError(f"run {self.run_id}: status {self.status!r} not in {RUN_STATUS}")
        for name in _TIME_FIELDS:
            v = getattr(self, name)
            if v is not None and v.tzinfo is None:
                raise ValueError(f"run {self.run_id}: naive datetime in {name}")
        if self.error_class is not None and self.error_class not in RUN_ERROR_CLASSES:
            raise ValueError(f"run {self.run_id}: error_class {self.error_class!r} not a run error class")

    @property
    def is_final(self) -> bool:
        return self.status != "running"

    def to_json(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        extra = out.pop("extra")
        for name in ("prompt_pin", "selection", "spend", "env_limits"):
            if out[name] is None:
                del out[name]
        for name in _TIME_FIELDS:
            v = out[name]
            out[name] = v.astimezone(dt.UTC).isoformat() if v is not None else None
        return {**extra, **out}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunRecord:
        """Parse a record; keys this code does not know go to ``extra`` with a
        :class:`RunRecordWarning` (R2-5), and an ``error_class`` it does not know is kept as read
        with the same warning (S25-37, newer code may add classes). The other known fields are
        validated as before."""
        known = {f.name for f in dataclasses.fields(cls)} - {"extra"}
        unknown = sorted(set(data) - known)
        kw = {k: v for k, v in data.items() if k in known}
        if unknown:
            warnings.warn(f"run record {data.get('run_id')!r}: unknown keys {unknown} kept as extra "
                          f"(written by newer code?)", RunRecordWarning, stacklevel=2)
            kw["extra"] = {k: data[k] for k in unknown}
        for name in _TIME_FIELDS:
            if kw.get(name) is not None:
                kw[name] = dt.datetime.fromisoformat(kw[name])
        error_class = kw.get("error_class")
        if error_class is None or error_class in RUN_ERROR_CLASSES:
            return cls(**kw)
        warnings.warn(f"run record {data.get('run_id')!r}: unknown error_class {error_class!r} kept as read "
                      f"(written by newer code?)", RunRecordWarning, stacklevel=2)
        rec = cls(**{**kw, "error_class": None})
        rec.error_class = error_class
        return rec


# ---------------------------------------------------------------- paths
def run_record_path(store_root: str | Path, record: RunRecord, build: str | None = None) -> Path:
    """``builds/<build>/runs/<head>.<stage>.json.zst`` (``build``: the build directory name) or,
    standalone, ``runs/<head>.<stage>.json.zst``; ``<head>`` from ``started_at``, ``run_id`` and
    ``comment`` (D320)."""
    stem = run_stem(make_head(record.started_at, record.run_id, record.comment), record.stage)
    return StoreLayout(Path(store_root)).run_json(stem, build)


def build_dir_name(record: Mapping[str, Any]) -> str:
    """``<head>.<plan>`` of a build record (``started_at``, ``build_id``, ``comment``, ``plan``)."""
    started = dt.datetime.fromisoformat(str(record["started_at"]))
    return build_name(make_head(started, str(record["build_id"]), record.get("comment")), str(record["plan"]))


def build_record_path(store_root: str | Path, record: Mapping[str, Any]) -> Path:
    """``builds/<head>.<plan>/build.json.zst``."""
    return StoreLayout(Path(store_root)).build_json(build_dir_name(record))


# ---------------------------------------------------------------- writing (atomic)
def write_json_zst(path: Path, data: Mapping[str, Any] | list[Any]) -> Path:
    """Write ``data`` as zstd JSON at ``path`` atomically: ``.<name>.<pid>.<thread>.tmp`` beside it,
    then ``os.replace`` (a reader sees the old or the new file, never a torn one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        tmp.write_bytes(zstandard.ZstdCompressor().compress(raw))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _existing_status(path: Path) -> str | None:
    """The ``status`` of the record at ``path``, ``None`` when there is none; an unreadable file
    raises (a record is never silently replaced)."""
    try:
        raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
    except FileNotFoundError:
        return None
    data = json.loads(raw)
    return str(data.get("status")) if isinstance(data, dict) else None


def write_running_record(store_root: str | Path, record: RunRecord, build: str | None = None) -> Path:
    """(Re)write a run's ``running`` json at :func:`run_record_path` (local only; a job's tick
    uploads it)."""
    if record.is_final:
        raise ValueError(f"run {record.run_id}: a running record, got {record.status!r}")
    return write_json_zst(run_record_path(store_root, record, build), record.to_json())


def write_final_record(store_root: str | Path, record: RunRecord, build: str | None = None) -> Path:
    """Write ``record`` (final) at :func:`run_record_path` (``build``: the build directory name of
    a build's run), replacing the run's ``running`` json, locally (seeding and tests; a run's
    own end is :meth:`stack.store.job.Job.finish`). A final record is written once: an existing
    final json there raises ``FileExistsError``."""
    path = run_record_path(store_root, record, build)
    if not record.is_final:
        raise ValueError(f"run {record.run_id}: a final record, got {record.status!r}")
    have = _existing_status(path)
    if have is not None and have != "running":
        raise FileExistsError(f"{path}: run {record.run_id} has a final {have} record already")
    return write_json_zst(path, record.to_json())


def read_run_record(path: str | Path) -> RunRecord:
    """A run record (``running`` or final) from its zstd-JSON file."""
    raw = zstandard.ZstdDecompressor().decompress(Path(path).read_bytes())
    try:
        return RunRecord.from_json(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path}: unreadable run record: {exc}") from exc


def read_run_records(store_root: str | Path) -> list[RunRecord]:
    """Every **final** run record of the store (:mod:`stack.store.index`), ordered by
    ``started_at`` then ``run_id``. A record that does not parse raises (with its key)."""
    from stack.store.index import store_index

    return [r for r in store_index(store_root).records() if r.is_final]


# ---------------------------------------------------------------- build records
def read_build_record(path: str | Path) -> dict[str, Any]:
    """A build record (A.7) from its zstd-JSON file."""
    with open(path, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as r:
        data = json.loads(r.read())
    if not isinstance(data, dict):
        raise TypeError(f"{path}: a build record is a JSON object")
    return data


def write_build_record(store_root: str | Path, record: Mapping[str, Any]) -> Path:
    """Write a build record (A.7) at ``builds/<head>.<plan>/build.json.zst`` atomically, locally
    (seeding and tests; a build's own record is :class:`stack.store.job.BuildJob`'s). A final
    record is never rewritten (``FileExistsError``)."""
    path = build_record_path(store_root, record)
    have = _existing_status(path)
    if have is not None and have != "running":
        raise FileExistsError(f"{path}: build {record['build_id']} has a final {have} record already")
    return write_json_zst(path, dict(record))


def _newest_build(store_root: str | Path, plan: str | None, statuses: tuple[str, ...],
                  series: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    from stack.store.index import store_index

    idx = store_index(store_root)
    hit = idx.newest_build(plan, statuses, series=series)
    return None if hit is None else (idx.store.path(hit.key), hit.record)


def newest_ok_build(store_root: str | Path, plan: str | None = None, *,
                    series: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    """The newest ``ok`` build record (of ``plan``, of ``series`` when given) and its path, ordered by
    ``finished_at`` with the name as the tiebreak (D80); ``None`` when there is none. Not what
    ``latest`` means (that is :func:`newest_finished_build`, D703)."""
    return _newest_build(store_root, plan, ("ok",), series)


def newest_finished_build(store_root: str | Path, plan: str | None = None, *,
                          series: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    """The newest finished build record, ``ok`` or ``incomplete`` (both passed their verify; not
    ``failed``, ``cancelled`` or ``running``), in :func:`newest_ok_build`'s order. The one meaning of
    ``latest`` (D703): ``--build latest`` and ``diff … latest`` (:func:`stack.fold.release.resolve_build`),
    the build ``stack verify`` checks and the build checks' reference (X2, X10, X14, ...) when none is
    given, and ``--series S`` (D652). A release never reads it: its gate judges the builds naming its
    corpus (:func:`stack.fold.release.release_gate`)."""
    return _newest_build(store_root, plan, ("ok", "incomplete"), series)


# ---------------------------------------------------------------- running records
def running_records(store_root: str | Path) -> list[RunRecord]:
    """Every ``running`` run record (:mod:`stack.store.index`), ordered by ``started_at`` then
    ``run_id``."""
    from stack.store.index import store_index

    return [r for r in store_index(store_root).records() if not r.is_final]


def find_run_record(store_root: str | Path, run_id: str) -> RunRecord | None:
    """The run's record (final, else ``running``) from the store index, else ``None``."""
    from stack.store.index import store_index

    hit = store_index(store_root).run(run_id)
    return hit.record if hit is not None else None


# ---------------------------------------------------------------- X1 over run records (D38)
def file_digest(path: str | Path) -> str:
    """sha256 hex of a file's bytes (``outputs[].file_digest``)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class LostOutput:
    """An ``ok`` record's output whose file is neither local nor on the store's remote (a lost
    file, X1)."""

    run_id: str
    family: str
    file_id: str
    path: str


def _on_remote(store_root: Path) -> Callable[[str], bool]:
    """Whether a key is on the store's remote: ``data/`` keys from one LIST, others by HEAD; always
    false for a local-only store."""
    from stack.store.remote import open_store

    remote = open_store(store_root).remote
    if remote is None:
        return lambda _key: False
    data = {o.key for o in remote.list(f"{DATA}/")}
    return lambda key: key in data if key.startswith(f"{DATA}/") else remote.head(key) is not None


def files_without_ok_record(store_root: str | Path) -> list[Path]:
    """Local ``data/`` family files that no ``ok`` run record names (by path; D38, D345): a crash
    between the rename and the final record, a hand-placed file, a failed run's file. They are
    in no chain (chains resolve from records): garbage, reported, never read."""
    from stack.store.index import store_index

    idx = store_index(store_root)
    named = {o["path"] for _, _, o in idx.outputs()}
    root = Path(store_root)
    return [p for p in sorted((root / DATA).rglob("*.parquet"))
            if not p.name.startswith(".") and p.relative_to(root).as_posix() not in named]


def has_ok_record(store_root: str | Path, path: str | Path) -> bool:
    """Whether an ``ok`` run record names the ``data/`` file ``path`` (by path)."""
    from stack.store.index import store_index

    root = Path(store_root)
    p = Path(path)
    rel = (p if p.is_absolute() else root / p).relative_to(root).as_posix()
    return any(o["path"] == rel for _, _, o in store_index(root).outputs())


def records_without_file(store_root: str | Path) -> list[LostOutput]:
    """Outputs of ``ok`` run records (excluded files too: ``chain orphan`` moves nothing) whose
    file is neither under the local ``data/`` nor on the store's remote (X1)."""
    from stack.store.index import store_index

    root = Path(store_root)
    on_remote: Callable[[str], bool] | None = None
    lost: list[LostOutput] = []
    for e, fam, out in store_index(root).outputs():
        if (root / out["path"]).exists():
            continue
        on_remote = on_remote or _on_remote(root)
        if on_remote(out["path"]):
            continue
        lost.append(LostOutput(e.record.run_id, fam, out["file_id"], out["path"]))
    return lost


__all__ = [
    "INFLIGHT_CAP",
    "RUN_ERROR_CLASSES",
    "RUN_ERROR_CLASSES_EXTRA",
    "FoldInfo",
    "LostOutput",
    "OutputInfo",
    "RunRecord",
    "RunRecordWarning",
    "VersionInfo",
    "build_dir_name",
    "build_record_path",
    "file_digest",
    "files_without_ok_record",
    "find_run_record",
    "has_ok_record",
    "newest_finished_build",
    "newest_ok_build",
    "read_build_record",
    "read_run_record",
    "read_run_records",
    "records_without_file",
    "run_record_path",
    "running_records",
    "write_build_record",
    "write_final_record",
    "write_json_zst",
    "write_running_record",
]
