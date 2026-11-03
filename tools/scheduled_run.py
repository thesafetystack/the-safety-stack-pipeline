"""The scheduled production cycle against an S3 store, for a transient container (docs/DEPLOY-railway.md).

    python tools/scheduled_run.py cycle [--dry-run | --test]        # collect -> ... -> fold.build -> release -> HF
    python tools/scheduled_run.py upload --release-id ID [--dry-run] [--check-hf]   # re-upload a staged release
    python tools/scheduled_run.py unlock --holder ID                # remove a stale cycle lock (after checking)
    python tools/scheduled_run.py pause [--note TEXT] | resume       # hold.json: drain the running cycle, refuse new ones
    python tools/scheduled_run.py report [--cycle ID]               # print the last (or one) cycle summary
    python tools/scheduled_run.py watchdog                          # mail ERROR when the last cycle is stale or ERROR

``cycle`` is the sequence the production chains once ran by hand: the OpenAlex re-sweep when due (weekly
by default; a failed one is a WARN and the cycle goes on, D789), the standalone S2 collect and arXiv sweep, build 1 (collectors + gather), the standalone s2-lookup, then
one-pass builds of the plan until one ends ``ok`` (at most ``STACK_SCHED_MAX_BUILDS``; a build short only on
collectors or on acquire's retry tail is releasable, a WARN: :func:`classify_build`, the tail at most
``STACK_SCHED_TAIL_MAX_PCT`` of the documents acquire covers (:func:`tail_share`); after an acquire halt no further
build runs, and a cap or stop before the catch-up build still releases the tail build); on an ``ok`` (or
releasable) build it exports
both dataset variants (``stack export --config dataset-public|dataset-internal``), checks them, stages them under the
ops prefix and uploads them to their Hugging Face repos (every successful weekly run publishes). The summary's ``release.quality`` (:func:`release_quality_summary`) carries the release's judge-eval
coverage and triage's agreement with the screen from its ``release.json`` (D728). The upload refuses, and the cycle fails and alerts, when the internal variant's repo is not private; it
never changes a repo's visibility (the public repo is uploaded to whatever its visibility). Every ``stack`` command
is a child process of this one; nothing here writes the store itself.

``--test`` (or ``STACK_SCHED_TEST=1``): the same cycle against the configured store (its runs are real and publish to
the store; the next cycle carries their work), with test caps unless set explicitly (1 build after build 1, $20,
6 h, no OpenAlex re-sweep), the release exported and checked locally and staged under ``test/``, no Hugging Face
call, summaries under ``test/cycles/`` (``cycles/latest.json`` untouched), alerts sent with ``[test]`` in the
subject. ``--dry-run`` is the mode that writes nothing.

Email (:func:`notify_cycle`): one summary after every cycle, subject ``OK|WARN|ERROR: …`` by the worst finding
(docs/DEPLOY-railway.md, "Email, severity, balances, watchdog"), and an ERROR mail on a crash of this process, through ``STACK_SCHED_ALERT`` = ``resend``
(Resend's HTTP API, ``RESEND_API_KEY``) or ``smtp`` (``STACK_SCHED_SMTP_*``), to ``STACK_SCHED_ALERT_TO``. A
container killed from outside (out of memory, SIGKILL) cannot report itself: ``watchdog`` (a second cron service)
mails ERROR when the last production summary is missing, older than 7 days or ERROR.

Memory log (D783): while a cycle runs, ``tools/memlog.py`` (a separate process, :func:`start_memlog`) prints a
``[mem]`` line every ``STACK_SCHED_MEMLOG_S`` (180 s; 0: none): the cgroup's memory and the three largest processes.
This process reads them from a pipe and prints them, so they are in Railway's logs once and in ``log.txt`` (D784),
but not in the emails' log tails.

Ops prefix (``STACK_SCHED_OPS_URI``, default ``s3://<bucket>/<store prefix>-ops``, outside the store so no ``stack``
verb lists it): ``lock.json`` (create-only, one cycle at a time), ``hold.json`` (the operator's pause),
``cycles/<cycle_id>/summary.json`` and ``log.txt``,
``cycles/latest.json``, ``releases/<id>/<cycle_id>/<variant>/...`` with ``releases/<id>/manifest.json`` (create-only)
written last, and ``releases/<id>/uploaded-<variant>.json``.

``--dry-run`` reads only: S3 through a read-only client (writes refused before they are signed), ``stack status``,
``stack build --dry-run``; it calls no LLM and no Hugging Face API (``--check-hf`` on ``upload`` reads the two
repos' settings). It prints what the cycle would do and exits 4 when the cycle would refuse to start.

Exit codes: 0 ok (an ``ok`` or releasable build; its release staged and uploaded unless ``STACK_SCHED_RELEASE=off``), 1 failed,
halted or the upload refused,
3 incomplete (no releasable build within the caps; the next cycle resumes), 4 refused to start (lock held, a live job,
a preflight problem). Settings (env, names only; values are never printed): see :class:`Settings`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stack.store.backend import BackendKeyError, PreconditionFailed, S3Backend  # noqa: E402
from stack.store.remote import STORE_S3_FALLBACK, mask_s3_text, parse_store_uri, store_config  # noqa: E402
from stack.store.s3config import ENV_FILE_ENV, env_file_values, redact_error  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_INCOMPLETE, EXIT_REFUSED = 0, 1, 3, 4
#: Keys the code reads from the environment, else by name from the STACK_ENV_FILE file (D160).
#: The LLM keys come from the plan (:func:`llm_keys`), not from this list. An S3 setting is also accepted under its
#: older name (``STORE_S3_FALLBACK``, D726).
REQUIRED_KEYS = ("STACK_S3_ACCESS_KEY_ID", "STACK_S3_SECRET_ACCESS_KEY", "STACK_S3_ENDPOINT_URL", "STACK_MAILTO")
#: The llm.yaml role a stage's prompt pin uses when it names none (workflows/*: triage/ask.py, collect/links.py,
#: annotate/{screen,affiliation,model_roles}); a pin's own ``role`` wins.
DEFAULT_ROLES = {"triage": ("classify",), "collect.links": ("classify",),
                 "annotate.screen": ("screen", "screen_adjudicate"), "annotate.affiliation": ("extract_main",),
                 "annotate.model_roles": ("extract_main",)}
OPTIONAL_KEYS = ("OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "HF_TOKEN")
#: Collector cap/sampling variables make a build refuse (D553); test hooks must never reach a production run;
#: STACK_OPENALEX_FULL and STACK_PLAN are set by this script per step, never globally.
FORBIDDEN_ENV = re.compile(r"^(STACK_[A-Z0-9_]*(_LIMIT|_FRACTION|_MAX_QUERIES)|STACK_WORKFLOWS_ROOT|STACK_LLM_TRANSPORT|"
                           r"STACK_CRASH_AT|STACK_CONVERT_(ABORT|HANG|REPORT)|STACK_OPENALEX_FULL|STACK_PLAN)$")
#: Build error classes worth one retry of the build (an S3 hiccup longer than the client's retries).
TRANSIENT = {"s3_transient", "tick_failed"}
#: Incomplete reasons expected while a weekly cycle catches up (chain-r6's OKRE, plus `budget`/`limit` on annotate
#: lines: the cycle's own cap lowers them with --max-usd). Another reason (e.g. `unspecified`) is a WARN and the next
#: build retries it; only the last build's state decides ERROR (docs/DEPLOY-railway.md, "Email, severity, balances, watchdog").
TOLERATED = {
    "acquire": {"time", "pending", "halted"},
    "triage": {"budget"},
    "annotate.screen": {"tier2_allowance", "budget", "limit"},
    "annotate.affiliation": {"budget", "limit"},
    "annotate.technique_staged": {"budget", "limit"},
    "annotate.model_roles": {"budget", "limit"},
    "collect.cards": {"windows_failed", "limit"},
    "collect.forums": {"windows_failed", "halted", "limit"},
}
#: Lines that need another build when incomplete (the others are collectors whose shortfall the next cycle retries).
NEEDS_BUILD = ("acquire", "triage", "annotate.")
#: Acquire's retry tail: a build short on acquire only for these reasons (and maybe on collectors) is releasable
#: (verdict ``tail``): its corpus holds the documents with text (fold filter ``has_text``), every later line ran on
#: them, and the documents still due enter a later build's corpus when fetched. ``halted`` (a lane-wide provider
#: refusal, e.g. Unpaywall's 429) ends the cycle's builds; ``time``/``pending`` alone let another build go on. Released
#: only while the tail is at most ``Settings.tail_max_pct`` of the documents acquire covers (:func:`tail_share`).
ACQUIRE_TAIL = {"halted", "time", "pending"}
VARIANTS = ("public", "internal")
#: The operator's pause on the ops prefix (``pause``/``resume``).
HOLD_KEY = "hold.json"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def env_bool_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


@dataclass
class Settings:
    """Everything the cycle reads from the environment (``STACK_SCHED_*`` are this script's own)."""

    store: str                      # STACK_STORE, s3://<bucket>/<prefix> (the bucket comes from Railway variables)
    plan: str                       # STACK_SCHED_PLAN
    ops_uri: str                    # STACK_SCHED_OPS_URI
    max_usd: float                  # STACK_SCHED_MAX_USD: spend cap of one cycle, all jobs it starts
    max_wall_h: float               # STACK_SCHED_MAX_WALL_H: wall cap; drained, then cancelled
    max_builds: int                 # STACK_SCHED_MAX_BUILDS: one-pass builds after build 1
    arxiv_max_min: float            # STACK_SCHED_ARXIV_MAX_MIN: the standalone arXiv sweep is drained after this
    openalex_full: str              # STACK_SCHED_OPENALEX_FULL: auto | 1 | 0
    openalex_full_days: float       # STACK_SCHED_OPENALEX_FULL_DAYS: auto re-sweeps when the last is older
    expect_plan_digest: str         # STACK_SCHED_EXPECT_PLAN_DIGEST: refuse another plan revision ('' = any)
    min_free_gb: float              # STACK_SCHED_MIN_FREE_GB: free disk under the mirror at start
    release: str                    # STACK_SCHED_RELEASE: export | off
    upload: bool                    # STACK_SCHED_UPLOAD=0 stages the release without uploading it (default 1)
    test: bool                      # STACK_SCHED_TEST=1 / --test (module docstring)
    allow_incomplete_release: bool  # STACK_SCHED_RELEASE_ALLOW_INCOMPLETE (default 1): release a build short only on
    #                                 collectors or on acquire's retry tail
    tail_max_pct: float             # STACK_SCHED_TAIL_MAX_PCT (default 5): the largest retry tail released, in percent
    #                                 of the documents acquire covers (:func:`tail_share`); above it, no release
    work: Path                      # STACK_SCHED_WORK: scratch for exports and uploads (ephemeral disk)
    watch_s: float = 600            # STACK_SCHED_WATCH_S: spend/wall check period
    heartbeat_s: float = 300        # lock heartbeat period
    drain_grace_s: float = 3600     # STACK_SCHED_DRAIN_GRACE_S: after a drain, cancel; after as long again, exit
    #                                 (D781: covers a drained build's final verify, 29-38 min on Railway)
    memlog_s: float = 180           # STACK_SCHED_MEMLOG_S: the memory log's period (D783); 0 = no memory log
    bucket: str = field(default="", repr=False)
    prefix: str = ""

    @classmethod
    def from_env(cls, *, test: bool = False) -> Settings:
        test = test or os.environ.get("STACK_SCHED_TEST", "") == "1"
        store = os.environ.get("STACK_STORE", "").strip()
        if not store.startswith("s3://"):
            raise SystemExit("error: STACK_STORE must be s3://<bucket>/<prefix> (set it from the Railway variables)")
        bucket, prefix = parse_store_uri(store)
        s = cls(
            store=store,
            plan=os.environ.get("STACK_SCHED_PLAN", "configs/plans/batch-2026-10a.yaml"),
            ops_uri=os.environ.get("STACK_SCHED_OPS_URI", "").strip() or f"s3://{bucket}/{prefix}-ops",
            max_usd=env_bool_float("STACK_SCHED_MAX_USD", 20.0 if test else 100.0),
            max_wall_h=env_bool_float("STACK_SCHED_MAX_WALL_H", 6.0 if test else 20.0),
            max_builds=int(env_bool_float("STACK_SCHED_MAX_BUILDS", 1 if test else 5)),
            arxiv_max_min=env_bool_float("STACK_SCHED_ARXIV_MAX_MIN", 45.0),
            openalex_full=os.environ.get("STACK_SCHED_OPENALEX_FULL", "").strip() or ("0" if test else "auto"),
            openalex_full_days=env_bool_float("STACK_SCHED_OPENALEX_FULL_DAYS", 6.0),
            expect_plan_digest=os.environ.get("STACK_SCHED_EXPECT_PLAN_DIGEST", "").strip(),
            min_free_gb=env_bool_float("STACK_SCHED_MIN_FREE_GB", 30.0),
            release=os.environ.get("STACK_SCHED_RELEASE", "export").strip() or "export",
            upload=os.environ.get("STACK_SCHED_UPLOAD", "1").strip() != "0",
            test=test,
            allow_incomplete_release=os.environ.get("STACK_SCHED_RELEASE_ALLOW_INCOMPLETE", "1") != "0",
            tail_max_pct=env_bool_float("STACK_SCHED_TAIL_MAX_PCT", 5.0),
            work=Path(os.environ.get("STACK_SCHED_WORK", "") or tempfile.gettempdir()) / "stack-sched",
            watch_s=env_bool_float("STACK_SCHED_WATCH_S", 600.0),
            drain_grace_s=env_bool_float("STACK_SCHED_DRAIN_GRACE_S", 3600.0),
            memlog_s=env_bool_float("STACK_SCHED_MEMLOG_S", 180.0),
            bucket=bucket, prefix=prefix)
        if s.openalex_full not in ("auto", "0", "1"):
            raise SystemExit("error: STACK_SCHED_OPENALEX_FULL must be auto, 0 or 1")
        if s.release not in ("export", "off"):
            raise SystemExit("error: STACK_SCHED_RELEASE must be export or off")
        if not 0 <= s.tail_max_pct <= 100:
            raise SystemExit("error: STACK_SCHED_TAIL_MAX_PCT must be a percentage, 0 to 100")
        parse_store_uri(s.ops_uri)
        if s.ops_uri.rstrip("/") == store.rstrip("/") or s.ops_uri.startswith(store.rstrip("/") + "/"):
            raise SystemExit("error: STACK_SCHED_OPS_URI must lie outside the store's prefix")
        return s


# ---------------------------------------------------------------- output (bucket masked everywhere)
class Log:
    """Lines to stdout (Railway keeps them 7-30 days) and to memory (written to the ops prefix at the end)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._quiet: set[int] = set()  # indices in ``lines`` of the memory log's lines (D784), left out of ``tail``
        self._lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        line = f"{utcnow():%Y-%m-%dT%H:%M:%SZ} [sched] {mask_s3_text(msg)}"
        with self._lock:
            self.lines.append(line)
        print(line, flush=True)

    def verbatim(self, line: str) -> None:
        """A line of the memory log (D784), already stamped and tagged ``[mem]`` by it: kept and printed as it is,
        and left out of :meth:`tail`."""
        line = mask_s3_text(line)
        with self._lock:
            self._quiet.add(len(self.lines))
            self.lines.append(line)
        print(line, flush=True)

    def tail(self, n: int) -> list[str]:
        """The last ``n`` lines for an email, without the memory log's lines."""
        with self._lock:
            return [ln for i, ln in enumerate(self.lines) if i not in self._quiet][-n:]


log = Log()


def key_present(name: str) -> bool:
    """Whether ``name`` (or, for an S3 store setting, its older name, D726) is set in the environment or, by name,
    in the ``STACK_ENV_FILE`` file (value never read out of this function)."""
    names = (name, STORE_S3_FALLBACK[name]) if name in STORE_S3_FALLBACK else (name,)
    if any(os.environ.get(n, "").strip() for n in names):
        return True
    path = os.environ.get(ENV_FILE_ENV)
    if not path:
        return False
    try:
        vals = env_file_values(path, names)
    except Exception:  # noqa: BLE001 - an unreadable file means "absent" here; store_config reports it properly
        return False
    return any(vals.get(n, "").strip() for n in names)


def key_label(name: str) -> str:
    """``name`` as a message shows it: an S3 store setting with its accepted older name."""
    return f"{name} (or {STORE_S3_FALLBACK[name]})" if name in STORE_S3_FALLBACK else name


def key_value(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    path = os.environ.get(ENV_FILE_ENV)
    return env_file_values(path, (name,)).get(name, "").strip() if path else ""


# ---------------------------------------------------------------- child processes
class Children:
    """The one ``stack`` child at a time, its signals and its output. Children start in their own session, so a
    signal to this process's group reaches them only through :meth:`signal_current` (once, as ``stack`` expects:
    a first SIGTERM drains, a second 5 s or more later cancels)."""

    def __init__(self) -> None:
        self.current: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.stop_reason: str | None = None   # set by a signal or the watchdog: start nothing new
        self.terms = 0

    def signal_current(self, why: str) -> None:
        with self.lock:
            p = self.current
        if p is not None and p.poll() is None:
            self.terms += 1
            log(f"SIGTERM {self.terms} to the running stack process ({why}): "
                + ("drain" if self.terms == 1 else "cancel"))
            p.send_signal(signal.SIGTERM)

    def request_stop(self, why: str) -> None:
        """The first request drains the running child (one SIGTERM); later ones (the watchdog finds the cap still
        reached every ``watch_s``, the lock still lost) change nothing: the cancel is the watchdog's, after the drain grace,
        and a child started after the stop (a tail build's release) is not signalled."""
        if self.stop_reason is None:
            self.stop_reason = why
            log(f"stop requested: {why}; nothing further is started")
            self.signal_current(why)


children = Children()


def mem_note(pid: int | None, cgroup: Path = Path("/sys/fs/cgroup")) -> str:
    """The container's memory for the LIVE line (D782): the cgroup's usage and peak with ``memory.stat``'s anon / file
    split (file is page cache, mostly the mirror, and reclaimable; anon is the processes' own memory), its swap, its
    tasks (``pids.current`` of ``pids.max``, D786), and the running stack child's VmRSS / VmHWM (its peak so far) and
    thread count. Best effort: what cannot be read is left out."""
    def gb(n: int) -> str:
        return f"{n / 2**30:.1f}"
    parts = []
    try:
        st = {k: int(v) for k, v in (ln.split() for ln in (cgroup / "memory.stat").read_text().splitlines())}
        cur = int((cgroup / "memory.current").read_text())
        parts.append(f"cgroup {gb(cur)} GB (anon {gb(st['anon'])}, file {gb(st['file'])}, shmem {gb(st['shmem'])})")
        parts.append(f"peak {gb(int((cgroup / 'memory.peak').read_text()))} GB")
    except (OSError, KeyError, ValueError):
        pass
    try:  # whether the container has swap at all: memory.swap.max is "max", a byte count, or 0
        parts.append(f"swap {gb(int((cgroup / 'memory.swap.current').read_text()))} GB of "
                     f"{(cgroup / 'memory.swap.max').read_text().strip()}")
    except (OSError, ValueError):
        pass
    try:  # tasks (threads and processes) of the container's limit, D786: Railway's pids.max is 1000
        parts.append(f"pids {int((cgroup / 'pids.current').read_text())} of {(cgroup / 'pids.max').read_text().strip()}")
    except (OSError, ValueError):
        pass
    try:
        st = dict(ln.split(":", 1) for ln in Path(f"/proc/{pid}/status").read_text().splitlines()) if pid else {}
        vm = {k: int(st[k].split()[0]) * 1024 for k in ("VmRSS", "VmHWM")}
        parts.append(f"child rss {gb(vm['VmRSS'])} GB, hwm {gb(vm['VmHWM'])} GB, threads {int(st['Threads'])}")
    except (OSError, KeyError, ValueError):
        pass
    return f", mem {'; '.join(parts)}" if parts else ""


MEMLOG = REPO / "tools" / "memlog.py"


@dataclass
class Memlog:
    """The memory log's process and the thread that copies its stdout into :data:`log` (D784)."""
    proc: subprocess.Popen[bytes]
    reader: threading.Thread


def read_memlog(p: subprocess.Popen[bytes]) -> None:
    """Each line of the memory log into :meth:`Log.verbatim` (so to stdout once and to ``log.txt``), until its end.
    A failure here is one WARNING line; the pipe is then closed, so the memory log ends on its next line
    (BrokenPipeError) rather than block on a full pipe."""
    assert p.stdout is not None
    try:
        for raw in p.stdout:
            log.verbatim(raw.decode(errors="replace").rstrip("\r\n"))
    except Exception as exc:  # noqa: BLE001 - telemetry never fails the cycle
        log(f"WARNING: memory log reader failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            p.stdout.close()
        except OSError:
            pass


def start_memlog(interval_s: float) -> Memlog | None:
    """The memory log (D783) as a separate process, its stdout read by a daemon thread (:func:`read_memlog`, D784);
    ``None`` when disabled or when it cannot start (one WARNING line; the cycle goes on without it). Its own session:
    neither a terminal's Ctrl-C nor a signal to this process's group reaches it, and it is not :class:`Children`'s
    child, so the cycle's drain and cancel never signal it (tini, without ``-g``, signals only this process).
    :func:`stop_memlog` ends it; if this process dies first, it exits by itself (its parent changes, or its next line
    meets a closed pipe)."""
    if interval_s <= 0:
        return None
    try:
        p = subprocess.Popen([sys.executable, str(MEMLOG), "--interval", f"{interval_s:g}",
                              "--parent", str(os.getpid())],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, start_new_session=True)
    except Exception as exc:  # noqa: BLE001 - telemetry never fails the cycle
        log(f"WARNING: memory log not started: {type(exc).__name__}: {exc}")
        return None
    reader = threading.Thread(target=read_memlog, args=(p,), name="memlog-reader", daemon=True)
    try:
        reader.start()
    except Exception as exc:  # noqa: BLE001 - without a reader the process would block on a full pipe: end it
        log(f"WARNING: memory log not started: {type(exc).__name__}: {exc}")
        p.kill()
        p.wait()
        return None
    return Memlog(p, reader)


def stop_memlog(m: Memlog | None) -> None:
    """SIGTERM to the memory log and a short wait, then the rest of its lines (the reader, until the pipe's end, at
    most 3 s); an early exit of its own (it crashed) is one WARNING line."""
    if m is None:
        return
    p = m.proc
    try:
        if p.poll() is not None:
            log(f"WARNING: the memory log had exited early (rc={p.returncode})")
        else:
            p.terminate()
            p.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001 - telemetry never fails the cycle; it exits with its parent anyway
        log(f"WARNING: memory log not stopped: {type(exc).__name__}: {exc}")
    m.reader.join(timeout=3)
    if m.reader.is_alive():
        log("WARNING: the memory log's reader did not end (left behind, a daemon thread)")


def stack_cmd() -> list[str]:
    exe = Path(sys.executable).parent / "stack"
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "stack.cli"]


@dataclass
class Result:
    rc: int
    out: str
    tail: list[str]
    wall_s: float
    head: list[str] = field(default_factory=list)  # the first lines of stderr (masked): a build's ``started`` line

    def json_line(self, start: str) -> dict[str, Any] | None:
        for line in reversed(self.out.splitlines()):
            if line.startswith(start):
                try:
                    return dict(json.loads(line))
                except ValueError:
                    return None
        return None

    def json_doc(self) -> dict[str, Any] | None:
        """The JSON document a ``--json`` verb printed: from the first line that starts one and parses to the end."""
        lines = self.out.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("{"):
                try:
                    return dict(json.loads("\n".join(lines[i:])))
                except ValueError:
                    continue
        return None


def run_stack(args: list[str], *, env: dict[str, str] | None = None, track: bool = True, echo: bool = True,
              time_cap_s: float | None = None) -> Result:
    """Run ``stack <args>`` from the repository, echoing its lines (masked) and keeping stdout. ``track``: it is the
    cycle's current child (signals and the watchdog reach it). ``time_cap_s``: one SIGTERM (drain) after that long.
    A ``--json`` stdout document is kept but not echoed: a build's report is 1000+ lines at once, over Railway's
    500 lines/s, and the dropped lines could include real ones."""
    e = {**os.environ, "STACK_PROGRESS": "0", **(env or {})}
    t = time.monotonic()
    p = subprocess.Popen([*stack_cmd(), *args], cwd=REPO, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True, bufsize=1)
    if track:
        with children.lock:
            children.current = p
            children.terms = 0
    out: list[str] = []
    tail: list[str] = []
    head: list[str] = []
    echo_out = echo and "--json" not in args

    def pump(stream: Any, keep: bool) -> None:
        for line in stream:
            line = line.rstrip("\n")
            if keep:
                out.append(line)
            elif len(head) < 20:
                head.append(line)
            tail.append(line)
            del tail[:-200]
            if echo_out if keep else echo:
                print(f"  | {mask_s3_text(line)}", flush=True)

    threads = [threading.Thread(target=pump, args=(p.stdout, True), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, False), daemon=True)]
    for th in threads:
        th.start()
    capped = False
    while p.poll() is None:
        time.sleep(1)
        if time_cap_s is not None and not capped and time.monotonic() - t > time_cap_s:
            capped = True
            log(f"stack {args[0]} {args[1] if len(args) > 1 else ''} over its time cap ({time_cap_s / 60:.0f} min): "
                "one SIGTERM (drain)")
            p.send_signal(signal.SIGTERM)
    for th in threads:
        th.join(timeout=30)
    if track:
        with children.lock:
            children.current = None
    return Result(p.returncode, "\n".join(out), [mask_s3_text(x) for x in tail], time.monotonic() - t,
                  [mask_s3_text(x) for x in head])


class StackFailed(RuntimeError):
    """A ``stack`` call that failed; the message holds only masked output lines (:func:`run_stack`), so it is shown
    as it is rather than redacted to the class name (:func:`error_text`)."""


def error_text(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, StackFailed) else redact_error(exc)


def status_json() -> dict[str, Any]:
    r = run_stack(["status", "--limit", "0", "--json"], track=False, echo=False)
    if r.rc != 0:
        raise StackFailed(f"stack status failed rc={r.rc}: {' | '.join(r.tail[-3:])}")
    d = r.json_doc()
    if d is None:
        raise StackFailed("stack status --json printed no JSON")
    return d


def spend_since(since: str, st: dict[str, Any] | None = None) -> float:
    """USD settled plus open of every job started at or after ``since`` (OPERATE, "Spend in run records")."""
    jobs = [j for j in (st or status_json())["jobs"] if j["started_at"] >= since]
    return float(sum(j["spend_usd_micro"] + j["open_usd_micro"] for j in jobs)) / 1e6


def cap_spend_since(since: str, st: dict[str, Any] | None = None) -> float:
    """What the cycle's spend cap counts (D797): USD settled of every job started at or after ``since``, plus the
    open reservations of those that are not live (a dead job's open amount is part of its spend). A live run's
    reservations are bounded by its own per-run cap (an annotate line's ``--max-usd`` is what was left of the cycle's,
    :func:`max_usd_args`); counting them too books that budget twice, and stopped a cycle at $4.66 settled when an
    annotate run started with $8.00 open across its workers."""
    jobs = [j for j in (st or status_json())["jobs"] if j["started_at"] >= since]
    return float(sum(j["spend_usd_micro"] + (0 if j.get("liveness") == "live" else j["open_usd_micro"])
                     for j in jobs)) / 1e6


def live_open_since(since: str, st: dict[str, Any]) -> float:
    """USD open in live jobs started at or after ``since``: what :func:`cap_spend_since` leaves out (the LIVE line)."""
    return float(sum(j["open_usd_micro"] for j in st["jobs"]
                     if j["started_at"] >= since and j.get("liveness") == "live")) / 1e6


# ---------------------------------------------------------------- the ops prefix: lock, summaries, releases
class Ops:
    """The ops prefix on the store's bucket, through :class:`S3Backend`; ``read_only`` installs the client guard."""

    def __init__(self, uri: str, *, read_only: bool, client: Any = None) -> None:
        cfg = store_config(uri)
        self.read_only = read_only
        self.b = S3Backend(cfg.bucket, cfg.base_path, client=client or cfg.client(read_only=read_only))

    def get_json(self, key: str) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self.b.get(key)))
        except BackendKeyError:
            return None

    def put_bytes(self, key: str, data: bytes, *, create_only: bool = False) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
        try:
            self.b.put_file(key, Path(f.name), create_only=create_only)
        finally:
            os.unlink(f.name)

    def put_json(self, key: str, doc: dict[str, Any], *, create_only: bool = False) -> None:
        self.put_bytes(key, (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode(), create_only=create_only)


@dataclass
class Lock:
    """``lock.json`` on the ops prefix: created with ``If-None-Match: *`` (one holder), rewritten by its holder at
    every heartbeat, deleted at the end. A lock left by a killed container is never broken automatically: the next
    cycle refuses (exit 4) and says how to remove it after checking that nothing runs (``unlock --holder``)."""

    ops: Ops
    holder: str
    doc: dict[str, Any] = field(default_factory=dict)

    KEY = "lock.json"

    def acquire(self, info: dict[str, Any]) -> dict[str, Any] | None:
        """``None`` when acquired, else the current holder's document."""
        now = utcnow().isoformat()
        self.doc = {"holder": self.holder, "started_at": now, "heartbeat_at": now, **info}
        try:
            self.ops.put_json(self.KEY, self.doc, create_only=True)
            return None
        except PreconditionFailed:
            return self.ops.get_json(self.KEY) or {"holder": "?"}

    def heartbeat(self) -> bool:
        cur = self.ops.get_json(self.KEY)
        if cur is None or cur.get("holder") != self.holder:
            log(f"WARNING: lock.json is no longer ours (now {cur.get('holder') if cur else 'absent'}): stopping")
            return False
        self.doc["heartbeat_at"] = utcnow().isoformat()
        self.ops.put_json(self.KEY, self.doc)
        return True

    def ours(self) -> bool:
        """``lock.json`` read again (nothing written): still this holder's (the release's check, D777)."""
        cur = self.ops.get_json(self.KEY)
        if cur is None or cur.get("holder") != self.holder:
            log(f"WARNING: lock.json is no longer ours (now {cur.get('holder') if cur else 'absent'})")
            return False
        return True

    def release(self) -> None:
        cur = self.ops.get_json(self.KEY)
        if cur is not None and cur.get("holder") == self.holder:
            self.ops.b.delete(self.KEY)
        else:
            log(f"WARNING: lock.json not ours at release (now {cur.get('holder') if cur else 'absent'}); left as is")


def lock_age_h(doc: dict[str, Any]) -> float | None:
    try:
        return (utcnow() - dt.datetime.fromisoformat(str(doc["heartbeat_at"]))).total_seconds() / 3600
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------- plan, store state
#: The prompt-pin fields that name a model reference (D722: a reference may carry a provider qualifier,
#: ``anthropic:claude-…``, and then goes to that provider whatever the role's).
PIN_MODEL_FIELDS = ("model", "adjudicate_model", "prepass_model", "select_model")


def llm_keys(prompts: dict[str, dict[str, Any]], llm: Any) -> dict[str, list[str]]:
    """``{api key env var: [the pinned stages whose calls need it]}``: per prompt pin, its ``role`` (else the stage's
    :data:`DEFAULT_ROLES`) and the model references in :data:`PIN_MODEL_FIELDS`, through
    ``LlmConfig.key_envs(role, refs)`` (the providers of the role's model, its fallbacks and the refs). Every pinned
    stage also needs the default provider's key (OpenRouter's): ``stack.llm.open_client`` refuses to start an LLM
    stage without it even when all of its calls go to another provider (D714/D722)."""
    out: dict[str, list[str]] = {}
    for stage, pin in sorted(prompts.items()):
        st = stage.split(".")[0] if stage.startswith("triage.") else stage
        roles = (pin["role"],) if pin.get("role") else DEFAULT_ROLES.get(st, ())
        refs = tuple(str(pin[f]) for f in PIN_MODEL_FIELDS if pin.get(f))
        envs = {llm.provider.api_key_env}
        for r in roles:
            envs |= llm.key_envs(llm.roles[r], refs)
        for env in sorted(envs):
            out.setdefault(env, []).append(stage)
    return out


def plan_keys(path: str) -> dict[str, list[str]]:
    from stack import config
    from stack.plan import load_plan

    return llm_keys(load_plan(REPO / path).prompts, config.load().llm)


def plan_info(path: str) -> tuple[str, str, dict[str, float]]:
    """``(plan_digest, series, {annotate line: its USD cap})``."""
    from stack import config
    from stack.plan import load_plan

    p = load_plan(REPO / path)
    per_run = float(config.load().llm.budget.per_run_usd)
    caps = {s.stage: float(s.budget.max_usd) if s.budget and s.budget.max_usd else per_run
            for s in p.stages if s.stage.startswith("annotate.")}
    if not p.series:
        raise SystemExit(f"error: plan {path} names no series")
    return p.plan_digest, p.series, caps


def last_openalex_full(store: str, series: str) -> dt.datetime | None:
    """Start of the newest ``ok`` collect.openalex run that re-swept from the floor (``counts.full_resweep``), from the
    mirror's run records (``stack status`` refreshed them)."""
    from stack.cli.common import mirror_base
    from stack.store.index import store_index
    from stack.store.remote import resolve_store

    idx = store_index(resolve_store(store, mirror_base=mirror_base()))
    best: dt.datetime | None = None
    for e in idx.runs:
        r = e.record
        if r.stage != "collect.openalex" or r.status != "ok" or not (r.counts or {}).get("full_resweep"):
            continue
        outs = [str(o.get("path", "")) for o in (r.outputs or {}).values()]
        if outs and not any(f"/{series}/" in o for o in outs):
            continue
        t = dt.datetime.fromisoformat(str(r.started_at))
        best = t if best is None or t > best else best
    return best


def free_gb(path: Path) -> float:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 2**30


def preflight(s: Settings, digest: str) -> tuple[list[str], list[str]]:
    """``(refusals, warnings)``: names only, never values."""
    from stack.cli.common import mirror_base

    refuse: list[str] = []
    warn: list[str] = []
    missing = [k for k in REQUIRED_KEYS if not key_present(k)]
    if missing:
        refuse.append(f"missing settings: {', '.join(key_label(k) for k in missing)}")
    for env, stages in plan_keys(s.plan).items():
        if not key_present(env):
            refuse.append(f"missing LLM key {env} (needed by the plan's {', '.join(stages)})")
    absent = [k for k in OPTIONAL_KEYS if not key_present(k)]
    if absent:
        warn.append(f"optional settings absent: {', '.join(absent)}")
    bad = sorted(k for k in os.environ if FORBIDDEN_ENV.match(k))
    if bad:
        refuse.append(f"variables that must not be set for a production cycle: {', '.join(bad)}")
    from stack.stage_runner import StageError, pool_ceiling

    try:  # STACK_POOL_MAX_THREADS (D786): the build child inherits it; a bad value would fail every pool stage
        pool_ceiling()
    except StageError as exc:
        refuse.append(str(exc))
    if s.expect_plan_digest and digest != s.expect_plan_digest:
        refuse.append(f"plan digest {digest[:16]} != STACK_SCHED_EXPECT_PLAN_DIGEST {s.expect_plan_digest[:16]}")
    for mod in ("pymupdf", "pymupdf4llm", "trafilatura"):
        try:
            __import__(mod)
        except ImportError:
            refuse.append(f"convert tool {mod} not installed (uv sync --frozen --group convert-pdf)")
    if shutil.which("pdftotext") is None:
        refuse.append("pdftotext not on PATH (poppler-utils; collect.card-dates-lookup reads card PDFs with it)")
    if shutil.which("git") is None or not (REPO / ".git").exists():
        if not re.fullmatch(r"[0-9a-f]{7,40}", os.environ.get("STACK_CODE_COMMIT", "")):
            (refuse if s.release == "export" else warn).append(
                "no git checkout and no STACK_CODE_COMMIT: the release would name code commit 'unknown'")
    gb = free_gb(mirror_base())
    if gb < s.min_free_gb:
        refuse.append(f"free disk under the mirror {gb:.0f} GB < STACK_SCHED_MIN_FREE_GB {s.min_free_gb:.0f}")
    if s.release == "export" and s.upload and not s.test and not key_present("HF_TOKEN"):
        refuse.append("HF_TOKEN missing: the cycle uploads its release (STACK_SCHED_UPLOAD=0 stages it only)")
    if alert_channel() is None:
        warn.append("no alert channel (STACK_SCHED_ALERT, STACK_SCHED_ALERT_TO): no summary email is sent")
    return refuse, warn


def live_jobs(st: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(live, recent dead/unknown)`` job ids (dead or unknown: started in the last 48 h)."""
    since = (utcnow() - dt.timedelta(hours=48)).isoformat()
    live = [j["id"] for j in st["jobs"] if j.get("liveness") == "live"]
    odd = [f"{j['id']} ({j['liveness']})" for j in st["jobs"]
           if j.get("liveness") in ("dead", "unknown") and j["started_at"] >= since]
    return live, odd


# ---------------------------------------------------------------- the build outcome
def classify_build(rec: dict[str, Any]) -> tuple[str, list[str]]:
    """``(verdict, notes)`` of a finished build record: ``ok``; ``retry`` (a transient failure); ``failed``;
    ``stopped`` (drained; or drained by the cycle's stop, then cancelled: :meth:`Cycle.stopped_record`, D781);
    ``again`` (incomplete on a line another build continues: acquire, triage, annotate);
    ``tail`` (incomplete on acquire only for :data:`ACQUIRE_TAIL` reasons, maybe on collectors too: releasable, WARN);
    ``collectors`` (incomplete only on other lines: released, WARN). A note starting ``unexpected`` names a reason
    outside :data:`TOLERATED` (a WARN)."""
    status = rec.get("status")
    if status == "ok":
        return "ok", []
    if status == "failed":
        ec = rec.get("error_class") or ""
        return ("retry" if ec in TRANSIENT else "failed"), [f"failed[{ec}]: {rec.get('error')}"]
    if status == "cancelled" and rec.get("stopped"):  # the cycle's own stop, drained then cancelled (D781)
        return "stopped", [f"stopped: {rec['stopped']}, then cancelled[{rec.get('error_class')}]"]
    if status != "incomplete":
        return "failed", [f"status {status}[{rec.get('error_class')}]"]
    if rec.get("stopped"):
        return "stopped", [f"stopped: {rec['stopped']}"]
    notes, needs, tail = [], False, False
    for e in rec.get("incomplete") or []:
        st, reasons = e["stage"], set(e.get("reasons") or [])
        odd = sorted(reasons - TOLERATED.get(st, set())) or ([] if reasons else ["none given"])
        notes.append(f"{'unexpected ' if odd else ''}incomplete: {st} ({', '.join(sorted(reasons)) or '-'})")
        if st == "acquire" and reasons and reasons <= ACQUIRE_TAIL:
            tail = True
        elif st.startswith(NEEDS_BUILD):
            needs = True
    return ("again" if needs else "tail" if tail else "collectors"), notes


def acquire_reasons(rec: dict[str, Any]) -> set[str]:
    """The reasons of the build record's ``incomplete`` acquire entry (empty without one)."""
    return {r for e in rec.get("incomplete") or [] if e["stage"] == "acquire" for r in e.get("reasons") or []}


def last_acquire_run(runs: list[dict[str, Any]], run_id: str | None) -> dict[str, Any]:
    """The build's acquire run ``run_id`` (the record's ``incomplete`` entry), else its last acquire run, else ``{}``."""
    acq = [x for x in runs if x.get("stage") == "acquire"]
    return next((x for x in acq if x.get("run_id") == run_id), acq[-1] if acq else {})


def last_run_ok(runs: list[dict[str, Any]], run_id: str | None) -> int:
    """Roles ok (``counts["outcome.ok"]``) in the build's last acquire run: the progress that lets a clock-only tail
    build be followed by another (the last run, not the build's sum: a build's first runs drain the easy part of
    the tail, the last one is what the next build's first run meets; build wtwv0wpnr8hfv: 26, then 12)."""
    try:
        return int((last_acquire_run(runs, run_id).get("counts") or {}).get("outcome.ok") or 0)
    except (TypeError, ValueError):
        return 0


def tail_size(runs: list[dict[str, Any]], run_id: str | None) -> str:
    """The summary's words for acquire's retry tail, from ``stack show runs`` of the build: the last acquire run's
    rows due, roles ok, roles left ``failed_transient`` and roles waiting on a ``retry_after``, the halted lanes, and
    the roles ok over the build's acquire runs."""
    acq = [x for x in runs if x.get("stage") == "acquire"]
    last = last_acquire_run(runs, run_id)
    c = last.get("counts") or {}
    lanes = sorted(k.removeprefix("halted.") for k in c if k.startswith("halted."))
    ok = sum(int((x.get("counts") or {}).get("outcome.ok", 0)) for x in acq)
    return (f"last acquire run {last.get('run_id')}: {c.get('scope.due', '?')} rows due, "
            f"{last_run_ok(runs, run_id)} roles ok, {c.get('outcome.failed_transient', 0)} roles failed_transient, "
            f"{c.get('refresh_pending', 0)} waiting on a retry_after, halted lanes: {', '.join(lanes) or '-'}; {ok} "
            f"roles ok over the build's {len(acq)} acquire runs")


def tail_share(runs: list[dict[str, Any]], run_id: str | None) -> tuple[int, int] | None:
    """``(due, covered)`` of acquire's retry tail, from the build's last acquire run (:func:`last_acquire_run`):
    ``due`` its ``counts["scope.due"]``, the documents (rows) with a fetch role due and fetchable when the run started
    (``workflows.acquire.scope``: ``not_attempted``, or ``retry_after`` passed; the tail that run met, so at most what
    it left); ``covered`` its ``counts["scope.fetch"]``, the documents in acquire's scope that triage lets it fetch,
    fetched already or not (the population the tail is a share of; build wtwv0wpnr8hfv: 677 of 72,935, 0.93%).
    ``None`` when either count is missing or not a number, or ``covered`` is 0: a tail of unknown size, not released."""
    c = last_acquire_run(runs, run_id).get("counts") or {}
    try:
        due, covered = int(c["scope.due"]), int(c["scope.fetch"])
    except (KeyError, TypeError, ValueError):
        return None
    return (due, covered) if covered > 0 else None


def tail_words(share: tuple[int, int] | None, max_pct: float) -> tuple[bool, str]:
    """``(within, words)``: whether the tail (:func:`tail_share`) is at most ``max_pct`` percent of the documents
    acquire covers, and the summary's words for it."""
    if share is None:
        return False, ("of unknown size (the last acquire run's scope.due or scope.fetch is not readable; "
                       f"STACK_SCHED_TAIL_MAX_PCT {max_pct:g})")
    due, covered = share
    pct = 100 * due / covered
    return due * 100 <= max_pct * covered, (f"{due} rows due, {pct:.2f}% of the {covered} rows acquire covers "
                                            f"(scope.fetch; STACK_SCHED_TAIL_MAX_PCT {max_pct:g})")


def max_usd_args(caps: dict[str, float], remaining: float) -> list[str]:
    """``--max-usd`` per annotate line: the lower of its own cap and what the cycle has left (triage's budget is in
    its config digest and cannot be lowered: its $20 per run comes on top)."""
    out: list[str] = []
    for stage, cap in sorted(caps.items()):
        v = min(cap, max(remaining, 0.01))
        if v < cap:
            out += ["--max-usd", f"{stage}={v:.2f}"]
    return out


def scan_for(root: Path, needle: bytes) -> list[str]:
    """Files under ``root`` whose bytes contain ``needle`` (the bucket name must appear in no shipped file)."""
    hits = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            with open(p, "rb") as f:
                prev = b""
                while chunk := f.read(1 << 20):
                    if needle in prev[-len(needle):] + chunk:
                        hits.append(p.relative_to(root).as_posix())
                        break
                    prev = chunk
    return hits


def release_quality_summary(rel: dict[str, Any]) -> dict[str, Any]:
    """The cycle summary's quality lines from a variant's ``release.json`` (D728): the judge eval's scored coverage
    and triage's agreement with the screen (overall and per kind; two model stages, not accuracy)."""
    q = rel.get("quality") or {}
    je, tvs = q.get("judge_eval") or {}, q.get("triage_vs_screen") or {}
    cov = je.get("coverage") or {}

    def prf(c: dict[str, Any]) -> dict[str, Any]:
        return {k: c.get(k) for k in ("n", "precision", "recall", "f1")}
    return {"judge_eval": {"labels_build": je.get("build"), "scored_build": je.get("scored_build"),
                           "scored": {s: f"{c.get('scored')}/{c.get('items')}" for s, c in cov.items()}},
            "triage_vs_screen": {"note": "agreement of two model stages, not accuracy",
                                 "overall": prf(tvs.get("overall") or {}),
                                 **{f"kind:{k}": prf(c) for k, c in (tvs.get("by_kind") or {}).items()}}}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- the cycle
@dataclass
class Cycle:
    s: Settings
    dry: bool
    t0: dt.datetime = field(default_factory=utcnow)
    steps: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "running"
    release: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = f"{self.t0:%Y%m%dT%H%MZ}-{os.urandom(2).hex()}"
        self.t0iso = self.t0.isoformat()
        self.digest, self.series, self.caps = plan_info(self.s.plan)
        self.code = os.environ.get("STACK_CODE_COMMIT") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "local"
        self.ops = Ops(self.s.ops_uri, read_only=self.dry)
        self.ns = "test/" if self.s.test else ""  # a test cycle's summaries and releases on the ops prefix
        self.code_exit: int | None = None
        self.check_balances_in_dry_run = False
        self.warnings: list[str] = []  # each a WARN line of the summary email (docs/DEPLOY-railway.md, "Email, severity, balances, watchdog")
        self.errors: list[str] = []    # each an ERROR line
        self.lock_lost = False         # lock.json found no longer ours (another cycle may run): no release (D777)
        self.lock: Lock | None = None  # the cycle's lock (:meth:`run`), read again before the release
        self.memlog: Memlog | None = None  # the memory log while the cycle runs (D783, D784)

    def warn(self, msg: str) -> None:
        log(f"WARN: {msg}")
        self.warnings.append(mask_s3_text(msg))

    def error(self, msg: str) -> None:
        log(f"ERROR: {msg}")
        self.errors.append(mask_s3_text(msg))

    def severity(self, code: int) -> str:
        return "ERROR" if code != EXIT_OK or self.errors else "WARN" if self.warnings else "OK"

    # -- helpers
    def inp(self, *families: str) -> list[str]:
        return [a for f in families for a in ("--in", f"data/{f}/{self.series}/")]

    def standalone(self, label: str, stage: str, families: tuple[str, ...], *, env: dict[str, str] | None = None,
                   time_cap_s: float | None = None) -> dict[str, Any]:
        args = ["run", stage, "--plan", self.s.plan, "--series", self.series, *self.inp(*families),
                "--note", f"scheduled cycle {self.id}: {label} ({self.code[:12]})"]
        log(f"{label}: stack {' '.join(args[:2])} (standalone)")
        r = run_stack(args, env=env, time_cap_s=time_cap_s)
        j = r.json_line('{"run_id"') or {}
        step = {"step": label, "rc": r.rc, "wall_min": round(r.wall_s / 60, 1), "run_id": j.get("run_id"),
                "status": j.get("status"), "error_class": j.get("error_class"),
                "scope_complete": j.get("scope_complete")}
        self.steps.append(step)
        log(f"{label} end: rc={r.rc} run {j.get('run_id')} {j.get('status')} scope_complete={j.get('scope_complete')} "
            f"({step['wall_min']} min)")
        if r.rc != 0:
            step["tail"] = r.tail[-40:]
        return step

    def s2_note(self, st: dict[str, Any]) -> None:
        """A WARN for the S2 collect run's ``failed_permanent`` windows and broken upper bounds (:func:`s2_collect_note`,
        D802); a check that fails is a WARN too."""
        if not st.get("run_id"):
            self.warn(f"{st['step']}: no run id printed (its counts were not checked)")
            return
        try:
            note = s2_collect_note(self.s.store, str(st["run_id"]))
        except Exception as exc:  # noqa: BLE001 - a check that fails is a WARN, never the cycle's crash
            self.warn(f"{st['step']}: run {st['run_id']}'s counts not read: {type(exc).__name__}")
            return
        if note:
            self.warn(note)

    def build(self, label: str, skips: list[str]) -> tuple[dict[str, Any], Result]:
        remaining = self.s.max_usd - spend_since(self.t0iso)
        args = ["build", self.s.plan, "--json", *skips, *max_usd_args(self.caps, remaining),
                "--note", f"scheduled cycle {self.id}: {label} ({self.code[:12]})"]
        log(f"{label}: stack build (cycle spend left ${remaining:.2f}) {' '.join(args[3:-2])}")
        r = run_stack(args)
        rec = r.json_doc() or self.stopped_record(r)
        step = {"step": label, "rc": r.rc, "wall_min": round(r.wall_s / 60, 1), "build_id": rec.get("build_id"),
                "status": rec.get("status"), "error_class": rec.get("error_class"),
                "incomplete": rec.get("incomplete") or [], "corpus": (rec.get("files") or {}).get("corpus")}
        self.steps.append(step)
        log(f"{label} end: rc={r.rc} build {rec.get('build_id')} {rec.get('status')}"
            f"{'[' + str(rec.get('error_class')) + ']' if rec.get('error_class') else ''} ({step['wall_min']} min)")
        if r.rc not in (0, 3):
            step["tail"] = r.tail[-40:]
        return rec, r

    @staticmethod
    def stopped_record(r: Result) -> dict[str, Any]:
        """The record of a build the cycle's own stop drained and its drain grace then cancelled (D781): ``stack
        build`` exits 130 on the cancel and prints no record; its runs published what they completed and the store
        holds its ``cancelled[operator]`` record, so the cycle reads it as stopped (:func:`classify_build`), not
        failed. Its id from the build's ``started`` line. ``{}`` (failed) for any other exit without a record."""
        if r.rc != 130 or not children.stop_reason:
            return {}
        m = next((m for x in r.head if (m := re.match(r"build (\w+) \(.*\): started", x))), None)
        log(f"  no build record printed: cancelled after the cycle's stop ({children.stop_reason}); its runs' work "
            "is published, the next cycle resumes")
        return {"build_id": m.group(1) if m else None, "status": "cancelled", "error_class": "operator",
                "stopped": {"by": "cycle", "reason": children.stop_reason}, "incomplete": []}

    def runs_of(self, build_id: str) -> list[dict[str, Any]]:
        """The build's run records (``stack show runs --json``; empty when it cannot be read)."""
        r = run_stack(["show", "runs", build_id, "--json"], track=False, echo=False)
        return list((r.json_doc() or {}).get("runs", []))

    @staticmethod
    def progress(runs: list[dict[str, Any]]) -> tuple[int, int]:
        """``(acquire rows written, triage pairs asked)`` over a build's runs (chain-r6's no-progress halts)."""
        rows = sum(int((x.get("counts") or {}).get("rows_written", 0)) for x in runs if x.get("stage") == "acquire")
        asked = sum(int((x.get("counts") or {}).get("asked_pairs", 0)) for x in runs if x.get("stage") == "triage")
        return rows, asked

    def over_caps(self, st: dict[str, Any] | None = None) -> str | None:
        """The wall or spend cap reached, else None; ``st``: a ``stack status --json`` already read (else read here)."""
        el = (utcnow() - self.t0).total_seconds() / 3600
        if el >= self.s.max_wall_h:
            return f"wall {el:.1f} h >= STACK_SCHED_MAX_WALL_H {self.s.max_wall_h}"
        usd = cap_spend_since(self.t0iso, st)
        if usd >= self.s.max_usd:
            return f"cycle spend ${usd:.2f} >= STACK_SCHED_MAX_USD {self.s.max_usd}"
        return None

    def check(self) -> bool:
        """Before each step: False when the cycle must start nothing more."""
        if children.stop_reason:
            return False
        try:
            why = self.over_caps() or self.held()
        except RuntimeError as exc:
            why = f"cap check failed: {exc}"
        if why:
            children.request_stop(why)
            return False
        return True

    def balances(self, when: str) -> None:
        """The balance checks (:func:`check_balances`) at the cycle's start and end; at the end also the runs of this
        cycle that stopped on OpenAlex's budget (ERROR)."""
        try:
            for svc, sev, msg in check_balances(self.s):
                (self.error if sev == "ERROR" else self.warn if sev == "WARN" else log)(f"balance {svc} ({when}): {msg}")
            if when == "end":
                for run in openalex_budget_stops(self.s.store, self.t0iso):
                    self.error(f"{run} stopped on its OpenAlex budget (credit or the line's max_usd)")
        except Exception as exc:  # noqa: BLE001 - a check that fails is a WARN, never the cycle's crash
            self.warn(f"balance check ({when}) failed: {type(exc).__name__}")

    def lock_gate(self) -> str | None:
        """Why nothing may be released (D777), checked just before the export and again before the upload: the
        watchdog's heartbeat found ``lock.json`` no longer this cycle's, or it is not when read again now, or it cannot
        be read. Another cycle may hold the store then and stage or upload the same release id."""
        if self.lock_lost:
            return "lock lost (the watchdog's heartbeat found lock.json no longer this cycle's)"
        try:
            ours = self.lock is not None and self.lock.ours()
        except Exception as exc:  # noqa: BLE001 - an unreadable lock is no proof of holding it: no release
            return f"lock.json not readable before the release ({type(exc).__name__})"
        if not ours:
            self.lock_lost = True
            return "lock lost (lock.json no longer this cycle's when read again before the release)"
        return None

    def held(self) -> str | None:
        h = self.ops.get_json(HOLD_KEY)
        return None if h is None else f"hold.json present ({h.get('at')}: {h.get('note') or '-'})"

    # -- the watchdog: caps, hold, drain/cancel escalation, lock heartbeat
    def watchdog(self, lock: Lock, done: threading.Event) -> None:
        """Every 30 s: the lock heartbeat, ``hold.json``, the caps (every ``watch_s``); after a stop, the drain and
        cancel of the child that was running and the exit when it outlives both. A child started after the stop (the
        release of a tail build, the only one :meth:`steps_all` starts then) gets that clock anew from its start: it
        was never drained, and its own 2 x drain grace is what it has before the exit."""
        last_cap = last_hb = time.monotonic()
        stop_at: float | None = None
        stopped: Any = None  # the child the escalation clock belongs to
        while not done.wait(30):
            now = time.monotonic()
            if now - last_hb >= self.s.heartbeat_s:
                last_hb = now
                try:
                    if not lock.heartbeat():
                        self.lock_lost = True
                        children.request_stop("lock lost")
                except Exception as exc:  # noqa: BLE001 - a failed heartbeat is logged; the cycle goes on
                    log(f"WARNING: lock heartbeat failed: {redact_error(exc)}")
            try:
                hold = None if children.stop_reason else self.held()
            except Exception as exc:  # noqa: BLE001 - an unreadable hold is logged, the caps still apply
                hold = None
                log(f"WARNING: hold check failed: {redact_error(exc)}")
            if hold:
                children.request_stop(hold)
            if now - last_cap >= self.s.watch_s:
                last_cap = now
                try:
                    st = status_json()  # one read for the cap and the LIVE line, so the two agree
                    why = self.over_caps(st)
                    log(f"LIVE: cycle {self.id}, spend ${cap_spend_since(self.t0iso, st):.2f} "
                        f"(+${live_open_since(self.t0iso, st):.2f} open in live runs), "
                        f"wall {(utcnow() - self.t0).total_seconds() / 3600:.1f} h"
                        f"{mem_note(getattr(children.current, 'pid', None))}")
                except RuntimeError as exc:
                    why = None
                    log(f"WARNING: cap check failed: {exc}")
                if why:
                    children.request_stop(why)
            if children.stop_reason and stop_at is None:
                stop_at, stopped = now, children.current
            if stop_at is not None and children.current is not None and children.current is not stopped:
                stop_at, stopped = now, children.current
            if stop_at is not None:
                if children.terms == 1 and now - stop_at > self.s.drain_grace_s:
                    children.signal_current("drain grace over")
                if now - stop_at > 2 * self.s.drain_grace_s and children.current is not None:
                    self.error("the stack process outlived drain and cancel; exiting (the container goes with it)")
                    self.outcome = "failed"
                    self.code_exit = EXIT_FAILED
                    self.finish()
                    stop_memlog(self.memlog)
                    os._exit(EXIT_FAILED)

    # -- steps
    def run(self) -> int:
        refuse, warn = preflight(self.s, self.digest)
        for w in warn:
            self.warn(w)
        hold = self.held()
        if hold:
            refuse.append(f"{hold}: paused by the operator (python tools/scheduled_run.py resume)")
        if refuse:
            for r in refuse:
                self.error(f"refused to start: {r}")
            self.outcome = "refused"
            return self.finish_code(EXIT_REFUSED)
        lock = self.lock = Lock(self.ops, self.id)
        held = lock.acquire({"code": self.code, "plan_digest": self.digest,
                             "railway_deployment": os.environ.get("RAILWAY_DEPLOYMENT_ID", "")})
        if held is not None:
            age = lock_age_h(held)
            self.error(f"refused to start: lock.json held by {held.get('holder')} (heartbeat {age if age is None else round(age, 1)} h "
                f"ago, code {held.get('code')}). If nothing runs (stack status: no live job; Railway: no active cron "
                "run), remove it: python tools/scheduled_run.py unlock --holder <holder>")
            self.outcome = "refused"
            return self.finish_code(EXIT_REFUSED)
        done = threading.Event()
        wd = threading.Thread(target=self.watchdog, args=(lock, done), daemon=True)
        try:
            st = status_json()
            live, odd = live_jobs(st)
            if odd:
                self.warn(f"recent dead/unknown jobs on the store: {', '.join(odd)}")
            if live:
                self.error(f"refused to start: live jobs on the store: {', '.join(live)} (one writer per series)")
                self.outcome = "refused"
                return self.finish_code(EXIT_REFUSED)
            log(f"cycle {self.id} start: plan {self.s.plan} digest {self.digest[:16]} series {self.series}, code "
                f"{self.code[:12]}; caps ${self.s.max_usd:.0f}, {self.s.max_wall_h:.0f} h, {self.s.max_builds} builds")
            self.memlog = start_memlog(self.s.memlog_s)
            wd.start()
            self.balances("start")
            code = self.steps_all()
            self.balances("end")
            return self.finish_code(code)
        except Exception as exc:  # noqa: BLE001 - every failure ends in a summary and a non-zero exit
            self.error(f"{type(exc).__name__}: {error_text(exc)}")
            self.outcome = "failed"
            return self.finish_code(EXIT_FAILED)
        finally:
            done.set()
            stop_memlog(self.memlog)
            self.memlog = None
            try:
                lock.release()
            except Exception as exc:  # noqa: BLE001
                log(f"WARNING: lock release failed: {redact_error(exc)}")

    def steps_all(self) -> int:
        s = self.s
        b1skip = ["--skip", "triage", "--skip", "acquire", "--skip", "fold.*", "--skip", "annotate.*"]
        alone = ["--skip", "collect.semantic_scholar", "--skip", "collect.s2-lookup", "--skip", "collect.arxiv_sweep"]
        # 1. the OpenAlex re-sweep from the floor (D582: weekly runs miss works indexed later than the look-back)
        due = s.openalex_full == "1"
        if s.openalex_full == "auto":
            last = last_openalex_full(s.store, self.series)
            due = last is None or (utcnow() - last).days >= s.openalex_full_days
            log(f"OpenAlex re-sweep: last {last.isoformat() if last else 'never'}; {'due' if due else 'not due'}")
        if due and self.check():
            st = self.standalone("openalex-full", "collect.openalex", ("sources.openalex", "coverage.openalex"),
                                 env={"STACK_OPENALEX_FULL": "1"})
            if st["rc"] != 0:  # D789: the builds' collect.openalex is the week's pass; the re-sweep stays due
                self.warn(f"OpenAlex re-sweep failed (run {st['run_id']} {st['status']}"
                          f"{'[' + str(st['error_class']) + ']' if st['error_class'] else ''}, rc={st['rc']}): the "
                          "cycle goes on with the builds' incremental collect.openalex; the re-sweep is due again "
                          "next cycle")
        # 2. S2 collect (standalone, D554), once more when scope-incomplete
        if self.check():
            st = self.standalone("s2-collect", "collect.semantic_scholar",
                                 ("sources.semantic_scholar", "coverage.semantic_scholar"))
            if st["rc"] != 0:
                return self.halt("S2 collect failed")
            self.s2_note(st)
            if st["scope_complete"] is False and self.check():
                st = self.standalone("s2-collect-2", "collect.semantic_scholar",
                                     ("sources.semantic_scholar", "coverage.semantic_scholar"))
                if st["rc"] != 0:
                    return self.halt("S2 collect (second run) failed")
                self.s2_note(st)
                if st["scope_complete"] is False:
                    self.warn("S2 collect still scope-incomplete after a second run (due queries wait for next week)")
        # 3. arXiv sweep (standalone, drained after its time cap; what it did not reach stays due)
        if self.check():
            st = self.standalone("arxiv-sweep", "collect.arxiv_sweep",
                                 ("sources.arxiv_sweep", "coverage.arxiv_sweep"), time_cap_s=s.arxiv_max_min * 60)
            if st["rc"] != 0:
                return self.halt("arXiv sweep failed")
        # 4. build 1: collectors and gather
        if self.check():
            rec, r = self.build("build-1", b1skip + alone)
            verdict, notes = classify_build(rec)
            if verdict == "retry" and self.check():
                self.warn(f"build 1 {'; '.join(notes)}: retried once")
                time.sleep(600)
                rec, r = self.build("build-1-retry", b1skip + alone)
                verdict, notes = classify_build(rec)
            for n in notes:
                log(f"  {n}")
            if verdict in ("failed", "retry", "stopped") or r.rc not in (0, 3):
                return self.halt(f"build 1 {verdict}: {'; '.join(notes) or rec.get('status')}")
            for n in notes:
                self.warn(f"build 1 {n} (retried next week)")
        # 5. s2-lookup (standalone; short is acceptable: enrichment)
        if self.check():
            st = self.standalone("s2-lookup", "collect.s2-lookup",
                                 ("sources.s2-lookup", "coverage.s2-lookup", "documents"))
            if st["rc"] != 0:
                return self.halt("s2-lookup failed")
        # 6. one-pass builds of every other line until one ends ok (or is short only on collectors); a build short
        # only on acquire's retry tail is releasable (while the tail is at most STACK_SCHED_TAIL_MAX_PCT): after a
        # halted one no further build, else the next one goes on; a cap or stop before the catch-up keeps it releasable
        ok_rec: dict[str, Any] | None = None
        # the newest such build, its tail_size words and its tail_share
        tail: tuple[dict[str, Any], str, tuple[int, int] | None] | None = None
        refused = ""  # why the newest tail build is not released (the ERROR's words)
        release_warn = ""  # the WARN of releasing an incomplete build, once the lock gate has passed
        last_notes: list[str] = []
        retried = False
        for k in range(s.max_builds):
            if not self.check():
                break
            rec, r = self.build(f"build-2.{k + 1}", alone)
            verdict, notes = classify_build(rec)
            last_notes = notes
            for n in notes:
                log(f"  {n}")
                if n.startswith("unexpected"):
                    self.warn(f"build {rec.get('build_id')} {n}; the next build retries it")
            if verdict == "ok":
                ok_rec = rec
                break
            if verdict == "retry" and not retried:
                retried = True
                self.warn(f"build {rec.get('build_id')} {'; '.join(notes)}: retried once")
                time.sleep(600)
                continue
            if verdict in ("failed", "retry"):
                return self.halt(f"build {rec.get('build_id')} {verdict}: {'; '.join(notes)}")
            if verdict == "stopped":
                break
            if verdict == "collectors":
                if s.allow_incomplete_release:
                    release_warn = (f"build {rec.get('build_id')} short only on collector lines "
                                    f"({'; '.join(notes)}); released, they are retried next week")
                    ok_rec = rec
                else:
                    log("  only collector lines short; STACK_SCHED_RELEASE_ALLOW_INCOMPLETE=0: no release")
                break
            runs = self.runs_of(str(rec.get("build_id")))
            rows, asked = self.progress(runs)
            log(f"  progress: acquire rows written {rows}, triage pairs asked {asked}")
            if verdict == "tail":
                from stack.build import HALTED_REPEAT_MIN_OK  # the build's threshold for a halted run's repeat

                run_id = next((e.get("run_id") for e in rec.get("incomplete") or [] if e["stage"] == "acquire"), None)
                tail = (rec, tail_size(runs, run_id), tail_share(runs, run_id))
                log(f"  short only on acquire's retry tail: {tail[1]}")
                if "halted" in acquire_reasons(rec):
                    log("  acquire halted on a provider: no further build this cycle (the tail is rate-limited)")
                    break
                if (ok := last_run_ok(runs, run_id)) < HALTED_REPEAT_MIN_OK:
                    log(f"  acquire's last run completed {ok} roles ok (< {HALTED_REPEAT_MIN_OK}): no further build "
                        "this cycle")
                    break
                continue
            inc = {e["stage"] for e in rec.get("incomplete") or []}
            if "acquire" in inc and rows <= 0:
                return self.halt("acquire incomplete and wrote 0 rows (no progress)")
            if "triage" in inc and asked <= 0:
                return self.halt("triage incomplete and asked 0 pairs (no progress)")
        if ok_rec is None and tail is not None:
            # a stop (a cap, hold.json, a signal) after the tail build does not change that build: its corpus is
            # written and every line after acquire ran complete on it (D769); after a lost lock nothing is released
            # (the lock gate below, D777)
            trec, words, share = tail
            within, size = tail_words(share, s.tail_max_pct)
            if not s.allow_incomplete_release:
                log("  short only on acquire's tail; STACK_SCHED_RELEASE_ALLOW_INCOMPLETE=0: no release")
            elif not within:
                refused = f"build {trec.get('build_id')} short on acquire's retry tail over its limit: {size}; "
                log(f"  {refused}not released")
            else:
                release_warn = (f"build {trec.get('build_id')} short only on acquire's retry tail "
                                f"({', '.join(sorted(acquire_reasons(trec)))}; {words}); the tail is {size}; released"
                                f"{f' after the cycle stopped ({children.stop_reason})' if children.stop_reason else ''}"
                                ", the next cycle retries the tail")
                ok_rec = trec
        if ok_rec is None:
            self.outcome = "stopped" if children.stop_reason else "incomplete"
            self.error(f"no releasable build ({refused}{children.stop_reason or f'{s.max_builds} builds ran'}); "
                       f"last build: {'; '.join(last_notes) or '-'}; the next cycle resumes")
            return EXIT_INCOMPLETE
        # 7. the release export (both variants), checked and staged on the ops prefix, then the upload; none of it
        # after a lost lock (D777)
        if why := self.lock_gate():
            return self.halt(f"{why}: build {ok_rec.get('build_id')} not released (another cycle may hold the store)")
        if release_warn:
            self.warn(release_warn)
        self.outcome = "ok"
        if s.release == "off":
            log("release export off (STACK_SCHED_RELEASE=off)")
            return EXIT_OK
        if not self.export(ok_rec):
            self.outcome = "release_failed"
            self.error("release export failed (see the log)")
            return EXIT_FAILED
        rid = str(self.release["release_id"])
        if s.test or not s.upload:
            log(f"release {rid} staged; not uploaded ({'test cycle' if s.test else 'STACK_SCHED_UPLOAD=0'})")
            if not s.test:
                self.warn(f"release {rid} staged but not uploaded (STACK_SCHED_UPLOAD=0)")
            return EXIT_OK
        if why := self.lock_gate():
            self.release["uploaded"] = False
            return self.halt(f"{why}: release {rid} staged, not uploaded (another cycle may hold the store)")
        rc = upload(s, self.ops, rid, dry=False, findings=self)
        self.release["uploaded"] = rc == EXIT_OK
        if rc != EXIT_OK:
            self.outcome = "upload_failed"
            self.error(f"release {rid}: upload failed (see the log)")
            return EXIT_FAILED
        return EXIT_OK

    def halt(self, why: str) -> int:
        self.error(f"halted: {why}")
        self.outcome = "halted"
        return EXIT_FAILED

    def export(self, rec: dict[str, Any]) -> bool:
        corpus = (rec.get("files") or {}).get("corpus") or {}
        path = corpus.get("path")
        if not path:
            log("HALT: the ok build names no corpus file")
            return False
        rid = os.environ.get("STACK_SCHED_RELEASE_ID", "").strip() or f"{utcnow():%Y-%m-%d}"  # the export's UTC date
        if self.ops.get_json(f"{self.ns}releases/{rid}/manifest.json") is not None:
            log(f"release {rid} already staged on the ops prefix; export skipped (set STACK_SCHED_RELEASE_ID)")
            self.release = {"release_id": rid, "skipped": "already staged"}
            return True
        out = self.s.work / "release" / rid
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        files: dict[str, list[dict[str, Any]]] = {}
        for v in VARIANTS:
            args = ["export", "--in", str(path), "--config", f"dataset-{v}", "--release-id", rid, "--out", str(out / v)]
            if self.s.allow_incomplete_release and rec.get("status") == "incomplete":
                args.append("--allow-incomplete")
            log(f"release export {v}: stack {' '.join(args[:6])} --release-id {rid}")
            r = run_stack(args)
            self.steps.append({"step": f"export-{v}", "rc": r.rc, "wall_min": round(r.wall_s / 60, 1)})
            if r.rc != 0:
                log(f"HALT: export {v} failed: {' | '.join(r.tail[-5:])}")
                return False
            hits = scan_for(out / v, self.s.bucket.encode())
            if hits:
                log(f"HALT: export {v}: the bucket name occurs in {len(hits)} file(s): {', '.join(hits[:5])}")
                return False
            files[v] = [{"path": p.relative_to(out / v).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
                        for p in sorted((out / v).rglob("*")) if p.is_file()]
        prefix = f"{self.ns}releases/{rid}/{self.id}"  # per cycle: a staging cut short leaves only its own files behind
        for v in VARIANTS:
            for f in files[v]:
                self.ops.b.put_file(f"{prefix}/{v}/{f['path']}", out / v / f["path"], create_only=True)
        rel = {v: json.loads((out / v / "release.json").read_text("utf-8")) for v in VARIANTS}
        manifest = {"release_id": rid, "cycle": self.id, "prefix": prefix, "build_id": rec.get("build_id"),
                    "corpus": path,
                    "code": self.code, "staged_at": utcnow().isoformat(),
                    "variants": {v: {"hf_repo": rel[v].get("hf_repo"), "rows": (rel[v].get("rows") or {}).get("out"),
                                     "files": files[v]} for v in VARIANTS}}
        self.ops.put_json(f"{self.ns}releases/{rid}/manifest.json", manifest, create_only=True)
        self.release = {"release_id": rid, "build_id": rec.get("build_id"),
                        **{v: {"rows": manifest["variants"][v]["rows"], "files": len(files[v]),
                               "bytes": sum(f["bytes"] for f in files[v])} for v in VARIANTS},
                        "quality": release_quality_summary(rel[VARIANTS[0]])}
        log(f"release {rid} staged: " + ", ".join(f"{v} {self.release[v]['rows']} rows, {self.release[v]['files']} "
                                                  f"files, {self.release[v]['bytes'] / 1e6:.1f} MB" for v in VARIANTS))
        shutil.rmtree(out, ignore_errors=True)
        return True

    # -- the summary
    def summary(self) -> dict[str, Any]:
        try:
            usd = round(spend_since(self.t0iso), 2)
        except RuntimeError:
            usd = None
        return {"cycle": self.id, "outcome": self.outcome, "started_at": self.t0iso,
                "finished_at": utcnow().isoformat(), "code": self.code, "plan": self.s.plan,
                "plan_digest": self.digest, "series": self.series, "spend_usd": usd,
                "caps": {"max_usd": self.s.max_usd, "max_wall_h": self.s.max_wall_h, "max_builds": self.s.max_builds},
                "stop_reason": children.stop_reason, "steps": self.steps, "release": self.release,
                "test": self.s.test, "exit": self.code_exit,
                "severity": self.severity(self.code_exit if self.code_exit is not None else EXIT_FAILED),
                "errors": self.errors, "warnings": self.warnings}

    def finish(self, write: bool = True) -> None:
        doc = self.summary()
        log(f"cycle {self.id} ends: {self.outcome}, spend ${doc['spend_usd']}")
        print(json.dumps(doc, indent=1), flush=True)
        if write and not self.dry:
            try:
                self.ops.put_json(f"{self.ns}cycles/{self.id}/summary.json", doc)
                self.ops.put_bytes(f"{self.ns}cycles/{self.id}/log.txt", ("\n".join(log.lines) + "\n").encode())
                self.ops.put_json(f"{self.ns}cycles/latest.json", doc)
            except Exception as exc:  # noqa: BLE001 - the summary is also on stdout
                log(f"WARNING: summary upload failed: {redact_error(exc)}")
        if not self.dry and self.code_exit is not None:
            notify_cycle(self, doc, self.code_exit)

    def finish_code(self, code: int, write: bool = True) -> int:
        self.code_exit = code
        self.finish(write=write)
        return code

    # -- the dry run
    def dry_run(self) -> int:
        s = self.s
        refuse, warn = preflight(s, self.digest)
        print(f"cycle (dry run) plan {s.plan} digest {self.digest[:16]} series {self.series}; store "
              f"{mask_s3_text(s.store)}; ops {mask_s3_text(s.ops_uri)}; code {self.code[:12]}")
        print(f"caps: ${s.max_usd:.0f} per cycle (annotate lines get --max-usd min(own cap, left); triage's $20 per run "
              f"on top), {s.max_wall_h:.0f} h wall, {s.max_builds} builds after build 1, arXiv {s.arxiv_max_min:.0f} min")
        held = self.ops.get_json(Lock.KEY)
        if held is not None:
            age = lock_age_h(held)
            refuse.append(f"lock.json held by {held.get('holder')} (heartbeat {age if age is None else round(age, 1)} h)")
        print(f"lock: {'held by ' + str(held.get('holder')) if held else 'free'}")
        hold = self.held()
        if hold:
            refuse.append(hold + " (python tools/scheduled_run.py resume)")
        st = status_json()
        live, odd = live_jobs(st)
        if live:
            refuse.append(f"live jobs on the store: {', '.join(live)}")
        print(f"store jobs: {st.get('by_liveness')}; recent dead/unknown: {', '.join(odd) or '-'}")
        builds = [j for j in st["jobs"] if j["kind"] == "build"][:3]
        for j in builds:
            print(f"  recent build {j['started_at'][:16]} {j['id']} {j['state']} ${j['spend_usd_micro'] / 1e6:.2f}")
        since14 = (utcnow() - dt.timedelta(days=14)).isoformat()
        print(f"spend of jobs started in the last 14 days: ${spend_since(since14, st):.2f}")
        last = last_openalex_full(s.store, self.series)
        due = s.openalex_full == "1" or (s.openalex_full == "auto" and
                                         (last is None or (utcnow() - last).days >= s.openalex_full_days))
        b1skip = ["--skip", "triage", "--skip", "acquire", "--skip", "fold.*", "--skip", "annotate.*"]
        alone = ["--skip", "collect.semantic_scholar", "--skip", "collect.s2-lookup", "--skip", "collect.arxiv_sweep"]
        print("would run, in order (each a recorded job; any rc other than 0/3 halts):")
        print(f"  1. {'collect.openalex re-sweep (STACK_OPENALEX_FULL=1)' if due else 'no OpenAlex re-sweep'} "
              f"(last {last.isoformat()[:10] if last else 'never'})")
        print("  2. stack run collect.semantic_scholar (again once when scope-incomplete)")
        print(f"  3. stack run collect.arxiv_sweep (drained after {s.arxiv_max_min:.0f} min)")
        print("  4. build 1: stack build " + " ".join([s.plan, *b1skip, *alone]))
        print("  5. stack run collect.s2-lookup")
        print(f"  6. builds 2.1-2.{s.max_builds}: stack build {s.plan} {' '.join(alone)} [--max-usd ...] until ok "
              f"(or short only on collectors, or on acquire's retry tail of at most {s.tail_max_pct:g}% of the "
              "documents acquire covers)")
        print(f"  7. release export {'off' if s.release == 'off' else 'of the ok (or releasable) build: dataset-public, dataset-internal'}"
              f" -> {mask_s3_text(s.ops_uri)}/{self.ns}releases/<export date>/; Hugging Face upload "
              f"{'off (test)' if s.test else 'on' if s.upload else 'off (STACK_SCHED_UPLOAD=0)'}")
        print(f"  8. summary email (OK/WARN/ERROR) via {alert_channel() or 'NONE configured'}; balance checks at start "
              "and end")
        if self.check_balances_in_dry_run:
            for svc, sev, msg in check_balances(self.s):
                print(f"  balance {svc}: {sev} {msg}")
        else:
            print("  (balances not read in a dry run; --check-balances reads them: OpenRouter credits, OpenAlex headers)")
        for label, skips in (("build 1", b1skip + alone), ("build 2", alone)):
            r = run_stack(["build", s.plan, "--dry-run", "--json", *skips], track=False, echo=False)
            d = r.json_doc()
            if r.rc != 0 or d is None:
                refuse.append(f"{label} dry run rc={r.rc}: {' | '.join(r.tail[-3:])}")
                continue
            errs = [f"{x['stage']}: {x['error']}" for x in d.get("stages", []) if x.get("error")]
            pre = d.get("preflight") or []
            runs = [x["stage"] for x in d.get("stages", []) if not x.get("skipped") and not x.get("error")]
            print(f"{label} dry run: {len(runs)} lines run: {', '.join(runs)}")
            if d.get("new_series") or d.get("env_limits"):
                refuse.append(f"{label}: new_series {d.get('new_series')} env_limits {d.get('env_limits')}")
            refuse += [f"{label} line error: {e}" for e in errs] + [f"{label} preflight: {p}" for p in pre]
        staged = sorted({k.split("/")[1] for k in self.ops.b.list("releases/") if k.endswith("/manifest.json")})
        if self.s.test:
            print("TEST cycle: real runs on the store, test caps, no Hugging Face upload, ops keys under test/")
        print(f"staged releases on the ops prefix: {', '.join(staged) or '-'}")
        for w in warn:
            print(f"warning: {w}")
        for why in refuse:
            print(f"WOULD REFUSE: {mask_s3_text(why)}")
        print("dry run: nothing written to S3, no LLM call, no upload")
        return EXIT_REFUSED if refuse else EXIT_OK


# ---------------------------------------------------------------- the upload
def upload(s: Settings, ops: Ops, rid: str, *, dry: bool, check_hf: bool = False, ns: str = "",
           findings: Any = None) -> int:
    """Upload a staged release's two variants to their Hugging Face dataset repos (every successful weekly
    cycle publishes). The internal variant's repo must be private before anything is uploaded
    and is checked again afterwards; when it is not, nothing (more) is uploaded, the command fails and the cycle
    alerts. No repo's visibility is ever changed here: the public variant's repo is uploaded to whatever its
    visibility (the maintainer makes it public by hand). Each variant goes up as ONE commit that makes the repo's tree exactly
    the release (every file added, every other file deleted except ``.gitattributes``: each release is clean and
    tagged), on the repo's head as read just before (``parent_commit``: a concurrent push fails the commit), and
    the commit is tagged :func:`release_tag` (an existing tag is a WARN, never moved)."""
    man = ops.get_json(f"{ns}releases/{rid}/manifest.json")
    if man is None:
        log(f"REFUSED: no staged release {rid} ({ns}releases/{rid}/manifest.json on the ops prefix)")
        return EXIT_REFUSED
    for v in VARIANTS:
        mv = man["variants"][v]
        prev = ops.get_json(f"{ns}releases/{rid}/uploaded-{v}.json")
        print(f"release {rid} {v}: {mv['rows']} rows, {len(mv['files'])} files -> {mv['hf_repo']}"
              + (f" (uploaded before: commit {prev.get('commit')})" if prev else ""))
    if s.test and not (dry or check_hf):
        log("REFUSED: a test cycle uploads nothing")
        return EXIT_REFUSED
    token = key_value("HF_TOKEN")
    if not token:
        log("REFUSED: HF_TOKEN not set")
        return EXIT_REFUSED
    from huggingface_hub import HfApi  # type: ignore[import-not-found,unused-ignore]  # the `release` uv group

    api = HfApi(token=token)
    repos = {v: str(man["variants"][v]["hf_repo"]) for v in VARIANTS}
    private_repo = repos["internal"]

    def private_ok(when: str) -> bool:
        priv = api.repo_info(private_repo, repo_type="dataset").private
        log(f"{private_repo} (internal variant): private={priv} ({when})")
        return priv is True

    if not private_ok("before upload"):
        log(f"ERROR: {private_repo} is not private: nothing uploaded; its visibility is left as it is")
        return EXIT_FAILED
    pub = api.repo_info(repos["public"], repo_type="dataset").private
    log(f"{repos['public']} (public variant): private={pub}; uploaded to as it is")
    if dry or check_hf:
        log("dry run: nothing uploaded")
        return EXIT_OK
    work = s.work / "upload" / rid
    for v in ("internal", "public"):
        repo = repos[v]
        d = work / v
        d.mkdir(parents=True, exist_ok=True)
        for f in man["variants"][v]["files"]:
            fp = d / f["path"]
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(ops.b.get(f"{man['prefix']}/{v}/{f['path']}"))
            if sha256_file(fp) != f["sha256"]:
                log(f"HALT: {v}/{f['path']}: sha256 differs from the manifest; nothing more uploaded")
                return EXIT_FAILED
        hits = scan_for(d, s.bucket.encode())
        if hits:
            log(f"HALT: {v}: the bucket name occurs in {', '.join(hits[:5])}; nothing more uploaded")
            return EXIT_FAILED
        if v == "internal" and not private_ok("just before its upload"):
            log(f"ERROR: {private_repo} turned public: nothing uploaded; its visibility is left as it is")
            return EXIT_FAILED
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete  # type: ignore[import-not-found,unused-ignore]

        wanted = {f["path"] for f in man["variants"][v]["files"]}
        head = api.repo_info(repo, repo_type="dataset").sha
        stale = sorted(set(api.list_repo_files(repo, repo_type="dataset", revision=head)) - wanted - {".gitattributes"})
        ops_: list[Any] = [CommitOperationAdd(path_in_repo=f, path_or_fileobj=str(d / f)) for f in sorted(wanted)]
        ops_ += [CommitOperationDelete(path_in_repo=f, is_folder=False) for f in stale]
        commit = api.create_commit(repo, ops_, commit_message=f"release {rid} ({v})", repo_type="dataset",
                                   parent_commit=head,
                                   commit_description=f"Release {rid}, {len(wanted)} files"
                                   + (f"; removed {len(stale)} files of earlier releases" if stale else ""))
        oid = getattr(commit, "oid", None)
        tag, tag_note = release_tag(rid), "created"
        try:
            api.create_tag(repo, tag=tag, revision=oid, repo_type="dataset", tag_message=f"release {rid}")
        except Exception as exc:  # noqa: BLE001 - an existing tag (a re-upload of the same id) is never moved
            tag_note = f"not created ({type(exc).__name__})"
            msg = f"{repo}: tag {tag} {tag_note}; the release commit {oid} is untagged"
            (findings.warn if findings is not None else log)(msg)
        after = api.repo_info(repo, repo_type="dataset").private
        doc = {"repo": repo, "commit": oid, "tag": tag, "tag_status": tag_note, "private_after": after,
               "at": utcnow().isoformat(), "removed_files": stale}
        ops.put_json(f"{ns}releases/{rid}/uploaded-{v}.json", doc)
        log(f"{repo}: commit {oid}, tag {tag} {tag_note}; {len(wanted)} files, {len(stale)} removed; private: {after}")
        if v == "internal" and after is not True:
            log(f"ERROR: {private_repo} is not private after the upload; its visibility is left as it is")
            return EXIT_FAILED
    shutil.rmtree(work, ignore_errors=True)
    return EXIT_OK


# ---------------------------------------------------------------- balances
#: A free request that carries OpenAlex's rate-limit headers ("Get single entity: Free").
OPENALEX_PROBE = "https://api.openalex.org/works/W2741809807?select=id"
OPENROUTER_CREDITS = "https://openrouter.ai/api/v1/credits"  # account credits; a management key only
OPENROUTER_KEY = "https://openrouter.ai/api/v1/key"          # the calling key's own limit; any key


def _get_json(url: str, headers: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "stack-scheduled-run", **headers})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return dict(json.loads(resp.read() or b"{}")), {k.lower(): v for k, v in resp.headers.items()}


def check_balances(s: Settings) -> list[tuple[str, str, str]]:
    """``[(service, OK|WARN|ERROR|SKIP, message)]`` for the paid services the cycle draws on prepaid credit
    (docs/DEPLOY-railway.md, "Email, severity, balances, watchdog"): OpenRouter's account credits (``/credits``, total credits minus total usage; it
    needs a management key, ``OPENROUTER_MANAGEMENT_KEY``), else the API key's own ``limit_remaining``
    (``/key``); OpenAlex's prepaid remaining from the headers of one free request (``x-ratelimit-prepaid-
    remaining-usd``; not in OpenAlex's docs, seen on every keyed answer; the key goes as a Bearer header, never in a
    URL, so the documented ``/rate-limit?api_key=`` endpoint is not used). WARN below ``STACK_SCHED_BALANCE_WARN_<SVC>_USD``,
    ERROR at or below zero. A failed check is a WARN (the check, not the balance). OpenAI and Anthropic publish
    no balance endpoint for a normal API key: SKIP, rely on their own billing alerts."""
    out: list[tuple[str, str, str]] = []

    def judge(svc: str, left: float, env: str, default: float) -> None:
        th = env_bool_float(env, default)
        sev = "ERROR" if left <= 0 else "WARN" if left < th else "OK"
        out.append((svc, sev, f"${left:.2f} left (warn below ${th:.2f}, {env})"))

    mkey = key_value("OPENROUTER_MANAGEMENT_KEY") if key_present("OPENROUTER_MANAGEMENT_KEY") else ""
    key = key_value("OPENROUTER_API_KEY") if key_present("OPENROUTER_API_KEY") else ""
    try:
        if mkey:
            d, _h = _get_json(OPENROUTER_CREDITS, {"Authorization": f"Bearer {mkey}"})
            data = d.get("data") or {}
            judge("openrouter", float(data["total_credits"]) - float(data["total_usage"]),
                  "STACK_SCHED_BALANCE_WARN_OPENROUTER_USD", 50.0)
        elif key:
            d, _h = _get_json(OPENROUTER_KEY, {"Authorization": f"Bearer {key}"})
            left = (d.get("data") or {}).get("limit_remaining")
            if left is None:
                out.append(("openrouter", "SKIP", "the API key has no credit limit and no OPENROUTER_MANAGEMENT_KEY "
                                                  "is set: the account balance is not readable"))
            else:
                judge("openrouter", float(left), "STACK_SCHED_BALANCE_WARN_OPENROUTER_USD", 50.0)
                out[-1] = (out[-1][0], out[-1][1], out[-1][2] + " (the key's own limit)")
        else:
            out.append(("openrouter", "SKIP", "no OPENROUTER_API_KEY"))
    except Exception as exc:  # noqa: BLE001 - reported without the response body
        out.append(("openrouter", "WARN", f"credits check failed ({type(exc).__name__})"))
    key = key_value("OPENALEX_API_KEY") if key_present("OPENALEX_API_KEY") else ""
    if key:
        try:
            _d, h = _get_json(OPENALEX_PROBE, {"Authorization": f"Bearer {key}"})
            pre = h.get("x-ratelimit-prepaid-remaining-usd")
            day = h.get("x-ratelimit-remaining-usd")
            if pre is None:
                out.append(("openalex", "WARN", "no x-ratelimit-prepaid-remaining-usd header in the answer"))
            else:
                judge("openalex", float(pre), "STACK_SCHED_BALANCE_WARN_OPENALEX_USD", 2.0)
                out[-1] = (out[-1][0], out[-1][1], out[-1][2] + f"; today's remaining allowance ${float(day or 0):.2f}")
        except Exception as exc:  # noqa: BLE001
            out.append(("openalex", "WARN", f"rate-limit header check failed ({type(exc).__name__})"))
    else:
        out.append(("openalex", "SKIP", "no OPENALEX_API_KEY (keyless: free, unmetered)"))
    for svc, env in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
        if key_present(env):
            out.append((svc, "SKIP", "no balance endpoint for an API key; set a budget alert in its console"))
    return out


def openalex_budget_stops(store: str, since: str) -> list[str]:
    """Runs started at or after ``since`` that stopped on OpenAlex's budget (``openalex_budget_stop`` counts)."""
    from stack.cli.common import mirror_base
    from stack.store.index import store_index
    from stack.store.remote import resolve_store

    idx = store_index(resolve_store(store, mirror_base=mirror_base()), refresh=True)
    return [f"{e.record.stage} {e.record.run_id}" for e in idx.runs
            if str(e.record.started_at) >= since and (e.record.counts or {}).get("openalex_budget_stop")]


def s2_collect_note(store: str, run_id: str) -> str | None:
    """The WARN text for a standalone S2 collect run (D802), from its run record: the build's
    ``failed_permanent_note`` (``windows_failed_permanent.<class>`` counts; S2 runs outside the builds, D554, so no
    build record says it) and S2's ``upper_bound_ignored`` / ``upper_bound_fallback`` counts; ``None`` when all are
    zero. A record the index does not list is said, not skipped."""
    from stack.build import failed_permanent_note
    from stack.cli.common import mirror_base
    from stack.store.index import store_index
    from stack.store.remote import resolve_store

    idx = store_index(resolve_store(store, mirror_base=mirror_base()), refresh=True)
    rec = next((e.record for e in idx.runs if e.record.run_id == run_id), None)
    if rec is None:
        return f"S2 collect run {run_id}: no run record in the store index (its counts were not checked)"
    c = rec.counts or {}
    ignored, fallback = int(c.get("upper_bound_ignored") or 0), int(c.get("upper_bound_fallback") or 0)
    notes = [failed_permanent_note(rec)]
    if ignored or fallback:
        notes.append(f"{rec.stage} run {run_id}: S2 ignored a year span's upper bound x{ignored}, the span re-asked "
                     f"open-ended x{fallback} (D802)")
    return "; ".join(n for n in notes if n) or None


def release_tag(rid: str) -> str:
    """The Hugging Face tag of release ``rid``: ``release-<id>`` (``release-2026-11-01``), so a tag never reads
    like a branch or a version of the hub's own."""
    return f"release-{rid}"


# ---------------------------------------------------------------- alerts
def alert_channel() -> str | None:
    """``resend`` or ``smtp`` when ``STACK_SCHED_ALERT`` names one whose settings are present and
    ``STACK_SCHED_ALERT_TO`` is set; else ``None`` (the preflight warns)."""
    ch = os.environ.get("STACK_SCHED_ALERT", "").strip()
    if not os.environ.get("STACK_SCHED_ALERT_TO", "").strip() or not os.environ.get("STACK_SCHED_ALERT_FROM", ""):
        return None
    if ch == "resend" and key_present("RESEND_API_KEY"):
        return "resend"
    if ch == "smtp" and os.environ.get("STACK_SCHED_SMTP_HOST", "").strip():
        return "smtp"
    return None


def send_email(subject: str, text: str) -> bool:
    """Send one plain-text email through the configured channel; False (logged, never raised) when none is
    configured or the send fails: an alert must not turn a cycle's outcome into a crash."""
    ch = alert_channel()
    if ch is None:
        log(f"alert not sent (no channel configured): {subject}")
        return False
    to = [a.strip() for a in os.environ["STACK_SCHED_ALERT_TO"].split(",") if a.strip()]
    sender = os.environ["STACK_SCHED_ALERT_FROM"].strip()
    subject, text = mask_s3_text(subject), mask_s3_text(text)
    try:
        if ch == "resend":
            import urllib.request

            # https://resend.com/docs/api-reference/emails/send-email: POST /emails, Bearer key, from/to/subject/text
            req = urllib.request.Request(
                "https://api.resend.com/emails", method="POST",
                data=json.dumps({"from": sender, "to": to, "subject": subject, "text": text}).encode(),
                headers={"Authorization": f"Bearer {key_value('RESEND_API_KEY')}", "Content-Type": "application/json",
                         "User-Agent": "stack-scheduled-run"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                ok = bool(200 <= int(resp.status) < 300)
        else:
            import smtplib
            from email.message import EmailMessage

            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(to), subject
            msg.set_content(text)
            port = int(os.environ.get("STACK_SCHED_SMTP_PORT", "587"))
            with smtplib.SMTP(os.environ["STACK_SCHED_SMTP_HOST"], port, timeout=30) as smtp:
                smtp.starttls()
                if os.environ.get("STACK_SCHED_SMTP_USER"):
                    smtp.login(os.environ["STACK_SCHED_SMTP_USER"], key_value("STACK_SCHED_SMTP_PASSWORD"))
                smtp.send_message(msg)
            ok = True
    except Exception as exc:  # noqa: BLE001 - reported, never raised (the error text carries no key)
        log(f"WARNING: alert email failed ({ch}): {type(exc).__name__}")
        return False
    log(f"alert email sent ({ch}): {subject}" if ok else f"WARNING: alert email answered non-2xx ({ch})")
    return ok


def notify_cycle(cyc: Cycle, doc: dict[str, Any], code: int) -> None:
    """The summary email after every cycle, its title naming the worst severity: subject ``[test] OK|WARN|ERROR: …``, body the errors, the warnings, then the summary."""
    tag = "[test] " if cyc.s.test else ""
    sev = doc["severity"]
    lines = [f"{sev}: Stack cycle {cyc.id}: {doc['outcome']} (exit {code}); spend ${doc['spend_usd']}; "
             f"code {cyc.code[:12]}; {doc['started_at'][:16]} to {doc['finished_at'][:16]} UTC", ""]
    lines += [f"ERRORS ({len(doc['errors'])}):", *(f"  - {e}" for e in doc["errors"])] if doc["errors"] else []
    lines += [f"WARNINGS ({len(doc['warnings'])}):", *(f"  - {w}" for w in doc["warnings"])] if doc["warnings"] else []
    lines += ["", f"stop reason: {doc['stop_reason'] or '-'}", f"release: {json.dumps(doc['release'])}", "", "steps:"]
    for st in doc["steps"]:
        lines.append(f"  {st.get('step')}: rc={st.get('rc')} {st.get('status') or ''} "
                     f"{st.get('run_id') or st.get('build_id') or ''} {st.get('wall_min')} min")
    bad = next((st for st in reversed(doc["steps"]) if st.get("tail")), None)
    if bad:
        lines += ["", f"last lines of {bad['step']}:", *bad["tail"][-30:]]
    if sev != "OK":
        lines += ["", "the cycle's own log (last 40 lines):", *log.tail(40)]
    lines += ["", f"More: python tools/scheduled_run.py report --cycle {cyc.id} (docs/DEPLOY-railway.md)"]
    send_email(f"{tag}{sev}: Stack weekly cycle {doc['outcome']} ({cyc.id})", "\n".join(lines))


# ---------------------------------------------------------------- entry
def install_signals() -> None:
    first: list[float] = []

    def on_term(signum: int, _frame: Any) -> None:
        now = time.monotonic()
        if not first:
            first.append(now)
            children.request_stop(f"signal {signal.Signals(signum).name}")
        elif now - first[0] >= 5:
            children.signal_current(f"second {signal.Signals(signum).name}")

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)


def watchdog(s: Settings, *, now: dt.datetime | None = None) -> int:
    """The second cron service (Mondays 06:00 UTC, docs/DEPLOY-railway.md): reads the production ``cycles/latest.json``
    and ``lock.json`` and mails ERROR when the last cycle's summary is missing, finished more than
    ``STACK_SCHED_WATCHDOG_MAX_AGE_H`` (168) hours ago, has severity ERROR, or a cycle still holds the lock. Catches
    what a cycle cannot report itself: an out-of-memory kill, an image that does not start, a schedule that did not
    fire. Silent (exit 0) when the last cycle is OK or WARN and recent."""
    now = now or utcnow()
    ops = Ops(s.ops_uri, read_only=True)
    doc = ops.get_json("cycles/latest.json")
    lock = ops.get_json(Lock.KEY)
    max_h = env_bool_float("STACK_SCHED_WATCHDOG_MAX_AGE_H", 168.0)
    problems: list[str] = []
    if doc is None:
        problems.append("no production cycle summary (cycles/latest.json) on the ops prefix")
    else:
        age_h = (now - dt.datetime.fromisoformat(str(doc["finished_at"]))).total_seconds() / 3600
        if age_h > max_h:
            problems.append(f"the last cycle {doc.get('cycle')} finished {age_h:.0f} h ago (> {max_h:.0f} h): "
                            "the weekly run did not run, or died without a summary (out of memory? image?)")
        if doc.get("severity") not in ("OK", "WARN"):
            problems.append(f"the last cycle {doc.get('cycle')} ended {doc.get('severity')}: {doc.get('outcome')}; "
                            + "; ".join(doc.get("errors") or [])[:1500])
    if lock is not None:
        age = lock_age_h(lock)
        problems.append(f"lock.json held by {lock.get('holder')} (heartbeat {age if age is None else round(age, 1)} h "
                        "ago): a cycle is still running or died holding it")
    if not problems:
        log(f"watchdog: last cycle {doc.get('cycle') if doc else '-'} {doc.get('severity') if doc else '-'}; nothing to report")
        return EXIT_OK
    for pr in problems:
        log(f"watchdog ERROR: {pr}")
    send_email("ERROR: Stack weekly cycle watchdog", "\n".join(
        ["The Monday check of the weekly Stack cycle found:", *(f"  - {pr}" for pr in problems), "",
         f"last summary: {json.dumps(doc, indent=1)[:4000] if doc else '-'}", "",
         "More: python tools/scheduled_run.py report; railway cron list --service stack-cycle (docs/DEPLOY-railway.md)"]))
    return EXIT_FAILED


def run_cycle_alerting(test: bool) -> int:
    """A real cycle; any exception that escapes it (settings, plan, a bug) still mails an alert before it
    propagates (the process then exits 1 with the traceback in the logs)."""
    try:
        s = Settings.from_env(test=test)
        cyc = Cycle(s, dry=False)
        install_signals()
        return cyc.run()
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            raise
        tag = "[test] " if test or os.environ.get("STACK_SCHED_TEST") == "1" else ""
        send_email(f"{tag}ERROR: Stack weekly cycle CRASHED: {type(exc).__name__}",
                   f"{type(exc).__name__}: {error_text(exc) if isinstance(exc, Exception) else exc}\n\n"
                   + "\n".join(log.tail(60)))
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle", help="the production cycle")
    c.add_argument("--dry-run", action="store_true", help="read only; print what would run")
    c.add_argument("--test", action="store_true", help="test cycle: test caps, no Hugging Face upload, ops test/")
    c.add_argument("--check-balances", action="store_true", help="dry run: also read the services' balances")
    sub.add_parser("watchdog", help="mail ERROR when the last production cycle is missing, stale or ERROR")
    u = sub.add_parser("upload", help="upload a staged release again (a cycle uploads its own)")
    u.add_argument("--release-id", required=True)
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--check-hf", action="store_true", help="also read both repos' settings (no upload)")
    k = sub.add_parser("unlock", help="remove lock.json of a dead cycle")
    k.add_argument("--holder", required=True, help="the holder id the refusal printed")
    ps = sub.add_parser("pause", help="write hold.json: the running cycle drains, new cycles refuse")
    ps.add_argument("--note", default="")
    sub.add_parser("resume", help="remove hold.json")
    rp = sub.add_parser("report", help="print cycles/latest.json (or one cycle's summary)")
    rp.add_argument("--cycle")
    a = ap.parse_args(argv)
    if a.cmd == "cycle" and not a.dry_run:
        return run_cycle_alerting(bool(a.test))
    s = Settings.from_env(test=bool(getattr(a, "test", False)))
    if a.cmd == "cycle":
        cyc = Cycle(s, dry=True)
        cyc.check_balances_in_dry_run = bool(a.check_balances)
        return cyc.dry_run()
    if a.cmd == "watchdog":
        return watchdog(s)
    if a.cmd == "upload":
        ops = Ops(s.ops_uri, read_only=a.dry_run)
        if a.dry_run and not a.check_hf:
            man = ops.get_json(f"releases/{a.release_id}/manifest.json")
            print(f"upload (dry run) of {a.release_id}: staged {'yes' if man else 'no'}; HF_TOKEN "
                  f"{'set' if key_present('HF_TOKEN') else 'absent'}; no Hugging Face call")
            return EXIT_OK
        rc = upload(s, ops, a.release_id, dry=a.dry_run, check_hf=a.check_hf)
        if rc != EXIT_OK and not a.dry_run:
            send_email(f"ERROR: Stack release upload failed ({a.release_id})", "\n".join(log.tail(60)))
        return rc
    if a.cmd == "report":
        doc = Ops(s.ops_uri, read_only=True).get_json(f"cycles/{a.cycle}/summary.json" if a.cycle
                                                         else "cycles/latest.json")
        print(json.dumps(doc, indent=1) if doc else "no cycle summary")
        return EXIT_OK if doc else EXIT_REFUSED
    ops = Ops(s.ops_uri, read_only=False)
    if a.cmd == "pause":
        ops.put_json(HOLD_KEY, {"at": utcnow().isoformat(), "note": a.note})
        print("hold.json written: a running cycle drains at its next check (<= 30 s), new cycles refuse")
        return EXIT_OK
    if a.cmd == "resume":
        ops.b.delete(HOLD_KEY)
        print("hold.json removed")
        return EXIT_OK
    cur = ops.get_json(Lock.KEY)
    if cur is None:
        print("no lock.json")
        return EXIT_OK
    if cur.get("holder") != a.holder:
        print(f"lock.json is held by {cur.get('holder')}, not {a.holder}: left as is")
        return EXIT_REFUSED
    ops.b.delete(Lock.KEY)
    print(f"removed lock.json of {a.holder} (heartbeat {cur.get('heartbeat_at')})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
