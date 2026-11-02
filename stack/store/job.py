"""Jobs on a store: one build or one run with its record, log and marker, its tick, its upload,
the recovery at an uploading job's start, and ``stack store push`` (D352-D359).

A **job** (:class:`Job`; a run's is :class:`RunJob`) owns three files beside each other
(:mod:`stack.store.layout`): the record ``<stem>.json.zst`` (``build.json.zst``), the log
``<stem>.log.zst`` (``build.log.zst``) and the marker ``<stem>-running.json``
(``build-running.json``) = ``{"tick": "<iso utc>"}``. ``upload`` is fixed at the start and recorded
(default: the store has a remote; a build's runs inherit the build's). A no-upload job writes its
files into the mirror only (blobs still go up, D361).

**Order** (§6.2-§6.5), the contract the tests pin:

* :meth:`Job.start`: the ``running`` json, the log and the marker locally; uploading: PUT json,
  log, marker, before any work (a failed PUT raises :class:`JobStartError`). The caller refreshed
  the index and ran :func:`recover` before (:func:`prepare`).
* :meth:`Job.tick` (every :data:`TICK`, a daemon thread): under the record lock the record is
  snapshotted (a run's ``spend`` from :func:`stack.llm.ledger.run_spend`) and the local json
  rewritten (temp + rename); then the log and the marker locally; uploading: PUT json, PUT log,
  PUT marker. A tick never writes a final json. Ticks failing **continuously** for
  :data:`SELF_CANCEL_AFTER`, measured from the first failed tick of the streak (D493;
  S25-12: not from the last good tick, so one failed tick after a suspend does not cancel), set the
  job's cancel flag; a good tick ends the streak and clears a flag nothing has raised yet (not sticky).
  :meth:`Job.check_cancel` (the stage context's ``heartbeat()``/``substage()``, annotate's and triage's
  ask loops, the build between stages, :func:`stack.store.chain.publish` before ``claim``) raises
  :class:`JobCancelled` on a set flag, from then on for good, and the job ends ``cancelled[tick_failed]``
  without publishing. Nothing checks it after ``claim``.
* publish (:func:`stack.store.chain.publish`, an ``ok`` run): ``claim`` against a refreshed
  index, the rename of ``data/<family>[/<series>]/.<name>.tmp``, the data PUTs (uploading), then the final record.
* :meth:`Job.finish` (every outcome): stop and join the tick thread, the final json locally
  (refusing an existing final one), the log drained and written; uploading: PUT json, PUT log,
  DELETE marker (the only runtime delete); the local marker goes too. A failed PUT here is a
  warning: the final record is local, and the next uploading job's :func:`recover` pushes it.

**Logs.** One buffer per job, written whole as zstd at each tick and at the end. Lines go to the
**active** job (:func:`current_job`, the innermost started and unfinished job: during a run its
run, otherwise the build). In the CLI process :func:`capture_stderr` (``stack.cli.main``) puts a
pipe on fd 2 and a reader thread that forwards to the original stderr and appends to the active
job's buffer, so subprocess and MuPDF output is kept; :meth:`Job.finish` drains it before the final
log write. Without it (library calls, tests) a job's log holds its own lines (:func:`say`).

**Drain** (D570-D573): a graceful stop of this process's job. :func:`request_drain` (the CLI's
first SIGTERM, ``stack.cli``; or a stop request read by a tick, below) sets the process's drain once:
the running stage admits no new work and waits at most ``wait_s`` (:data:`DRAIN_WAIT_S` by default)
for its in-flight items (:meth:`stack.stage_runner.StageContext.drain`), the run publishes what it
completed and ends ``ok``, ``scope_complete = false``, count ``stopped.drain``; a build then starts
no further run (:mod:`stack.build`). It lasts until the outermost job of the process ends. A
**stop request** is a small json ``{"mode": "drain"|"now", "requested_at", "wait_s"}`` beside the
job's marker: ``<stem>-stop.json`` (``build-stop.json`` for a build), written by ``stack stop``
locally and, on an S3 store for an uploading job, PUT at its key. Each tick reads the local file
and, uploading, HEADs the key (:meth:`Job.poll_stop`; a failed read is ignored and retried at the
next tick, not a tick failure): ``drain`` drains, ``now`` is the hard cancel
(:func:`hard_cancel`: ``KeyboardInterrupt`` in the main thread, ``cancelled[operator]``). The end
of a job that acted on a request removes it (local, and the S3 key when uploading: with the marker
DELETE the only runtime deletes). The index and recovery ignore the file (not a job file name).
:func:`request_stop` (``stack stop``) writes requests for live jobs only.

**Recovery** (:func:`recover`, §6.2) and **push** (:func:`push_job`, :func:`check_push`, §6.1,
§7.5) keep the publish order: data files, then the json, then the log, then the marker deleted;
a release's ``release.json`` last of its directory.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import select
import signal
import struct
import sys
import termios
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

from stack.runs import RunRecord, run_record_path, write_json_zst
from stack.store.index import DEAD_AFTER, TICK, StoreIndex, read_json_zst, store_index
from stack.store.layout import BUILDS_DIR, DATA, NONE, RELEASES, RUNS, StoreLayout
from stack.store.remote import Store, open_store
from stack.store.s3config import S3TransientError, is_s3_transient, redact_error

#: Ticks failing continuously this long, from the first failed tick, cancel the job (§6.3: half of
#: DEAD_AFTER; D493: counted from the streak's first failure, cleared by a good tick).
SELF_CANCEL_AFTER = DEAD_AFTER / 2
JSON_SUFFIX, LOG_SUFFIX, MARKER_SUFFIX = ".json.zst", ".log.zst", "-running.json"
BUILD_JSON, BUILD_LOG, BUILD_MARKER = "build.json.zst", "build.log.zst", "build-running.json"
#: A stop request beside a job's marker (module docstring, Drain; D572).
STOP_SUFFIX, BUILD_STOP = "-stop.json", "build-stop.json"
STOP_MODES: tuple[str, ...] = ("drain", "now")
#: Seconds a drained stage waits for its in-flight items before it abandons them as not attempted
#: (D571); a stop request may carry its own ``wait_s``.
DRAIN_WAIT_S = 120.0
#: A SIGTERM this soon after the drain began is the same stop, not the hard cancel (D570): a signal to
#: the process group reaches ``stack`` twice under ``uv run``, which forwards it.
SIGTERM_REPEAT_S = 5.0
RELEASE_JSON = "release.json"

#: Crash injection (tests only, as ``stack.stage_runner``'s ``STACK_CRASH_AT``): SIGKILL at one
#: point of a run's end. ``after_claim`` (``claim`` passed, nothing renamed),
#: ``after_rename`` (before the data PUT), ``after_data_put`` (before the final json),
#: ``after_final_local`` (the final json local, before its PUT), ``after_final_put`` (json and
#: log PUT, before the marker DELETE).
CRASH_ENV = "STACK_CRASH_AT"
CRASH_POINTS: tuple[str, ...] = ("after_claim", "after_rename", "after_data_put", "after_final_local",
                                 "after_final_put")


def crash_point() -> str | None:
    """The configured crash point; an unknown value raises ``ValueError``."""
    at = os.environ.get(CRASH_ENV)
    if at is not None and at not in CRASH_POINTS:
        raise ValueError(f"{CRASH_ENV}={at!r} not in {CRASH_POINTS}")
    return at


def maybe_die(point: str) -> None:
    """SIGKILL this process at ``point`` when ``STACK_CRASH_AT`` names it; the line saying so goes
    straight to the original stderr (under :func:`capture_stderr` a line still in the pipe dies
    with the process)."""
    if os.environ.get(CRASH_ENV) == point:
        with contextlib.suppress(OSError, ValueError):
            sys.stderr.flush()
        fd = _capture.saved if _capture is not None else 2
        os.write(fd, f"{CRASH_ENV}={point}: SIGKILL pid {os.getpid()}\n".encode())
        os.kill(os.getpid(), signal.SIGKILL)


class JobCancelled(BaseException):
    """The job's ticks failed continuously for :data:`SELF_CANCEL_AFTER`: it ends ``cancelled[tick_failed]``. A
    ``BaseException`` (as ``KeyboardInterrupt``), so a stage's ``except Exception`` lets it pass."""


class JobStartError(RuntimeError):
    """An uploading job's start PUTs failed: it does no work and ends ``failed[tick_failed]``."""


class PushError(RuntimeError):
    """``stack store push`` refused, or a record cannot be pushed (a data file it names is
    neither local nor on S3)."""


def _utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(at: dt.datetime) -> str:
    return at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- the active job and its log
_active: list[Job] = []
_alock = threading.Lock()


def current_job() -> Job | None:
    """The innermost started, unfinished job of this process (a build's run inside its build)."""
    with _alock:
        return _active[-1] if _active else None


def job_active() -> bool:
    """Whether a job of this process is started and unfinished; lock-free, for a signal handler."""
    return bool(_active)


# ---------------------------------------------------------------- drain (D570-D573)
@dataclass(frozen=True)
class Drain:
    """The process's drain (module docstring): who asked (``signal`` or ``request``), when (``at``;
    ``since`` = ``time.monotonic()`` then), and the bounded wait: ``deadline`` = ``since + wait_s``."""

    by: str
    at: dt.datetime
    wait_s: float
    deadline: float
    since: float

    def to_json(self) -> dict[str, Any]:
        return {"by": self.by, "requested_at": _iso(self.at), "wait_s": self.wait_s}


_drain: Drain | None = None
_hard = False  # a `now` stop request asked for the hard cancel (:func:`hard_cancel`)


def request_drain(by: str, wait_s: float | None = None) -> bool:
    """Set the process's drain; ``True`` when this call set it, ``False`` when one is set already
    (the first wins). No lock and no I/O: a signal handler calls it."""
    global _drain
    if _drain is not None:
        return False
    w = DRAIN_WAIT_S if wait_s is None else max(0.0, float(wait_s))
    now = time.monotonic()
    _drain = Drain(by, _utc(), w, now + w, now)
    return True


def drain_state() -> Drain | None:
    """The process's drain, ``None`` when none was requested."""
    return _drain


def clear_drain() -> None:
    """Forget the drain (the outermost job's end; tests)."""
    global _drain, _hard
    _drain = None
    _hard = False


def hard_cancel_requested() -> bool:
    """Whether :func:`hard_cancel` was called (the CLI's SIGTERM handler then cancels at once)."""
    return _hard


def hard_cancel() -> None:
    """The hard cancel from any thread (a ``now`` stop request, D572): ``KeyboardInterrupt`` in the
    main thread, as a second SIGTERM. When this process handles SIGTERM in Python (the CLI,
    ``stack.cli``), SIGTERM is sent to the main thread (a real signal wakes a blocked call) with a drain
    set, which the handler turns into ``KeyboardInterrupt``; otherwise :func:`_thread.interrupt_main`
    (library callers: effective when Python handles SIGINT)."""
    import _thread

    global _hard
    request_drain("request")
    _hard = True
    if callable(signal.getsignal(signal.SIGTERM)):
        ident = threading.main_thread().ident
        assert ident is not None
        signal.pthread_kill(ident, signal.SIGTERM)
    else:
        _thread.interrupt_main()


def stop_request_bytes(mode: str, wait_s: float | None = None, at: dt.datetime | None = None) -> bytes:
    """A stop request's json (module docstring, Drain)."""
    if mode not in STOP_MODES:
        raise ValueError(f"stop mode {mode!r} not in {STOP_MODES}")
    body = {"mode": mode, "requested_at": _iso(at or _utc()), "wait_s": wait_s}
    return (json.dumps(body) + "\n").encode()


def parse_stop_request(raw: bytes) -> tuple[str, float | None]:
    """``(mode, wait_s)`` of a stop request; an unreadable one is a ``drain`` (a stop was asked)."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "drain", None
    if not isinstance(body, dict):
        return "drain", None
    mode = body.get("mode") if body.get("mode") in STOP_MODES else "drain"
    wait = body.get("wait_s")
    ok = isinstance(wait, int | float) and not isinstance(wait, bool) and wait >= 0
    return str(mode), float(wait) if ok else None


def stop_path(marker_path: Path) -> Path:
    """The stop request beside a job's marker (``…-running.json`` → ``…-stop.json``)."""
    name = marker_path.name
    if name == BUILD_MARKER:
        return marker_path.with_name(BUILD_STOP)
    if not name.endswith(MARKER_SUFFIX):
        raise ValueError(f"{marker_path}: not a job marker")
    return marker_path.with_name(name.removesuffix(MARKER_SUFFIX) + STOP_SUFFIX)


class _Log:
    """A job's whole log, in memory; written whole at each tick and at the end."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._buf += data

    def data(self) -> bytes:
        with self._lock:
            return bytes(self._buf)


class _Capture:
    """fd 2 onto a pipe; a reader thread forwards each chunk to the original stderr and appends it
    to the active job's log. Every read and its handling happen under ``_lock``, so
    :meth:`drain` (an empty pipe while holding the lock) means every byte written so far is in."""

    def __init__(self) -> None:
        sys.stderr.flush()
        self.saved = os.dup(2)
        self.r, w = os.pipe()
        os.dup2(w, 2)
        os.close(w)
        self._lock = threading.Lock()
        self._closed = False
        self.thread = threading.Thread(target=self._pump, name="stderr-capture", daemon=True)
        self.thread.start()

    def _pending(self) -> int:
        return int(struct.unpack("i", fcntl.ioctl(self.r, termios.FIONREAD, b"\0\0\0\0"))[0])

    def _read_one(self) -> bool:
        chunk = os.read(self.r, 1 << 16)
        if not chunk:
            return False
        view = memoryview(chunk)
        while view:
            try:
                view = view[os.write(self.saved, view):]
            except OSError:
                break  # the original stderr is gone; keep logging
        job = current_job()
        if job is not None:
            job.log.append(chunk)
        return True

    def _pump(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self.r], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not ready:
                if self._closed:
                    return
                continue
            with self._lock:
                try:
                    if not self._read_one():
                        return
                except OSError:
                    return

    def drain(self, timeout: float = 2.0) -> None:
        """Wait until every byte written to fd 2 so far went through the reader."""
        with contextlib.suppress(OSError, ValueError):
            sys.stderr.flush()
        deadline = _mono() + timeout
        while _mono() < deadline:
            with self._lock:
                if self._pending() == 0:
                    return
            _sleep(0.005)

    def close(self) -> None:
        self.drain()
        os.dup2(self.saved, 2)  # our end of the pipe closes; children may still hold one
        with self._lock:
            while self._pending() > 0 and self._read_one():
                pass
            self._closed = True
        self.thread.join(timeout=1.0)
        os.close(self.saved)


def _mono() -> float:
    import time

    return time.monotonic()


def _sleep(s: float) -> None:
    import time

    time.sleep(s)


_capture: _Capture | None = None


@contextlib.contextmanager
def capture_stderr() -> Iterator[None]:
    """The CLI process's fd-level capture (module docstring); a no-op when fd 2 is not open.
    Never used under pytest (``stack.cli.main`` only)."""
    global _capture
    try:
        os.fstat(2)
    except OSError:
        yield
        return
    cap = _Capture()
    _capture = cap
    try:
        yield
    finally:
        _capture = None
        cap.close()


def drain_stderr() -> None:
    if _capture is not None:
        _capture.drain()


def say(message: str) -> None:
    """One line of the active job's own (a warning, an orchestrator line): to stderr, and straight
    into the active job's log when stderr is not captured (library calls, tests); when it is, the
    reader thread logs it."""
    line = message if message.endswith("\n") else message + "\n"
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(line)
        sys.stderr.flush()
    own_line(line)


def own_line(line: str) -> None:
    """Append ``line`` to the active job's log unless stderr is captured (then it is logged as
    written to stderr)."""
    if _capture is not None:
        return
    job = current_job()
    if job is not None:
        job.log.append((line if line.endswith("\n") else line + "\n").encode("utf-8", "replace"))


# ---------------------------------------------------------------- the job
def default_upload(store: Store, upload: bool | None) -> bool:
    """``upload`` as given, else whether the store has a remote; ``True`` on a local-only store
    raises ``ValueError``."""
    if upload is None:
        return store.remote is not None
    if upload and store.remote is None:
        raise ValueError(f"store {store.root}: a local-only store has nothing to upload to")
    return bool(upload)


class Job:
    """One build or run (module docstring). ``snapshot()`` returns the record's current JSON
    (status ``running``), called under :attr:`lock`. ``clock`` stamps markers and measures the
    failing time (tests inject one); ``interval`` is the tick period."""

    def __init__(self, store: Store, *, json_path: Path, log_path: Path, marker_path: Path, upload: bool,
                 snapshot: Callable[[], dict[str, Any]], label: str, lock: threading.RLock | None = None,
                 interval: dt.timedelta | None = None, clock: Callable[[], dt.datetime] | None = None) -> None:
        self.store = store
        self.json_path, self.log_path, self.marker_path = json_path, log_path, marker_path
        self.json_key, self.log_key, self.marker_key = (store.key(p) for p in (json_path, log_path, marker_path))
        self.stop_path = stop_path(marker_path)  # D572: a stop request beside the marker
        self.stop_key = store.key(self.stop_path)
        #: the mode of the stop request acted on (``None``: none); the job's end removes the request
        self.stop_seen: str | None = None
        self.upload = default_upload(store, upload)
        self.snapshot = snapshot
        self.label = label
        self.lock = lock or threading.RLock()
        self.interval = interval or TICK  # read at construction: tests shorten it
        self.clock = clock or _utc
        self.log = _Log()
        self.cancel = threading.Event()
        self.cancel_reason: str | None = None
        #: ticks that failed (counted in the run record's ``counts.tick_errors``)
        self.tick_failures = 0
        #: the first failed tick of the current streak of failures (``None``: the last tick was good)
        self._failing_since: dt.datetime | None = None
        #: guards the cancel flag between a tick clearing it and :meth:`check_cancel` raising it
        self._cancel_lock = threading.Lock()
        self._cancel_raised = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._final = False
        #: the final json is written locally: from here on the job's outcome is fixed (an
        #: interrupt during the uploads that follow must not write a second final record)
        self.final_written = False
        self._started = False

    # ------------------------------------------------------------ local files
    def _write_running(self) -> None:
        """Under :attr:`lock`: the snapshot as the local ``running`` json (never a final one)."""
        data = self.snapshot()
        if data.get("status") != "running":
            raise ValueError(f"{self.label}: a tick writes a running json, got status {data.get('status')!r}")
        write_json_zst(self.json_path, data)

    def _write_log(self) -> None:
        _write_atomic(self.log_path, zstandard.ZstdCompressor().compress(self.log.data()))

    def _marker_bytes(self, at: dt.datetime) -> bytes:
        return (json.dumps({"tick": _iso(at)}) + "\n").encode()

    def save(self) -> None:
        """Rewrite the local ``running`` json now (no upload; the tick uploads)."""
        with self.lock:
            if not self._final:
                self._write_running()

    # ------------------------------------------------------------ start, tick, finish
    def start(self) -> None:
        """Write the ``running`` json, the log and the marker locally; uploading, PUT them in that
        order (:class:`JobStartError` when one fails). The job is then the active one."""
        with _alock:
            _active.append(self)
        self._started = True
        at = self.clock()
        with self.lock:
            self._write_running()
        self._write_log()
        _write_atomic(self.marker_path, self._marker_bytes(at))
        if self.upload:
            assert self.store.remote is not None
            try:
                self.store.remote.put_file(self.json_key, self.json_path)
                self.store.remote.put_file(self.log_key, self.log_path)
                self.store.remote.put_bytes(self.marker_key, self._marker_bytes(at))
            except Exception as exc:
                raise JobStartError(f"{self.label}: start upload failed: {_s3_error(exc)}") from exc

    def tick(self) -> bool:
        """One tick (module docstring); ``False`` when it failed. After :meth:`finish` began, a
        no-op. Then the stop request is read (:meth:`poll_stop`), whatever the tick's outcome."""
        ok = self._tick()
        if not self._final:
            self.poll_stop()
        return ok

    def poll_stop(self) -> str | None:
        """Read this job's stop request (module docstring, Drain): the local file, then, uploading,
        the S3 key (HEAD, GET when present). A ``drain`` request drains the process
        (:func:`request_drain`), a ``now`` one is :func:`hard_cancel`; each is acted on once.
        Returns the mode acted on (``None``: nothing new). A read that fails is ignored: the next
        tick reads again."""
        if self.stop_seen == "now":
            return None
        raw: bytes | None = None
        try:
            raw = self.stop_path.read_bytes()
        except OSError:
            raw = None
        if raw is None and self.upload and self.store.remote is not None:
            try:
                if self.store.remote.head(self.stop_key) is not None:
                    raw = self.store.remote.get_bytes(self.stop_key)
            except Exception:  # noqa: BLE001 — an S3 hiccup: read again at the next tick
                raw = None
        if raw is None:
            return None
        mode, wait_s = parse_stop_request(raw)
        if mode == self.stop_seen or (mode == "drain" and self.stop_seen is not None):
            return None
        self.stop_seen = mode
        if mode == "now":
            with contextlib.suppress(Exception):
                say(f"{self.label}: stop request (now): hard cancel")
            hard_cancel()
        elif request_drain("request", wait_s):
            d = drain_state()
            with contextlib.suppress(Exception):
                say(f"{self.label}: stop request (drain): no new work; in-flight work gets up to "
                    f"{d.wait_s if d is not None else DRAIN_WAIT_S:.0f}s, then what is done is published")
        return mode

    def _tick(self) -> bool:
        at = self.clock()
        try:
            with self.lock:
                if self._final:
                    return True
                self._write_running()
            self._write_log()
            _write_atomic(self.marker_path, self._marker_bytes(at))
            if self.upload:
                assert self.store.remote is not None
                self.store.remote.put_file(self.json_key, self.json_path)
                self.store.remote.put_file(self.log_key, self.log_path)
                self.store.remote.put_bytes(self.marker_key, self._marker_bytes(at))
        except Exception as exc:  # noqa: BLE001 — counted, retried, and after SELF_CANCEL_AFTER a cancel
            self.tick_failures += 1
            since = self._failing_since = self._failing_since or at  # D493: the streak's first failure
            if at - since >= SELF_CANCEL_AFTER:
                with self._cancel_lock:
                    if not self.cancel.is_set():
                        self.cancel_reason = (f"{self.label}: ticks failing continuously since {_iso(since)} "
                                              f"(>= {SELF_CANCEL_AFTER}); last: {_s3_error(exc)}")
                        self.cancel.set()
                        with contextlib.suppress(Exception):
                            say(f"warning: {self.cancel_reason}; cancelling the job")
            else:
                with contextlib.suppress(Exception):
                    say(f"warning: {self.label}: tick failed ({_s3_error(exc)}); retrying")
            return False
        if self._failing_since is not None:  # D493: a good tick ends the streak
            self._failing_since = None
            with self._cancel_lock:
                if self.cancel.is_set() and not self._cancel_raised:
                    self.cancel.clear()
                    with contextlib.suppress(Exception):
                        say(f"warning: {self.label}: ticks recovered before the cancel took effect; "
                            f"not cancelling ({self.cancel_reason})")
                    self.cancel_reason = None
        return True

    def start_tick(self) -> None:
        secs = self.interval.total_seconds()
        if secs <= 0:
            raise ValueError(f"{self.label}: tick interval {self.interval} must be positive")

        def loop() -> None:
            while not self._stop.wait(secs):
                self.tick()

        self._thread = threading.Thread(target=loop, name=f"tick-{self.label}", daemon=True)
        self._thread.start()

    def stop_tick(self) -> None:
        """Stop and join the tick thread (an in-flight tick completes first)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def check_cancel(self) -> None:
        """Raise :class:`JobCancelled` when the cancel flag is set; once raised, the cancel stands (a later
        good tick no longer clears it, D493)."""
        with self._cancel_lock:
            if not self.cancel.is_set():
                return
            self._cancel_raised = True
        raise JobCancelled(self.cancel_reason or f"{self.label}: cancelled")

    def finish(self, final: Mapping[str, Any]) -> list[str]:
        """End the job with the record ``final`` (module docstring); returns warnings (a failed
        upload). The final json is written once: an existing final one raises ``FileExistsError``."""
        if final.get("status") == "running":
            raise ValueError(f"{self.label}: a final record, got status 'running'")
        self.stop_tick()
        notes: list[str] = []
        try:
            with self.lock:
                self._final = True
                have = _status_at(self.json_path)
                if have is not None and have != "running":
                    raise FileExistsError(f"{self.json_path}: {self.label} has a final {have} record already")
                write_json_zst(self.json_path, dict(final))
                self.final_written = True
            maybe_die("after_final_local")
            drain_stderr()
            self._write_log()
            if self.upload:
                assert self.store.remote is not None
                try:
                    self.store.remote.put_file(self.json_key, self.json_path)
                    self.store.remote.put_file(self.log_key, self.log_path)
                    maybe_die("after_final_put")
                    self.store.remote.delete(self.marker_key)
                    if self.stop_seen is not None:  # D572: its stop request (the one other runtime delete)
                        self.store.remote.delete(self.stop_key)
                except Exception as exc:  # noqa: BLE001 — the record is local; recovery pushes it
                    notes.append(f"{self.label}: final upload failed ({_s3_error(exc)}); the final "
                                 "record is local and the next uploading job (or `stack store push`) uploads it")
            self.marker_path.unlink(missing_ok=True)
            self.stop_path.unlink(missing_ok=True)
        finally:
            self._leave()
        return notes

    def _leave(self) -> None:
        with _alock:
            if self in _active:
                _active.remove(self)
            if not _active:  # D570: the drain lasts until the process's outermost job ends
                clear_drain()

    def abandon(self) -> None:
        """Stop the tick and leave the active stack without a final record (a start that failed
        before its record existed)."""
        self.stop_tick()
        self._leave()


def _status_at(path: Path) -> str | None:
    try:
        raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
    except FileNotFoundError:
        return None
    data = json.loads(raw)
    return str(data.get("status")) if isinstance(data, dict) else None


def _spend(run_id: str) -> dict[str, Any] | None:
    """The run's spend so far (B1's :func:`stack.llm.ledger.run_spend`), ``None`` when the ledger
    module is not loaded (no ledger opened)."""
    ledger = sys.modules.get("stack.llm.ledger")
    return None if ledger is None else ledger.run_spend(run_id)


class RunJob(Job):
    """A run's job over its :class:`~stack.runs.RunRecord` (``build``: the build directory name of
    a build's run). The runner changes :attr:`record` under :attr:`lock`; each snapshot stamps
    ``heartbeat_at`` and ``spend``."""

    def __init__(self, store_root: str | Path, record: RunRecord, build: str | None = None, *,
                 upload: bool | None = None, interval: dt.timedelta | None = None,
                 clock: Callable[[], dt.datetime] | None = None) -> None:
        store = open_store(store_root)
        record.upload = default_upload(store, upload)
        self.record = record
        self.build = build
        path = run_record_path(store.root, record, build)
        stem = path.name.removesuffix(JSON_SUFFIX)
        super().__init__(store, json_path=path, log_path=path.with_name(stem + LOG_SUFFIX),
                         marker_path=path.with_name(stem + MARKER_SUFFIX), upload=record.upload,
                         snapshot=self._snapshot, label=f"run {record.run_id}", interval=interval, clock=clock)

    def _snapshot(self) -> dict[str, Any]:
        self.record.heartbeat_at = self.clock()
        spend = _spend(self.record.run_id)
        if spend is not None:
            self.record.spend = spend
        return self.record.to_json()

    def set_inflight(self, keys: list[str], cap: int) -> None:
        with self.lock:
            self.record.inflight = keys[:cap]
            dropped = len(keys) - len(self.record.inflight)
            if dropped:
                self.record.counts["inflight_dropped"] = dropped
            else:
                self.record.counts.pop("inflight_dropped", None)
            self.save()

    def set_substage(self, name: str, *, ran: bool, scope_complete: bool) -> None:
        with self.lock:
            self.record.substages[name] = {"ran": ran, "scope_complete": scope_complete}
            self.save()

    def update_counts(self, counts: Mapping[str, int]) -> None:
        with self.lock:
            self.record.counts.update(counts)

    def tolerant(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Call a local-saving setter; an ``OSError`` is counted and logged, not raised (the change
        stays in :attr:`record`; the next tick writes it)."""
        try:
            fn(*args, **kwargs)
        except OSError as exc:
            self.tick_failures += 1
            say(f"warning: {self.label}: record save failed ({exc!r}); continuing")

    def finish_record(self) -> list[str]:
        """:meth:`finish` with :attr:`record` (final)."""
        with self.lock:
            final = self.record.to_json()
        return self.finish(final)


class BuildJob(Job):
    """A build's job over its record dict (``builds/<name>/build.json.zst``)."""

    def __init__(self, store_root: str | Path, name: str, record: dict[str, Any], *, upload: bool | None = None,
                 interval: dt.timedelta | None = None, clock: Callable[[], dt.datetime] | None = None) -> None:
        store = open_store(store_root)
        record["upload"] = default_upload(store, upload)
        self._view = _copy(record)
        layout = StoreLayout(store.root)
        super().__init__(store, json_path=layout.build_json(name), log_path=layout.build_log(name),
                         marker_path=layout.build_marker(name), upload=record["upload"],
                         snapshot=lambda: _copy(self._view),
                         label=f"build {record['build_id']}", interval=interval, clock=clock)

    def set_record(self, record: Mapping[str, Any]) -> None:
        """What the next tick writes: a copy of the build's ``running`` record (the orchestrator
        calls it after each run; the tick never reads the live dict)."""
        view = _copy(record)
        with self.lock:
            self._view = view


def _copy(record: Mapping[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(record, default=str))
    assert isinstance(out, dict)
    return out


# ---------------------------------------------------------------- start of a job: refresh, recover
def prepare(store_root: str | Path, upload: bool) -> StoreIndex:
    """What an uploading or no-upload job does before pinning anything (§6.2, D353): refresh the
    index (``uploaded_only`` = ``upload``: an uploading job ignores records not on S3) and, when
    uploading, :func:`recover`; refreshed again when recovery pushed something. A transient S3
    failure (:func:`stack.store.s3config.is_s3_transient`) raises :class:`S3TransientError`
    (redacted; no record exists yet: a build ends ``failed[s3_transient]``, S25-47)."""
    try:
        idx = store_index(store_root, refresh=True, uploaded_only=upload)
        if upload and idx.store.remote is not None and recover(idx.store, idx):
            idx = store_index(store_root, refresh=True)
    except Exception as exc:
        if is_s3_transient(exc) and not isinstance(exc, S3TransientError):
            raise S3TransientError(f"prepare (index refresh, recovery): {redact_error(exc)}") from None
        raise
    return idx


def _local_records(root: Path) -> list[tuple[str, dict[str, Any]]]:
    """``(key, record)`` of every local build and run json."""
    out = []
    for pat in (f"{BUILDS_DIR}/*/{RUNS}/*{JSON_SUFFIX}", f"{RUNS}/*{JSON_SUFFIX}", f"{BUILDS_DIR}/*/{BUILD_JSON}"):
        for p in sorted(root.glob(pat)):
            if p.name.startswith(".") or not p.is_file():
                continue
            try:
                data = read_json_zst(p)
            except (OSError, ValueError, zstandard.ZstdError):
                continue  # the index reports an unreadable record
            if isinstance(data, dict):
                out.append((p.relative_to(root).as_posix(), data))
    return out


def marker_key(json_key: str) -> str:
    """The marker key beside a record key."""
    if json_key.endswith("/" + BUILD_JSON):
        return json_key.removesuffix(BUILD_JSON) + BUILD_MARKER
    return json_key.removesuffix(JSON_SUFFIX) + MARKER_SUFFIX


def log_key(json_key: str) -> str:
    if json_key.endswith("/" + BUILD_JSON):
        return json_key.removesuffix(BUILD_JSON) + BUILD_LOG
    return json_key.removesuffix(JSON_SUFFIX) + LOG_SUFFIX


def _publish_order(items: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Runs before builds (a build record names its runs), each by ``finished_at`` then id."""
    def k(item: tuple[str, dict[str, Any]]) -> tuple[int, str, str]:
        key, rec = item
        return (1 if key.endswith("/" + BUILD_JSON) else 0, str(rec.get("finished_at") or ""),
                str(rec.get("run_id") or rec.get("build_id") or ""))
    return sorted(items, key=k)


def recover(store: Store, idx: StoreIndex | None = None) -> list[str]:
    """§6.2 recovery at an uploading job's start: every local ``upload: true`` record that is final
    while S3 lacks its json or still holds its marker (the json there is then the earlier
    ``running`` one, or the log or the marker DELETE is missing) is pushed in publish order
    (:func:`push_record`). A local ``running`` record is left alone (a dead job). Returns the
    record keys pushed. A record that cannot be pushed (a named data file is neither local nor on
    S3) is a warning."""
    remote = store.remote
    if remote is None:
        return []
    objs = idx.remote_objects() if idx is not None else None
    have = set(objs) if objs is not None else {o.key for p in (f"{BUILDS_DIR}/", f"{RUNS}/")
                                                for o in remote.list(p)}
    todo = [(key, rec) for key, rec in _local_records(store.root)
            if rec.get("upload") is True and rec.get("status") not in (None, "running")
            and (key not in have or marker_key(key) in have)]
    pushed = []
    data: set[str] | None = None
    for key, rec in _publish_order(todo):
        if data is None:
            data = {o.key for o in remote.list(f"{DATA}/")}
        try:
            push_record(store, key, rec, data)
        except PushError as exc:
            say(f"warning: recovery: {exc}")
            continue
        say(f"recovery: pushed {key} (a final record S3 did not have)")
        pushed.append(key)
    return pushed


def _release_tag(path: str) -> str | None:
    parts = path.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == RELEASES else None


def push_release_dir(store: Store, tag: str) -> None:
    """PUT ``releases/<tag>/``: every file, ``release.json`` last (§5)."""
    if store.remote is None:
        return
    base = store.root / RELEASES / tag
    files = sorted(p for p in base.rglob("*") if p.is_file() and p.name != RELEASE_JSON)
    for p in [*files, base / RELEASE_JSON]:
        store.push(store.key(p), create_only=True)


def push_record(store: Store, key: str, rec: Mapping[str, Any], remote_data: set[str] | None = None) -> None:
    """PUT one finished record in publish order: the data files its ``outputs`` name that S3 lacks
    (a release directory with ``release.json`` last), then the json, then the log (when local),
    then DELETE its marker. :class:`PushError` before any PUT when a named data file is neither
    local nor on S3."""
    remote = store.remote
    if remote is None:
        return
    if remote_data is None:
        remote_data = {o.key for o in remote.list(f"{DATA}/")}
    if rec.get("status") == "ok":
        recheck_outputs(store, key, rec)
    data_puts: list[str] = []
    releases: list[str] = []
    for out in (rec.get("outputs") or {}).values():
        path = str(out["path"])
        tag = _release_tag(path)
        if tag is not None:
            if remote.head(f"{RELEASES}/{tag}/{RELEASE_JSON}") is None:
                if not (store.root / RELEASES / tag / RELEASE_JSON).is_file():
                    raise PushError(f"{key}: release {tag} is neither local nor on S3")
                releases.append(tag)
            continue
        if path in remote_data:
            continue
        if not store.path(path).is_file():
            raise PushError(f"{key}: output {path} is neither local nor on S3; not pushed")
        data_puts.append(path)
    for path in data_puts:
        store.push(path, create_only=True)
        remote_data.add(path)
    for tag in releases:
        push_release_dir(store, tag)
    store.push(key)
    if store.path(log_key(key)).is_file():
        store.push(log_key(key))
    remote.delete(marker_key(key))


def recheck_outputs(store: Store, key: str, rec: Mapping[str, Any]) -> None:
    """The publish rule again before a finished ``ok`` record goes up late (recovery, ``push``;
    D372), against the index of uploaded records (D353): per series output, the
    directory's last uploaded file must be the output's ``after`` and sort before it, unless it
    is this run's own output (committed already). :class:`PushError` otherwise."""
    from stack.store.chain import dir_files
    from stack.store.layout import parse_data_key

    for out in (rec.get("outputs") or {}).values():
        path = str(out["path"])
        if not path.startswith(f"{DATA}/") or not out.get("series"):
            continue
        f = parse_data_key(path)
        files = dir_files(store.root, f.dir)
        last = files[-1] if files else None
        if last is not None and last.run_id == rec.get("run_id"):
            continue
        last_name = last.name if last is not None else ""
        if last_name != out.get("after", ""):
            raise PushError(f"{key}: {f.dir}/ moved on S3 (last file {last_name or 'none'}, the run pinned "
                            f"{out.get('after') or 'an empty directory'}); {f.name} is not published (head_moved)")
        if last_name and f.name <= last_name:
            raise PushError(f"{key}: {f.name} does not sort after {f.dir}/{last_name} on S3 (name_order)")


def _s3_error(exc: BaseException) -> str:
    """An exception for a record, a log or a warning: a provider (botocore) exception as
    :func:`stack.store.s3config.redact_error` gives it (class and S3 code; its message can name
    the endpoint URL, the bucket and the key), any other as ``<Class>: <message>``."""
    if type(exc).__module__.split(".")[0] in ("botocore", "boto3", "s3transfer"):
        return redact_error(exc)
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- stack store push
def _remote_status(store: Store, key: str) -> str | None:
    """The ``status`` of the json at ``key`` on S3; ``None`` when absent."""
    assert store.remote is not None
    try:
        raw = store.remote.get_bytes(key)
    except KeyError:
        return None
    data = json.loads(zstandard.ZstdDecompressor().decompress(raw))
    return str(data.get("status")) if isinstance(data, dict) else None


def check_push(store_root: str | Path) -> list[str]:
    """``stack store push --check`` (§7.5): one line per local job that discarding this machine
    would lose: a local ``upload: true`` record whose S3 copy is absent or of another status
    (GET and compare, not key presence), and a local ``upload: false`` job not on S3 with its
    status. Empty when nothing would be lost. A local-only store has no S3 to compare with:
    refused (:class:`PushError`)."""
    store = open_store(store_root)
    if store.remote is None:
        raise PushError(f"push --check: {store.root} is a local-only store (no S3 remote); nothing is pushed from it")
    out = []
    for key, rec in _local_records(store.root):
        status = rec.get("status")
        there = _remote_status(store, key)
        if there == status:
            continue
        what = "upload" if rec.get("upload") is True else "no-upload"
        out.append(f"{key}: local {status} ({what} job), S3 {there or 'absent'}")
    return out


def request_stop(store_root: str | Path, *, build: str | None = None, run: str | None = None,
                 mode: str = "drain", wait_s: float | None = None) -> list[tuple[str, str]]:
    """``stack stop`` (D572; module docstring, Drain): write a stop request beside each target's
    marker: ``run`` (a run id or stem, standalone or a build's), ``build`` (a build id or directory
    name), neither = every live build and live standalone run. The local file always; on a store
    with an S3 remote also PUT at its key when the job uploads (a job elsewhere reads S3). A target
    that is finished, dead or unknown raises :class:`PushError` before anything is written; none live
    too. Returns ``(job label, stop key)`` per request. The job reads it at its next tick."""
    if build is not None and run is not None:
        raise PushError("stop: --build or --run, not both")
    body = stop_request_bytes(mode, wait_s)
    store = open_store(store_root)
    idx = store_index(store.root, refresh=True)
    targets: list[tuple[str, str, str, bool, dict[str, Any] | None]] = []  # label, key, liveness, upload, status
    for b in idx.builds:
        if (build is None and run is None) or build in (b.build_id, b.name):
            targets.append((f"build {b.build_id}", b.key, b.liveness, b.record.get("upload") is not False,
                            {"status": b.status}))
    for r in idx.runs:
        if (build is None and run is None and r.build is None) or run in (r.run_id, r.stem):
            targets.append((f"run {r.run_id}", r.key, r.liveness, r.record.upload is not False,
                            {"status": r.record.status}))
    if build is None and run is None:
        targets = [t for t in targets if t[2] == "live"]
        if not targets:
            raise PushError("stop: no live build or run in this store (`stack status` lists the jobs)")
    elif not targets:
        raise PushError(f"stop: no {'build' if build is not None else 'run'} named "
                        f"{build if build is not None else run!r} in this store")
    for label, key, live, _, st in targets:
        if st and st.get("status") != "running":
            raise PushError(f"stop: {label} already ended ({st.get('status')}); nothing to stop")
        if live != "live":
            raise PushError(f"stop: {label} is {live}: no tick reads a stop request; a dead job is "
                            "finalised by recovery (`stack status`)")
    out = []
    for label, key, _, upload, _ in targets:
        mk = marker_key(key)
        sk = mk.removesuffix(BUILD_MARKER) + BUILD_STOP if mk.endswith(BUILD_MARKER) else \
            mk.removesuffix(MARKER_SUFFIX) + STOP_SUFFIX
        _write_atomic(store.root / sk, body)
        if store.remote is not None and upload:
            try:
                store.remote.put_bytes(sk, body)
            except Exception as exc:
                raise PushError(f"stop: {label}: the request is local, its S3 PUT failed ({_s3_error(exc)}); "
                                "a job on another machine will not see it") from None
        out.append((label, sk))
    return out


def _find_job(store_root: Path, ref: str) -> tuple[str, dict[str, Any]]:
    """The local record of a run or build named by id, stem or directory name. A build id names
    the build record only (each of its run records carries it too), a run id a run record only."""
    hits = []
    for key, rec in _local_records(store_root):
        is_build = key.endswith("/" + BUILD_JSON)
        name = key.rsplit("/", 2)[-2] if is_build else key.rsplit("/", 1)[-1].removesuffix(JSON_SUFFIX)
        if ref in (rec.get("build_id") if is_build else rec.get("run_id"), name):
            hits.append((key, rec))
    if not hits:
        raise PushError(f"push: no local run or build record named {ref!r}")
    if len(hits) > 1:
        raise PushError(f"push: {ref!r} names {len(hits)} records: {[k for k, _ in hits]}")
    return hits[0]


def _push_run(store: Store, key: str, rec: dict[str, Any]) -> None:
    """Push one finished run (refusals before any PUT): its inputs must be named by ``ok``
    records on S3, and each series output must still pass the publish rule there
    (:func:`recheck_outputs` in :func:`push_record`, against a fresh index of uploaded records)."""
    if rec.get("status") == "running":
        raise PushError(f"{key}: still running (or dead); only a finished job is pushed")
    idx = store_index(store.root, refresh=True, uploaded_only=True)
    named = {o["file_id"] for _, _, o in idx.outputs()}
    refs = [*(rec.get("inputs") or {}).values(), *(rec.get("prior") or [])]
    missing = sorted({str(r) for r in refs if r not in (None, NONE) and r not in named})
    if missing:
        raise PushError(f"{key}: inputs {missing} are not named by ok records on S3; push their jobs first")
    push_record(store, key, rec)


def push_job(store_root: str | Path, ref: str) -> list[str]:
    """``stack store push <job>`` (§6.1): upload a finished local job (a run, or a build with its
    runs first, in start order) in publish order. Refuses (:class:`PushError`, or
    a moved directory, :func:`recheck_outputs`) before the PUTs of the run it
    stops at. Returns the record keys pushed; a record whose S3 copy already has its status is
    skipped."""
    store = open_store(store_root)
    if store.remote is None:
        raise PushError(f"push: {store.root} is a local-only store (no S3 remote)")
    key, rec = _find_job(store.root, ref)
    if rec.get("status") == "running":
        raise PushError(f"{key}: still running (or dead); only a finished job is pushed")
    jobs = [(key, rec)]
    if key.endswith("/" + BUILD_JSON):
        base = key.removesuffix(BUILD_JSON) + f"{RUNS}/"
        runs = [(k, r) for k, r in _local_records(store.root) if k.startswith(base)]
        runs.sort(key=lambda kr: (str(kr[1].get("started_at")), str(kr[1].get("run_id"))))
        jobs = [*runs, (key, rec)]
    pushed = []
    for k, r in jobs:
        if _remote_status(store, k) == r.get("status"):
            continue
        if k.endswith("/" + BUILD_JSON):
            push_record(store, k, r)
        else:
            _push_run(store, k, r)
        pushed.append(k)
    return pushed


__all__ = [
    "BUILD_STOP",
    "CRASH_ENV",
    "CRASH_POINTS",
    "DRAIN_WAIT_S",
    "SELF_CANCEL_AFTER",
    "SIGTERM_REPEAT_S",
    "STOP_MODES",
    "STOP_SUFFIX",
    "TICK",
    "BuildJob",
    "Drain",
    "Job",
    "JobCancelled",
    "JobStartError",
    "PushError",
    "RunJob",
    "capture_stderr",
    "check_push",
    "clear_drain",
    "crash_point",
    "current_job",
    "default_upload",
    "drain_state",
    "drain_stderr",
    "hard_cancel",
    "hard_cancel_requested",
    "job_active",
    "maybe_die",
    "own_line",
    "parse_stop_request",
    "prepare",
    "push_job",
    "push_record",
    "push_release_dir",
    "recheck_outputs",
    "recover",
    "request_drain",
    "request_stop",
    "say",
    "stop_path",
    "stop_request_bytes",
]

