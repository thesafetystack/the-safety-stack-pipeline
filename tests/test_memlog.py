"""tools/memlog.py (D783, the scheduled cycle's memory log): its line from fake ``/proc`` and cgroup trees, the CPU
delta between lines, missing files, the disabled interval, and its exit when its parent dies. The cycle's start and
stop of it: tests/test_scheduled_run.py."""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.tools

MEMLOG = Path(__file__).parents[1] / "tools" / "memlog.py"
_spec = importlib.util.spec_from_file_location("memlog", MEMLOG)
assert _spec and _spec.loader
ml = importlib.util.module_from_spec(_spec)
sys.modules["memlog"] = ml
_spec.loader.exec_module(ml)

G = 2**30


def proc_entry(root: Path, pid: int, comm: str, argv: list[str], rss_kb: int | None, ticks: tuple[int, int] = (0, 0),
               start: int = 1000) -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(f"Name:\t{comm}\nState:\tS (sleeping)\n"
                              + (f"VmHWM:\t{rss_kb} kB\nVmRSS:\t{rss_kb} kB\n" if rss_kb is not None else ""))
    rest = ["S", "1", str(pid), str(pid), "0", "-1", "4194560", "0", "0", "0", "0", str(ticks[0]), str(ticks[1]),
            "0", "0", "20", "0", "1", "0", str(start), "123", "456"]
    (d / "stat").write_text(f"{pid} ({comm}) {' '.join(rest)}\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """A container: the cgroup at the mount's root, a scheduler, a build, a converter, a kernel thread, tini."""
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.current").write_text(f"{int(21.3 * G)}\n")
    (cg / "memory.max").write_text(f"{32 * G}\n")
    (cg / "memory.peak").write_text(f"{int(31.9 * G)}\n")
    (cg / "memory.stat").write_text(f"anon {int(9.1 * G)}\nfile {12 * G}\nkernel 123\nshmem 0\nfile_dirty 5\n")
    (cg / "memory.swap.current").write_text("0\n")
    (cg / "memory.swap.max").write_text("0\n")
    (cg / "memory.events").write_text("low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\n")
    (cg / "pids.current").write_text("412\n")
    (cg / "pids.max").write_text("1000\n")
    py = "/app/.venv/bin/python"
    proc_entry(proc, 1, "tini", ["/usr/bin/tini", "--", py, "/app/tools/scheduled_run.py", "cycle"], 1024)
    proc_entry(proc, 7, "python", [py, "/app/tools/scheduled_run.py", "cycle", "--test"], 300 * 1024)
    proc_entry(proc, 40, "stack", [py, "/app/.venv/bin/stack", "build", "configs/plans/x.yaml", "--json"],
               int(8.7 * 2**20), (1000, 200))
    proc_entry(proc, 41, "pdftotext", ["pdftotext", "-layout", "a.pdf", "-"], 200 * 1024, (50, 5))
    proc_entry(proc, 2, "kthreadd", [], None)
    (proc / "self").mkdir()
    (proc / "self" / "cgroup").write_text("0::/\n")
    (proc / "meminfo").write_text("MemTotal:       16000000 kB\nMemAvailable:    9000000 kB\n")
    return proc, cg


def test_the_line_from_a_containers_trees(trees: tuple[Path, Path]) -> None:
    proc, cg = trees
    line = ml.Sampler(proc, cg).line(now=100.0)
    assert line == ("[mem] cg 21.3/32.0G peak 31.9 anon 9.1 file 12.0 shmem 0.0 swap 0.0/0.0G oom_kill 1 "
                    "pids 412/1000 | stack-build 8.7G, sched 0.3G, pdftotext 0.2G")


def test_the_line_carries_the_containers_tasks_when_the_cgroup_counts_them(trees: tuple[Path, Path]) -> None:
    """D786: ``pids.current`` of ``pids.max`` (threads and processes; Railway's limit is 1000); ``max`` as written,
    and no part at all without the pids controller."""
    proc, cg = trees
    (cg / "pids.max").write_text("max\n")
    assert " pids 412/max | " in ml.Sampler(proc, cg).line(now=100.0)
    (cg / "pids.current").unlink()
    assert " pids " not in ml.Sampler(proc, cg).line(now=100.0)


def test_cpu_is_the_ticks_since_the_previous_line(trees: tuple[Path, Path]) -> None:
    proc, cg = trees
    s = ml.Sampler(proc, cg)
    assert "%" not in s.line(now=100.0)  # the first line has no previous one
    tck = ml.CLK_TCK
    proc_entry(proc, 40, "stack", ["/app/.venv/bin/python", "/app/.venv/bin/stack", "build"], int(8.7 * 2**20),
               (1000 + 3 * tck, 200 + tck))          # 4 CPU-seconds over 2 s: 200%
    proc_entry(proc, 41, "pdftotext", ["pdftotext"], 200 * 1024, (50, 5), start=2000)  # a new process, same pid
    line = s.line(now=102.0)
    assert line.endswith("| stack-build 8.7G 200%, sched 0.3G 0%, pdftotext 0.2G")


def test_missing_files_are_left_out_and_meminfo_stands_in(trees: tuple[Path, Path], tmp_path: Path) -> None:
    proc, cg = trees
    for f in ("memory.peak", "memory.swap.current", "memory.events", "pids.current"):
        (cg / f).unlink()
    (cg / "memory.max").write_text("max\n")
    (cg / "memory.stat").write_text("anon nonsense\nfile 0\n")
    assert ml.totals(cg, proc) == "cg 21.3G/max file 0.0"
    # no cgroup v2 (or unreadable): /proc/meminfo's used of total and what is available
    assert ml.totals(tmp_path / "absent", proc) == "mem 6.7/15.3G avail 8.6"
    (proc / "meminfo").unlink()
    (proc / "41" / "stat").unlink()     # a process that exits between its status and its stat
    (proc / "7" / "status").unlink()    # ... or before
    assert ml.Sampler(proc, tmp_path / "absent").line() == "[mem] | stack-build 8.7G, tini 0.0G"


def test_a_hosts_root_cgroup_reads_this_process_cgroup(trees: tuple[Path, Path]) -> None:
    proc, root = trees
    own = root / "user.slice" / "x.scope"
    own.mkdir(parents=True)
    (own / "memory.current").write_text(f"{2 * G}\n")
    (root / "memory.current").unlink()  # a host's root cgroup has no memory.current
    (proc / "self" / "cgroup").write_text("0::/user.slice/x.scope\n")
    assert ml.totals(root, proc) == "cg 2.0G"


@pytest.mark.parametrize(("comm", "argv", "want"), [
    ("python3", ["/usr/bin/python3", "-u", "/app/tools/memlog.py", "--interval", "180"], "memlog"),
    ("python", ["python", "-m", "stack.cli", "--verbose", "run", "triage"], "stack-run"),
    ("python", ["python", "-X", "dev", "-m", "pytest", "-q"], "pytest"),
    ("python", ["python", "-c", "print(1)"], "python-c"),
    ("python", ["python"], "python"),
    ("soffice.bin", ["/usr/lib/libreoffice/program/soffice.bin", "--headless"], "soffice.bin"),
    ("MainThread", ["/usr/bin/node", "server.js"], "node"),
    ("python", ["python", "/x/a_very_long_script_name_indeed.py"], "a_very_long_scri"),
])
def test_short_names(comm: str, argv: list[str], want: str) -> None:
    assert ml.short_name(comm, argv) == want


def test_interval_zero_prints_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert ml.main(["--interval", "0"]) == 0
    monkeypatch.setenv("STACK_SCHED_MEMLOG_S", "0")
    assert ml.main([]) == 0
    assert capsys.readouterr().out == ""


def test_count_lines_on_this_machine(capsys: pytest.CaptureFixture[str]) -> None:
    assert ml.main(["--interval", "0.05", "--count", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[mem\] .* \| \S", ln) for ln in lines)


def test_it_exits_when_its_parent_dies(tmp_path: Path) -> None:
    """The cycle died without stopping it (SIGKILL, ``os._exit``), here even before it started: with the cycle's pid
    as ``--parent`` it ends within about a second. Red without ``--parent`` (the parent read at start)."""
    pidfile = tmp_path / "pid"
    script = ("import os, subprocess, sys\n"
              "p = subprocess.Popen([sys.executable, sys.argv[1], '--interval', '60', '--parent', str(os.getpid())],"
              " stdout=subprocess.DEVNULL, start_new_session=True)\n"
              "open(sys.argv[2], 'w').write(str(p.pid))\n"
              "os._exit(0)\n")
    subprocess.run([sys.executable, "-c", script, str(MEMLOG), str(pidfile)], check=True, timeout=30)
    pid = int(pidfile.read_text())

    def alive() -> bool:
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
        except OSError:
            return False

    try:
        deadline = time.monotonic() + 15
        while alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not alive(), f"memlog {pid} outlived its parent"
    finally:
        if alive():  # never leave it behind on a failure
            os.kill(pid, signal.SIGTERM)


def test_it_exits_quietly_when_its_reader_is_gone() -> None:
    """D784: the cycle reads its stdout from a pipe; when that pipe is closed (the cycle died) the next line ends it
    with exit 0 and nothing on stderr. Red without the devnull redirect (exit 120, "Exception ignored ... Broken
    pipe")."""
    p = subprocess.Popen([sys.executable, str(MEMLOG), "--interval", "0.2"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    try:
        assert p.stdout is not None and p.stderr is not None
        assert b"[mem]" in p.stdout.readline()
        p.stdout.close()
        assert p.wait(timeout=15) == 0
        assert p.stderr.read() == b""
    finally:
        if p.poll() is None:  # never leave it behind on a failure
            p.terminate()
            p.wait()
