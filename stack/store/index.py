"""The store index: every build and run record of a store, with each job's liveness
(D344; ``docs/OPERATE.md``, "The store, jobs and upload").

Records are the store's index: chains, priors, liveness and spend are read from them alone.
Three record locations (D344):

```
builds/<head>.<plan>/build.json.zst                      a build record (a JSON object)
builds/<head>.<plan>/runs/<head>.<stage>.json.zst        a run of that build (RunRecord)
runs/<head>.<stage>.json.zst                             a standalone run (RunRecord)
```

beside each json a ``-running.json`` marker (``build-running.json`` for a build; written by
:mod:`stack.store.job`) and a log. A **local-only** store globs these locations. A **mirror** also LISTs
``builds/`` and ``runs/`` on S3 (on the first load and on :meth:`StoreIndex.refresh`) and GETs
(:meth:`stack.store.remote.Store.fetch`) every json that is not local, or is local and
``running`` while S3 holds a newer copy (D359); the local files are re-globbed on every
:func:`store_index` call, so a job sees its own writes at once.

**Liveness** (:func:`liveness`): a final json is ``finished`` whatever its marker; a ``running``
json whose marker (its newest mtime locally or ``LastModified`` on S3) is younger than
:data:`DEAD_AFTER` is ``live``, older ``dead``; a ``running`` json without a marker is
``unknown``. **Uploaded** (``entry.uploaded``): the json's key is on S3 (always true on a
local-only store); an index with ``uploaded_only`` (an uploading job, D353) ignores the others.

A record whose name disagrees with its content (run id, stage, build id, plan) raises
:class:`RecordIndexError`, naming the key: an old-layout file under ``runs/`` (``*.run.json.zst``) is
refused that way instead of being read around.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import warnings
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zstandard

from stack.runs import OutputInfo, RunRecord, file_digest
from stack.store.layout import BUILDS_DIR, DATA, RUNS, parse_job_name, split_job_file
from stack.store.remote import Obj, Store, open_store

#: A job rewrites its json and marker this often (D354; :mod:`stack.store.job`).
TICK = dt.timedelta(seconds=30)
#: A ``running`` job whose marker is older than this is dead (D344).
DEAD_AFTER = dt.timedelta(minutes=5)
LIVENESS: tuple[str, ...] = ("finished", "live", "dead", "unknown")
BUILD_JSON, BUILD_MARKER = "build.json.zst", "build-running.json"
#: ``runs/builds/``: where the old layout kept build records; refused, never read around.
OLD_BUILDS = "builds"
JSON_SUFFIX, MARKER_SUFFIX = ".json.zst", "-running.json"
#: Build statuses whose files a later build carries as its priors.
PRIOR_BUILD_STATUSES: tuple[str, ...] = ("ok", "incomplete")


class RecordIndexError(ValueError):
    """A record the index cannot take: unreadable, or its name disagrees with its content."""


class OutputDigestError(ValueError):
    """A downloaded data file whose sha256 is not its record's ``file_digest``."""


@dataclass(frozen=True)
class RunEntry:
    """One run record and where it sits."""

    key: str  # builds/<build>/runs/<stem>.json.zst or runs/<stem>.json.zst
    stem: str  # <head>.<stage>
    build: str | None  # the build directory name, None for a standalone run
    record: RunRecord
    uploaded: bool
    liveness: str
    marker_at: dt.datetime | None = None  # the marker's newest stamp (the last good tick), if any

    @property
    def run_id(self) -> str:
        return self.record.run_id


@dataclass(frozen=True)
class BuildEntry:
    """One build record and where it sits."""

    key: str  # builds/<build>/build.json.zst
    name: str  # <head>.<plan>
    record: dict[str, Any]
    uploaded: bool
    liveness: str
    marker_at: dt.datetime | None = None  # the marker's newest stamp (the last good tick), if any

    @property
    def build_id(self) -> str:
        return str(self.record["build_id"])

    @property
    def status(self) -> str | None:
        return self.record.get("status")


def _synced(st: os.stat_result, obj: Obj) -> bool:
    """A local copy that is the listed object: same size, mtime = ``LastModified`` (set at
    download)."""
    return (obj.last_modified is not None and st.st_size == obj.size
            and abs(st.st_mtime - obj.last_modified.timestamp()) < 1e-3)


def liveness(final: bool, marker_at: dt.datetime | None, now: dt.datetime) -> str:
    """The rule of the module docstring."""
    if final:
        return "finished"
    if marker_at is None:
        return "unknown"
    return "live" if now - marker_at < DEAD_AFTER else "dead"


def read_json_zst(path: Path) -> Any:
    with open(path, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as r:
        return json.loads(r.read())


def _finished_key(rec: Mapping[str, Any], name: str) -> tuple[dt.datetime, str]:
    """Order of build records (D80): ``finished_at`` (a missing or naive value sorts first), then
    the name."""
    raw = rec.get("finished_at")
    at = dt.datetime.min.replace(tzinfo=dt.UTC)
    if isinstance(raw, str) and raw:
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            at = parsed.astimezone(dt.UTC)
    return at, name


def _classify(key: str) -> tuple[str, str | None, str, str] | None:
    """``(kind, build, stem, suffix)`` of a job-file key, ``kind`` in ``build``/``run``; ``None``
    for any other key (a log, a temp file)."""
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == RUNS and parts[1] == OLD_BUILDS:
        raise RecordIndexError(f"{key}: a build record of the old layout (runs/builds/); the store needs the "
                               "one-off migration (read around, it would leave builds without a prior)")
    if len(parts) == 3 and parts[0] == BUILDS_DIR and parts[2] in (BUILD_JSON, BUILD_MARKER):
        return "build", parts[1], parts[1], JSON_SUFFIX if parts[2] == BUILD_JSON else MARKER_SUFFIX
    if len(parts) == 4 and parts[0] == BUILDS_DIR and parts[2] == RUNS:
        build, name = parts[1], parts[3]
    elif len(parts) == 2 and parts[0] == RUNS:
        build, name = None, parts[1]
    else:
        return None
    if not (name.endswith((JSON_SUFFIX, MARKER_SUFFIX))) or name.startswith("."):
        return None
    try:
        stem, suffix = split_job_file(name)
    except ValueError as exc:
        raise RecordIndexError(f"{key}: not a job file of the store layout ({exc})") from None
    return "run", build, stem, suffix


@dataclass
class _Parsed:
    stamp: tuple[int, int]
    value: Any


@dataclass
class StoreIndex:
    """Every record of one store (module docstring). Build it with :func:`store_index`."""

    store: Store
    uploaded_only: bool = False
    _remote: dict[str, Obj] | None = None  # key -> object, from the last LIST
    _cache: dict[str, _Parsed] = field(default_factory=dict)
    _runs: list[RunEntry] = field(default_factory=list)
    _builds: list[BuildEntry] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _listing: bool = False  # inside refresh(): a new LIST

    @property
    def root(self) -> Path:
        return self.store.root

    # ------------------------------------------------------------ loading
    def refresh(self) -> StoreIndex:
        """LIST ``builds/`` and ``runs/`` on S3 again (a mirror), then rescan."""
        with self._lock:
            if self.store.remote is not None:
                self._remote = {o.key: o for prefix in (f"{BUILDS_DIR}/", f"{RUNS}/")
                                for o in self.store.remote.list(prefix)}
            self._listing = True
            try:
                return self.rescan()
            finally:
                self._listing = False

    def rescan(self, now: dt.datetime | None = None) -> StoreIndex:
        """Re-glob the local record locations and rebuild the entries (no LIST)."""
        with self._lock:
            if self.store.remote is not None and self._remote is None:
                return self.refresh()
            now = now or dt.datetime.now(dt.UTC)
            remote = self._remote or {}
            local = self._local_keys()
            jsons: dict[str, tuple[str, str | None, str]] = {}  # key -> (kind, build, stem)
            markers: dict[tuple[str, str | None, str], dt.datetime] = {}
            for key in sorted(set(local) | set(remote)):
                c = _classify(key)
                if c is None:
                    continue
                kind, build, stem, suffix = c
                if suffix == JSON_SUFFIX:
                    jsons[key] = (kind, build, stem)
                    continue
                ats = [t for t in (local.get(key), remote[key].last_modified if key in remote else None)
                       if t is not None]
                if ats:
                    markers[(kind, build, stem)] = max(ats)
            runs: list[RunEntry] = []
            builds: list[BuildEntry] = []
            for key, (kind, build, stem) in jsons.items():
                uploaded = self.store.remote is None or key in remote
                if self.uploaded_only and not uploaded:
                    continue
                mk = f"{key.removesuffix(BUILD_JSON)}{BUILD_MARKER}" if kind == "build" else \
                    f"{key.removesuffix(JSON_SUFFIX)}{MARKER_SUFFIX}"
                value = self._load(key, remote.get(key), mk in remote)
                if value is None:
                    continue
                if kind == "build":
                    entry_b = self._build_entry(key, stem, value, uploaded, markers.get((kind, build, stem)), now)
                    builds.append(entry_b)
                else:
                    runs.append(self._run_entry(key, build, stem, value, uploaded,
                                                markers.get((kind, build, stem)), now))
            runs.sort(key=lambda e: (e.record.started_at, e.record.run_id))
            builds.sort(key=lambda e: e.name)
            self._runs, self._builds = runs, builds
            return self

    def _local_keys(self) -> dict[str, dt.datetime | None]:
        """Local job-file keys -> marker mtime (``None`` for a json)."""
        root = self.root
        out: dict[str, dt.datetime | None] = {}
        if (root / RUNS / OLD_BUILDS).is_dir() and any((root / RUNS / OLD_BUILDS).iterdir()):
            _classify(f"{RUNS}/{OLD_BUILDS}/{next((root / RUNS / OLD_BUILDS).iterdir()).name}")  # raises
        pats = (f"{BUILDS_DIR}/*/{BUILD_JSON}", f"{BUILDS_DIR}/*/{BUILD_MARKER}",
                f"{BUILDS_DIR}/*/{RUNS}/*{JSON_SUFFIX}", f"{BUILDS_DIR}/*/{RUNS}/*{MARKER_SUFFIX}",
                f"{RUNS}/*{JSON_SUFFIX}", f"{RUNS}/*{MARKER_SUFFIX}")
        for pat in pats:
            for p in root.glob(pat):
                if p.name.startswith(".") or not p.is_file():
                    continue
                key = p.relative_to(root).as_posix()
                if key.endswith((MARKER_SUFFIX, BUILD_MARKER)):
                    try:
                        out[key] = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.UTC)
                    except FileNotFoundError:
                        continue
                else:
                    out[key] = None
        return out

    def _load(self, key: str, obj: Obj | None, marker_on_remote: bool) -> Any:
        """The parsed json of ``key`` (D359). A key S3 does not hold is the local file. Otherwise
        the local copy is used while it is **synced**: its size and mtime equal the LIST's size
        and ``LastModified`` (a download sets the mtime to it), and it is not a ``running`` json
        whose marker a new LIST no longer shows (a same-second final PUT). Anything else is re-fetched,
        final jsons too (a LIST change is a change); never by comparing with the local mtime as a
        clock. A download goes to a temp file and replaces the local copy only when that copy did
        not change meanwhile and the download is not a ``running`` json over a local final one."""
        path = self.store.path(key)
        if obj is None:
            return self._parse(key, path) if path.is_file() else None
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        if st is not None:
            cached = self._parse(key, path)
            running = isinstance(cached, dict) and cached.get("status") == "running"
            if _synced(st, obj) and not (running and not marker_on_remote and self._listing):
                return cached
        self._fetch_json(key, obj, path, st)
        return self._parse(key, path) if path.is_file() else None

    def _fetch_json(self, key: str, obj: Obj, path: Path, seen: os.stat_result | None) -> None:
        assert self.store.remote is not None
        tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.dl"
        try:
            self.store.remote.download(key, tmp)
        except KeyError:
            return  # gone since the LIST
        try:
            got = read_json_zst(tmp)
            if seen is not None:
                local = self._parse(key, path)
                if isinstance(local, dict) and local.get("status") not in (None, "running") \
                        and isinstance(got, dict) and got.get("status") == "running":
                    return  # a local final json is never replaced by a remote running one
                now = path.stat() if path.exists() else None
                if now is None or (now.st_mtime_ns, now.st_size) != (seen.st_mtime_ns, seen.st_size):
                    return  # written here meanwhile (a job's own record): keep it
            if obj.last_modified is not None:
                ts = obj.last_modified.timestamp()
                os.utime(tmp, (ts, ts))
            os.replace(tmp, path)
            self._cache.pop(key, None)  # the download may keep the cached copy's (mtime, size) stamp
        except (OSError, ValueError, zstandard.ZstdError) as exc:
            raise RecordIndexError(f"{key}: unreadable record on S3: {type(exc).__name__}: {exc}") from None
        finally:
            tmp.unlink(missing_ok=True)

    def _parse(self, key: str, path: Path) -> Any:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._cache.get(key)
        if hit is not None and hit.stamp == stamp:
            return hit.value
        try:
            value = read_json_zst(path)
        except (OSError, ValueError, zstandard.ZstdError) as exc:
            raise RecordIndexError(f"{key}: unreadable record: {type(exc).__name__}: {exc}") from None
        if not isinstance(value, dict):
            raise RecordIndexError(f"{key}: a record is a JSON object")
        self._cache[key] = _Parsed(stamp, value)
        return value

    def _run_entry(self, key: str, build: str | None, stem: str, value: dict[str, Any], uploaded: bool,
                   marker: dt.datetime | None, now: dt.datetime) -> RunEntry:
        try:
            rec = RunRecord.from_json(value)
        except (ValueError, TypeError, KeyError) as exc:
            raise RecordIndexError(f"{key}: unreadable run record: {exc}") from None
        job = parse_job_name(stem)
        if (job.head.id, job.tail) != (rec.run_id, rec.stage):
            raise RecordIndexError(f"{key}: the name says run {job.head.id} of stage {job.tail}, the record "
                              f"{rec.run_id} of {rec.stage} (an old-layout file? the store needs migrating)")
        return RunEntry(key, stem, build, rec, uploaded, liveness(rec.is_final, marker, now), marker)

    def _build_entry(self, key: str, name: str, value: dict[str, Any], uploaded: bool,
                     marker: dt.datetime | None, now: dt.datetime) -> BuildEntry:
        job = parse_job_name(name)
        if (job.head.id, job.tail) != (value.get("build_id"), value.get("plan")):
            raise RecordIndexError(f"{key}: the name says build {job.head.id} of plan {job.tail}, the record "
                              f"{value.get('build_id')} of {value.get('plan')}")
        final = value.get("status") != "running"
        return BuildEntry(key, name, value, uploaded, liveness(final, marker, now), marker)

    def remote_objects(self) -> dict[str, Obj] | None:
        """The last LIST of ``builds/`` and ``runs/`` on S3 (key -> object); ``None`` for a
        local-only store."""
        with self._lock:
            return None if self.store.remote is None else dict(self._remote or {})

    # ------------------------------------------------------------ runs
    @property
    def runs(self) -> list[RunEntry]:
        """Every run record, ordered by ``started_at`` then run id."""
        return list(self._runs)

    @property
    def builds(self) -> list[BuildEntry]:
        """Every build record, ordered by name."""
        return list(self._builds)

    def run(self, run_id: str) -> RunEntry | None:
        hits = [e for e in self._runs if e.record.run_id == run_id]
        if len(hits) > 1:
            raise RecordIndexError(f"run {run_id}: {len(hits)} records: {[e.key for e in hits]}")
        return hits[0] if hits else None

    def records(self) -> list[RunRecord]:
        return [e.record for e in self._runs]

    def ok_runs(self) -> list[RunEntry]:
        return [e for e in self._runs if e.record.status == "ok"]

    def excluded(self) -> frozenset[str]:
        """File ids taken out of their directories by an ``ok`` record's ``excludes`` (``chain orphan``)."""
        return frozenset(f for e in self.ok_runs() for f in e.record.excludes)

    def outputs(self) -> Iterator[tuple[RunEntry, str, OutputInfo]]:
        """``(entry, outputs key, output)`` of every ``ok`` record, excluded files included."""
        for e in self.ok_runs():
            for k, out in e.record.outputs.items():
                yield e, k, out

    def data_outputs(self) -> Iterator[tuple[RunEntry, str, OutputInfo]]:
        """:meth:`outputs` under ``data/`` (a release run's output sits under ``releases/``)."""
        for e, k, o in self.outputs():
            if str(o["path"]).startswith(f"{DATA}/"):
                yield e, k, o

    def output(self, fid: str) -> tuple[RunEntry, str, OutputInfo] | None:
        """The ``ok`` record output with file id ``fid`` (excluded or not); ``None`` if none."""
        hits = [h for h in self.outputs() if h[2]["file_id"] == fid]
        if len(hits) > 1:
            raise RecordIndexError(f"file {fid}: named by {len(hits)} ok records: {[h[0].key for h in hits]}")
        return hits[0] if hits else None

    def dir_outputs(self, dir_key: str) -> list[tuple[RunEntry, str, OutputInfo]]:
        """The published files of directory ``dir_key`` (``data/<family>/<series>``): outputs of
        ``ok`` records whose path is directly in it, minus every excluded file id (D372).
        An output without ``series`` (a record written before D372) raises: the store needs the
        one-off series migration."""
        ex = self.excluded()
        d = dir_key.rstrip("/") + "/"
        out = []
        for e, k, o in self.data_outputs():
            path = str(o["path"])
            if not path.startswith(d) or "/" in path[len(d):]:
                continue
            if "series" not in o:
                raise RecordIndexError(f"{e.key}: output {k} has no series (a record written before D372; "
                                       "the store needs the one-off series migration)")
            if o["file_id"] not in ex:
                out.append((e, k, o))
        return out

    def series_dirs(self, family_dir: str | None = None) -> list[str]:
        """Series directories (``data/<family>/<series>``) with at least one published file, of
        ``family_dir`` when given, sorted."""
        ex = self.excluded()
        out = set()
        for _e, _k, o in self.data_outputs():
            parts = str(o["path"]).split("/")
            if len(parts) == 4 and o["file_id"] not in ex and (family_dir is None or parts[1] == family_dir):
                out.add("/".join(parts[:3]))
        return sorted(out)

    def fetch_output(self, out: Mapping[str, Any]) -> Path:
        """The local path of a recorded output whose bytes have the record's ``file_digest``.
        A local file is hashed (:func:`local_digest`, once per process per file state; D371); on a
        mismatch a mirror moves it aside (``.<name>.mismatch-<utc>`` in ``data/``, which no reader
        lists) with a warning and downloads the store's copy, while a local-only store raises
        :class:`OutputDigestError` naming the file and leaves it in place. A download goes into a
        temp file, checked against ``file_digest``, then renamed into place, so a corrupt body is
        never at the output's path (:class:`OutputDigestError` naming the key; D359).
        ``FileNotFoundError`` when neither side has it."""
        key = str(out["path"])
        path = self.store.path(key)
        want = str(out["file_digest"])
        mismatch = None
        if path.is_file():
            got = local_digest(path)
            if got == want:
                return path
            mismatch = f"{path}: local sha256 {got[:12]}… but the record of {key} says {want[:12]}…"
            if self.store.remote is None:
                raise OutputDigestError(f"{mismatch} (a local-only store: nothing to re-download)")
            aside = path.parent / f".{path.name}.mismatch-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%fZ}"
            os.replace(path, aside)
            mismatch += f"; moved aside to {aside.name}"
            warnings.warn(f"{mismatch}, downloading the store's copy", stacklevel=2)
        if self.store.remote is None:
            raise FileNotFoundError(f"{key}: recorded output is neither local nor on the store's remote")
        tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.dl"
        try:
            try:
                self.store.remote.download(key, tmp)
            except KeyError:
                if mismatch is not None:
                    raise OutputDigestError(f"{mismatch}, and the store's remote has no copy") from None
                raise FileNotFoundError(f"{key}: recorded output is neither local nor on the store's remote") from None
            digest = file_digest(tmp)
            if digest != want:
                raise OutputDigestError(f"{key}: downloaded sha256 {digest[:12]}… but the record says "
                                        f"{want[:12]}…")
            os.replace(tmp, path)
            _remember(path, digest)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    # ------------------------------------------------------------ builds
    def newest_build(self, plan: str | None, statuses: tuple[str, ...], *,
                     series: str | None = None) -> BuildEntry | None:
        """The newest build record (of ``plan``, of ``series`` when given) whose status is in
        ``statuses``, by ``finished_at`` then name (D80); a ``running`` build never matches unless
        asked for."""
        hits = [b for b in self._builds if b.status in statuses and (plan is None or b.record.get("plan") == plan)
                and (series is None or b.record.get("series") == series)]
        return max(hits, key=lambda b: _finished_key(b.record, b.name)) if hits else None

    def build(self, ref: str) -> BuildEntry | None:
        """A build by id or by directory name."""
        hits = [b for b in self._builds if ref in (b.build_id, b.name)]
        if len(hits) > 1:
            raise RecordIndexError(f"build {ref}: {len(hits)} records: {[b.key for b in hits]}")
        return hits[0] if hits else None


_indexes: dict[Path, StoreIndex] = {}
_ilock = threading.Lock()


def store_index(root: str | Path, *, refresh: bool = False, uploaded_only: bool | None = None) -> StoreIndex:
    """The index of the store at ``root`` (one per resolved root per process), rescanned locally;
    ``refresh`` re-LISTs a mirror's remote first; ``uploaded_only`` (when given) sets the option
    for every later use in this process (an uploading job, D353)."""
    store = open_store(root)
    ck = Path(root).resolve()
    with _ilock:
        idx = _indexes.get(ck)
        if idx is None or idx.store is not store:  # open_store's cache was reset (a test's fake client)
            idx = _indexes[ck] = StoreIndex(store)
    if uploaded_only is not None:
        idx.uploaded_only = uploaded_only
    return idx.refresh() if refresh else idx.rescan()


def forget_indexes() -> None:
    """Drop every cached index and every remembered local digest (tests)."""
    with _ilock:
        _indexes.clear()
    with _dlock:
        _digests.clear()


#: ``(resolved path, size, mtime_ns) -> sha256`` of local data files hashed in this process (D371).
_digests: dict[tuple[Path, int, int], str] = {}
_dlock = threading.Lock()


def _state(path: Path) -> tuple[Path, int, int]:
    p = path.resolve()
    st = p.stat()
    return p, st.st_size, st.st_mtime_ns


def _remember(path: Path, digest: str) -> None:
    with _dlock:
        _digests[_state(path)] = digest


def local_digest(path: str | Path) -> str:
    """sha256 of a local file, hashed at most once per process while its resolved path, size and
    ``mtime_ns`` stay the same (D371); a file that changed while it was hashed is not remembered."""
    before = _state(Path(path))
    with _dlock:
        hit = _digests.get(before)
    if hit is not None:
        return hit
    digest = file_digest(before[0])
    if _state(before[0]) == before:
        with _dlock:
            _digests[before] = digest
    return digest


__all__ = [
    "DEAD_AFTER",
    "LIVENESS",
    "PRIOR_BUILD_STATUSES",
    "TICK",
    "BuildEntry",
    "OutputDigestError",
    "RecordIndexError",
    "RunEntry",
    "StoreIndex",
    "forget_indexes",
    "liveness",
    "local_digest",
    "read_json_zst",
    "store_index",
]
