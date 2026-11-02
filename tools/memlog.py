"""The scheduled cycle's memory log (D783): one line every ``--interval`` seconds to stdout, the container's memory
and its largest processes.

    python tools/memlog.py [--interval S] [--top N] [--count N]

    2026-11-01T00:12:00Z [mem] cg 21.3/32.0G peak 31.9 anon 9.1 file 12.0 shmem 0.0 swap 0.0/0.0G oom_kill 0
        pids 412/1000 |
        stack-build 8.7G 180%, sched 0.3G 1%, pdftotext 0.2G 95%          (one line)

``tools/scheduled_run.py cycle`` starts it when a cycle starts and stops it when the cycle ends; it is a separate
process so that a slow ``/proc`` scan never delays the cycle and a failure here never fails it. Totals are the
cgroup v2 files (``memory.current`` of ``memory.max``, ``memory.peak``, ``memory.stat``'s anon / file / shmem,
``memory.swap.current`` of ``memory.swap.max``, ``memory.events``' oom_kill; ``pids.current`` of ``pids.max``, the
tasks, threads included, that a container may run, D786) of ``/sys/fs/cgroup`` or, when that is
the host's root, of this process's own cgroup; without cgroup v2, ``/proc/meminfo``. Processes: the ``--top`` largest
by VmRSS, each with its CPU since the previous line (none on the first line, nor for a process new since then).
Python processes are named by their script or module (``sched``, ``memlog``, ``stack-<subcommand>``). Every field is
best effort: what cannot be read is left out, and a failed sample is a line saying so.

Interval: ``--interval``, else ``STACK_SCHED_MEMLOG_S``, else 180; 0 (or less) prints nothing and exits. It exits by
itself when its parent changes (the cycle died without stopping it), checked every second (``--parent``: the
cycle's pid), and when its stdout (the cycle's pipe, D784) is closed. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
#: Python scripts by a shorter name; ``stack`` (the console script or ``-m stack.cli``) gets its subcommand.
SCRIPT_NAMES = {"scheduled_run": "sched", "stack.cli": "stack"}
NAME_MAX = 16


def gb(n: int) -> str:
    return f"{n / 2**30:.1f}"


def read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(p: Path) -> int | None:
    v = read(p)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def keyed(p: Path, sep: str | None = None) -> dict[str, int]:
    """``key value`` lines (``memory.stat``, ``memory.events``; ``/proc/meminfo`` with ``sep=":"``, kB) as ints."""
    out: dict[str, int] = {}
    for ln in (read(p) or "").splitlines():
        k, _, v = ln.partition(sep) if sep else ln.partition(" ")
        try:
            out[k.strip()] = int(v.split()[0])
        except (IndexError, ValueError):
            pass
    return out


def of_max(cur: int, mx: str | None) -> str:
    """``cur/max`` in GiB: ``21.3/32.0G``; ``21.3G/max`` without a limit; ``21.3G`` when the limit is unreadable."""
    if mx is None:
        return f"{gb(cur)}G"
    try:
        return f"{gb(cur)}/{gb(int(mx))}G"
    except ValueError:
        return f"{gb(cur)}G/{mx}"


def cgroup_dir(sys_cg: Path, proc: Path) -> Path | None:
    """``sys_cg`` itself when it is a cgroup with memory accounting (a container's namespace: its own cgroup), else
    this process's cgroup under it (a host's root cgroup has no ``memory.current``); ``None`` without cgroup v2."""
    cands = [sys_cg]
    for ln in (read(proc / "self" / "cgroup") or "").splitlines():
        if ln.startswith("0::") and ln[3:].strip("/ "):
            cands.append(sys_cg / ln[3:].strip("/ "))
    return next((c for c in cands if read_int(c / "memory.current") is not None), None)


def totals(sys_cg: Path, proc: Path) -> str:
    parts: list[str] = []
    cg = cgroup_dir(sys_cg, proc)
    if cg is not None:
        cur = read_int(cg / "memory.current")
        if cur is not None:
            parts.append(f"cg {of_max(cur, read(cg / 'memory.max'))}")
        peak = read_int(cg / "memory.peak")
        if peak is not None:
            parts.append(f"peak {gb(peak)}")
        st = keyed(cg / "memory.stat")
        parts += [f"{k} {gb(st[k])}" for k in ("anon", "file", "shmem") if k in st]
        swap = read_int(cg / "memory.swap.current")
        if swap is not None:
            parts.append(f"swap {of_max(swap, read(cg / 'memory.swap.max'))}")
        ev = keyed(cg / "memory.events")
        if "oom_kill" in ev:
            parts.append(f"oom_kill {ev['oom_kill']}")
        pids = read_int(cg / "pids.current")  # threads and processes, of the container's limit (D786)
        if pids is not None:
            parts.append(f"pids {pids}/{read(cg / 'pids.max') or '?'}")
    if not parts:
        mi = {k: v * 1024 for k, v in keyed(proc / "meminfo", ":").items()}
        if "MemTotal" in mi and "MemAvailable" in mi:
            parts.append(f"mem {of_max(mi['MemTotal'] - mi['MemAvailable'], str(mi['MemTotal']))} "
                         f"avail {gb(mi['MemAvailable'])}")
        if "SwapTotal" in mi and "SwapFree" in mi:
            parts.append(f"swap {of_max(mi['SwapTotal'] - mi['SwapFree'], str(mi['SwapTotal']))}")
    return " ".join(parts)


def short_name(comm: str, argv: list[str]) -> str:
    """``comm``, except for Python: the script's stem or the module (``SCRIPT_NAMES``), and for ``stack`` its
    subcommand (``stack-build``). A console script's shebang makes ``argv`` start with the interpreter."""
    if not argv or not Path(argv[0]).name.startswith("python"):  # node names its main thread "MainThread"
        return (Path(argv[0]).name if argv and comm == "MainThread" else comm)[:NAME_MAX]
    target, rest, i = None, argv[1:], 0
    while i < len(rest):
        a = rest[i]
        if a == "-c":
            return "python-c"
        if a == "-m":
            target, i = (rest[i + 1] if i + 1 < len(rest) else None), i + 2
            break
        if a in ("-W", "-X", "-Q"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        target, i = Path(a).stem, i + 1
        break
    if not target:
        return comm[:NAME_MAX]
    name = SCRIPT_NAMES.get(target, target.rsplit(".", 1)[-1])
    if name == "stack":
        sub = next((a for a in rest[i:] if not a.startswith("-")), None)
        name = f"stack-{sub}" if sub else name
    return name[:NAME_MAX]


@dataclass
class Proc:
    pid: int
    name: str
    rss: int    # bytes
    ticks: int  # utime + stime
    start: int  # starttime: with the pid, the process's identity across samples


def scan(proc: Path) -> list[Proc]:
    """Every process with a VmRSS (kernel threads have none); one that exits mid-scan is skipped."""
    out = []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            rss = next((int(ln.split()[1]) * 1024 for ln in (d / "status").read_text().splitlines()
                        if ln.startswith("VmRSS:")), None)
            if rss is None:
                continue
            stat = (d / "stat").read_text()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            f = stat[stat.rindex(")") + 2:].split()  # f[0] is field 3 (state): utime 14, stime 15, starttime 22
            argv = [a.decode(errors="replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a]
            out.append(Proc(int(d.name), short_name(comm, argv), rss, int(f[11]) + int(f[12]), int(f[19])))
        except (OSError, ValueError, IndexError):
            continue
    return out


class Sampler:
    def __init__(self, proc: Path = Path("/proc"), sys_cg: Path = Path("/sys/fs/cgroup"), top: int = 3) -> None:
        self.proc, self.sys_cg, self.top = proc, sys_cg, top
        self.prev: dict[tuple[int, int], int] = {}
        self.prev_t: float | None = None

    def line(self, now: float | None = None) -> str:
        """``[mem] <totals> | <top processes>``; CPU % is the ticks since the previous line over its wall time."""
        now = time.monotonic() if now is None else now
        try:
            tot = totals(self.sys_cg, self.proc)
        except Exception as exc:  # noqa: BLE001 - best effort: the processes still print
            tot = f"totals failed: {type(exc).__name__}"
        procs = scan(self.proc)
        dt_s = now - self.prev_t if self.prev_t is not None else 0.0
        top = []
        for p in sorted(procs, key=lambda p: -p.rss)[:self.top]:
            was = self.prev.get((p.pid, p.start))
            cpu = f" {100 * (p.ticks - was) / CLK_TCK / dt_s:.0f}%" if was is not None and dt_s > 0 else ""
            top.append(f"{p.name} {gb(p.rss)}G{cpu}")
        self.prev = {(p.pid, p.start): p.ticks for p in procs}
        self.prev_t = now
        return " ".join(x for x in ("[mem]", tot, "|" if top else "", ", ".join(top)) if x)


def interval_default() -> float:
    try:
        return float(os.environ.get("STACK_SCHED_MEMLOG_S", "").strip() or 180)
    except ValueError:
        return 180.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--interval", type=float, default=None, help="seconds between lines (default "
                    "STACK_SCHED_MEMLOG_S, else 180); 0 disables")
    ap.add_argument("--top", type=int, default=3, help="processes per line (default 3)")
    ap.add_argument("--count", type=int, default=0, help="stop after this many lines (default 0: until stopped)")
    ap.add_argument("--parent", type=int, default=None, help="exit when this is no longer the parent (default: the "
                    "parent at start; the cycle passes its pid, so a cycle that died before this started counts too)")
    ap.add_argument("--proc", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    ap.add_argument("--cgroup", type=Path, default=Path("/sys/fs/cgroup"), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    interval = interval_default() if a.interval is None else a.interval
    if interval <= 0:
        return 0
    parent = os.getppid() if a.parent is None else a.parent
    if os.getppid() != parent:
        return 0
    sampler = Sampler(a.proc, a.cgroup, a.top)
    n = 0
    try:
        while True:
            try:
                body = sampler.line()
            except Exception as exc:  # noqa: BLE001 - a failed sample is a line, never the end of the log
                body = f"[mem] sample failed: {type(exc).__name__}: {exc}"
            print(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} {body}", flush=True)
            n += 1
            if a.count and n >= a.count:
                return 0
            end = time.monotonic() + interval
            while (left := end - time.monotonic()) > 0:
                time.sleep(min(1.0, left))
                if os.getppid() != parent:  # the cycle died without stopping us: reparented to the subreaper
                    return 0
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:  # the reader is gone (the cycle died, D784): exit 0 and quietly, not 120 at the flush
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
