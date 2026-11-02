"""The stage runner (§3.1, §3.3, §7.6, §7.9; D16, D32, D38): one ``stack run <stage>`` = one run.

This is the REWRITE of v3's ``stack/runner.py`` (removed at T4.5 with the writer and the DB layer;
the rename of this module to ``stack/runner.py`` is deferred, D41). Salvaged from it as a pure
function: :func:`topo_order` (phase precedence as the tiebreak). v3's ``bump_on_drift`` was
deleted (D437: nothing compared config stamps; a non-empty ``bump_on`` is refused at plan load).

**Protocol of one run** (:func:`run_stage`):

1. load the stage module and validate ``--only`` against its ``SUBSTAGES`` (refusals raise
   :class:`StageError` before any run exists);
2. **pins at dispatch** (D372, D373): every ``--in`` (a series directory: its last
   published file; an exact file: that file; ``data/<family>/`` alone is refused) is resolved to
   its version and sorted into inputs or priors by family (:func:`stage_reads`); an empty series
   directory is an empty prior when the stage reads the family as a prior; an answers file and
   its evidence must come from one run (pairs); the directories' last files are noted (each
   output's ``after``). Nothing is selected by plan, build or chain id;
3. (before the pins) refresh the store index and, for an uploading run, recover what an earlier
   crash left local (:func:`stack.store.job.prepare`, D353);
4. mint ``run_id`` and start the run's job (:class:`stack.store.job.RunJob`: the ``running`` json
   at :func:`stack.runs.run_record_path`, ``builds/<build>/runs/<head>.<stage>.json.zst`` for a
   build's run, ``runs/<head>.<stage>.json.zst`` standalone, with its log and marker, PUT when
   ``upload``; D344, D352) and its tick (every 30 s; it runs until the final record);
5. call the module's ``run(ctx)`` (``ctx.blobs`` queues its remote uploads, D407); stage every
   emitted table as ``<dir>/.<name>.tmp`` (the
   destination: its ``--out``, else the directory of the pinned version it extends, else
   ``--series``; the kind: ``--part`` or the line's ``out: {kind: part}``, else full, a part the
   stage emits being applied to its version first, :func:`overlay`; one run-wide name stamp,
   :func:`stack.store.layout.tie_guard`)
   (``chain.stage_file``: the family writer runs S1-S7) with the header the runner fills;
6. wait for the run's blob uploads (``ctx.blobs.flush()``; a failed upload fails the run
   ``code_bug``, ``s3_transient`` for a transient S3 error, before anything is renamed, D407);
   ``chain.publish(staged, on_published)``: the cancel check, the publish rule (``claim``:
   ``head_moved``, ``name_order``) over a refreshed index, the renames, the data PUTs
   (``If-None-Match: *``; a transient S3 error is retried, :data:`stack.store.chain.DATA_PUT_RETRY_DELAYS`), then the final ``ok``
   record **inside** the hook (:meth:`stack.store.job.Job.finish`: tick joined, json local, json
   and log PUT, marker deleted; the commit, D345, D355). No locks (§6.6);
7. any failure writes a final ``failed[<error_class>]`` record (``head_moved`` for a refused
   publish, ``name_order`` for a name that sorts too early, ``environment`` for an :class:`EnvironmentRefused`, ``tick_failed`` when the start
   upload failed, ``s3_transient`` for a transient S3 failure (:func:`stack.store.s3config.is_s3_transient`,
   its error redacted; S25-47), ``code_bug`` for any other exception) and leaves nothing a record names in
   ``data/``; staged files stay as ``data/<family>[/<series>]/.<name>.tmp`` (garbage). Ticks failing for 2.5 min end
   the run ``cancelled[tick_failed]`` (:class:`stack.store.job.JobCancelled`, raised where the
   stage reports progress or before ``claim``; D354). ``KeyboardInterrupt`` writes
   ``cancelled`` and re-raises.

**Stage protocol.** A stage ``a.b`` lives in ``workflows/a/b/main.py``, a bare ``a`` in
``workflows/a/main.py`` (loaded by path, as v3 did); ``fold.<config>`` dispatches to the adapter
:mod:`stack.fold.stage` (T1.6; ``fold.release`` is refused before dispatch: it writes
``releases/<tag>/``, T5.2). The
module exposes::

    SUBSTAGES: tuple[str, ...] = (...)        # optional; names `--only` accepts (D32)
    PURE_SUBSTAGES: tuple[str, ...] = (...)   # optional; always run, even under `--only`
    OPTIONS: frozenset[str] = frozenset(...)  # optional; the `options` keys its code reads (D434)
    def run(ctx: StageContext) -> StageResult: ...

A module declaring ``OPTIONS`` has every other key of its line's ``options`` refused
(:func:`check_options`): a :class:`StageError` before any run record exists (D472; a standalone run
is refused as a build is), and at a build's start (:func:`stack.build.preflight`: ``stage_refused``
before its first run). A misspelt option would otherwise do nothing.

:class:`StageContext` gives ``run_id``, ``stage``, ``plan``, ``plan_stage`` (the stage line, a bare
one if the plan does not list the stage), ``store_root``, ``inputs``/``prior`` (key → pinned
``Chain``; the key is the family, or ``<family>/<family dir>`` when one family is given twice;
``Chain.chain_id`` is the family directory),
``view(key)`` (the chain view, cached), ``filter``, ``only``, ``counts`` (a dict the stage fills),
``config_digest`` (default: the stage line's digest; the stage may set its own before
returning), ``prompt_pin`` (``None``; ``annotate.<wf>`` sets the resolved pin, copied to the run
record, R1-1), ``selection`` (``None``; ``annotate.<wf>`` and ``triage`` set the line's target, accepted
configs and selection digest, copied to the run record, D758), ``blobs`` (the store's blobs, :func:`stack.store.blobs.open_blobs`), ``emit_part(family, table,
blocks_written, *, chain_id=None)`` / ``emit_full(family, table, blocks_written='*', *,
chain_id=None)`` (one output per family, ``chain_id`` naming the family directory;
``writes_part(family, *, chain_id=None)`` says whether ``emit_part`` would write a part; the runner
fills the header: stage, writer, run_id, plan, inputs, prior, only, config_digest, code_digest,
scope_complete, series, base = the destination's file the run read, after),
``heartbeat(inflight_keys=None)`` (rewrites the local ``running`` json; with keys, replaces
``inflight`` first, D19; raises :class:`stack.store.job.JobCancelled` when the run's ticks gave
up) and ``substage(name)`` (a context manager yielding a :class:`Substage`
whose ``active`` says whether to do the work under ``--only``; on exit it records ``{ran,
scope_complete}`` and rewrites the record). ``run`` returns :class:`StageResult(scope_complete,
counts, rows_touched, error)`; a planned stop is ``scope_complete=False`` with status ``ok`` (§3.3); ``error``
(D583) is the reason an ``ok`` run stopped short when its counts cannot say it (the record's ``error``).

**Drain** (D570, D571; :mod:`stack.store.job`, "Drain"): ``ctx.drain`` (a :class:`DrainCheck`) says
whether the process is draining (``stopping()``: admit no new row, role, call, window, page or
batch) and until when in-flight items may finish (``deadline()``, ``time.monotonic()``; after it they
are abandoned as not attempted, their threads left to end on their own timeouts:
:class:`DrainPool`). A stage that saw ``stopping()`` true ends its run ``ok`` with ``scope_complete =
false`` and ``counts["stopped.drain"]`` (the runner sets both; build reason ``stopped``), publishing
what it completed exactly as at a normal end (blobs flushed first, D407). A stage may hand a drain
back by raising :class:`StageStopped` (the collectors' ``RunStopped``) after registering
``ctx.on_stopped`` (a ``() -> StageResult`` that emits what is done; the collector harness's
``finish``); without one the raise is a ``code_bug``. Stages without checks (gather, the folds)
run to their end; the build then starts nothing more.
The runner adds ``counts.llm_calls`` (the LLM transport requests of the run's clients,
:func:`stack.llm.run_calls`) to a run that opened an LLM client, so every stage's calls are
counted in one key (passes, ``stack build --passes``).
A stage may also set ``ctx.writer`` (the file name's and record's ``writer``; default the stage)
and ``ctx.fold`` (a ``FoldInfo``: the run record's ``fold`` entry and the header's fold keys);
the fold adapter does both.

:func:`run_recorded` runs a callable that publishes through its own driver as one run with the
same job (running json, tick, final ``ok`` written in the publish hook, ``failed`` / ``cancelled``
otherwise); ``stack fold`` uses it around :func:`stack.fold.run_fold` (T1.6).

**Crash injection** (crash tests only): env ``STACK_CRASH_AT`` ∈ :data:`CRASH_POINTS`
(:mod:`stack.store.job`) SIGKILLs the process at that point of a run's end. Unset in every real
run; an unknown value is refused.

``code_digest`` = sha256 over ``(path, sha256(bytes))`` of every ``*.py`` under ``stack/`` and
``workflows/``, sorted by path (proposed in T1.3; a dirty tree is visible, no git needed).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import functools
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack.collect.sampling import cap_env_set
from stack.core import ids
from stack.plan import Plan, StageLine
from stack.runs import INFLIGHT_CAP, FoldInfo, InputFile, OutputInfo, RunRecord, VersionInfo
from stack.schemas.families import FAMILIES
from stack.schemas.families import family as family_spec
from stack.store import chain as chain_mod
from stack.store.blobs import BlobStore, open_blobs
from stack.store.chain import (
    CHAIN_OF_ONE_KINDS,
    Chain,
    HeaderFields,
    HeadMovedError,
    NameOrderError,
    Published,
    StagedFile,
    head_of,
    publish,
    read_view,
    resolve,
    stage_file,
)
from stack.store.job import (
    CRASH_ENV,
    CRASH_POINTS,
    JobCancelled,
    JobStartError,
    RunJob,
    crash_point,
    default_upload,
    drain_state,
    own_line,
    prepare,
    say,
)
from stack.store.layout import (
    COMMENT_RE,
    DATA,
    NONE,
    WILDCARD,
    check_series,
    family_of_dir,
    make_data_file,
    parse_dir_ref,
    series_dir,
    tie_guard,
)
from stack.store.index import store_index
from stack.store.remote import open_store
from stack.store.s3config import is_s3_transient, redacted_trace

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_ROOT = REPO_ROOT / "workflows"
#: Env var naming another workflows root (T5.1a: the build scenario's stand-in stages under
#: ``tests/synth/stages/``); read at every load, ``fold.*`` still dispatches to ``stack.fold``.
WORKFLOWS_ROOT_ENV = "STACK_WORKFLOWS_ROOT"
#: v4 phase order: the tiebreak of :func:`topo_order` (§0's chain, §3.4).
PHASE_ORDER: tuple[str, ...] = ("overrides", "collect", "gather", "triage", "acquire", "fold.corpus", "annotate",
                                "fold.build", "fold.release", "fold.compact")
_SEG_RE = re.compile(r"[a-z][a-z0-9_]*")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


class StageError(Exception):
    """A run refused before dispatch (unknown stage, bad ``--only``, input not at head, …), or a
    stage that broke the protocol (two outputs for one family, a part with no base, …)."""


class EnvironmentRefused(StageError):
    """Raised by a stage's ``run(ctx)`` when it cannot run correctly in this environment (a tool
    of its config is not installed: acquire's convert pin guard, R4-1). The run ends
    ``failed[environment]`` with the message (no traceback), and a build passes the class through."""


def phase_of(stage: str) -> str:
    """``collect.arxiv`` → ``collect``; folds keep their config (``fold.corpus``)."""
    return stage if stage.startswith("fold.") else stage.split(".", 1)[0]


def topo_order(stages: Sequence[str], requires: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """Order ``stages`` dependency-first by ``requires`` (edges within the set only), with
    :data:`PHASE_ORDER` then list position as the tiebreak (v3 ``runner.py:895-933``). Raises
    :class:`StageError` on a cycle."""
    requires = requires or {}
    start = list(dict.fromkeys(stages))
    inset = set(start)

    def rank(s: str) -> int:
        p = phase_of(s)
        return PHASE_ORDER.index(p) if p in PHASE_ORDER else len(PHASE_ORDER)

    roots = sorted(start, key=lambda s: (rank(s), start.index(s)))
    state: dict[str, str] = {}
    order: list[str] = []

    def visit(s: str) -> None:
        st = state.get(s)
        if st == "done":
            return
        if st == "active":
            raise StageError(f"'requires' cycle involving {s!r}")
        state[s] = "active"
        for dep in sorted(requires.get(s, ()), key=lambda d: (rank(d), d)):
            if dep in inset:
                visit(dep)
        state[s] = "done"
        order.append(s)

    for s in roots:
        visit(s)
    return order


def plan_order(plan: Plan) -> list[str]:
    return topo_order([s.stage for s in plan.stages])


# ---------------------------------------------------------------- code digest
_CODE_BASE = REPO_ROOT


@functools.cache
def code_digest(roots: tuple[str, ...] = ("stack", "workflows"), base: Path = _CODE_BASE) -> str:
    """sha256 over ``(path relative to base, sha256(bytes))`` of every ``*.py`` under
    ``base/<root>``, sorted by path. Computed once per process."""
    h = hashlib.sha256()
    for r in roots:
        for p in sorted((base / r).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(p.relative_to(base).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


# ---------------------------------------------------------------- stage modules
FOLD_STAGE_MODULE = "stack.fold.stage"


def stage_module_path(stage: str) -> Path:
    """``a.b`` → ``workflows/a/b/main.py``, ``a`` → ``workflows/a/main.py``; ``fold.<config>`` →
    the adapter ``stack/fold/stage.py``. ``workflows/`` is ``$STACK_WORKFLOWS_ROOT`` when set."""
    if stage.startswith("fold."):
        return REPO_ROOT / "stack" / "fold" / "stage.py"
    phase, _, name = stage.partition(".")
    if not _SEG_RE.fullmatch(phase) or (name and not _NAME_RE.fullmatch(name)):
        raise StageError(f"stage {stage!r}: expected `<phase>` or `<phase>.<name>` slugs")
    env = os.environ.get(WORKFLOWS_ROOT_ENV)
    root = Path(env) if env else WORKFLOWS_ROOT
    return root / phase / name / "main.py" if name else root / phase / "main.py"


def _load_fold_stage(stage: str) -> ModuleType:
    """``fold.<config>``: refuse an unknown config (:class:`StageError`) or one M1 does not
    implement (``NotImplementedError``) before any run exists; else the adapter module."""
    from stack.fold.configs import FOLD_CONFIGS

    config = stage.removeprefix("fold.")
    spec = FOLD_CONFIGS.get(config)
    if spec is None:
        raise StageError(f"stage {stage!r}: no fold config {config!r} (configs {list(FOLD_CONFIGS)})")
    if not spec.implemented:
        raise NotImplementedError(f"stage {stage!r} is not implemented; implemented: "
                                  f"{[f'fold.{c}' for c, s in FOLD_CONFIGS.items() if s.implemented]}")
    if spec.output == "release_dir":  # T5.2: releases/<tag>/ is not a family file
        raise StageError(f"stage {stage!r} is not a stage: use `stack fold release` or `stack release`")
    return importlib.import_module(FOLD_STAGE_MODULE)


def load_path_module(modname: str, path: Path) -> ModuleType | None:
    """The module at ``path`` executed as ``modname`` (``workflows/`` is not a package), registered in
    ``sys.modules`` first, as an import would be: ``@dataclass`` under ``from __future__ import
    annotations`` looks its module up there (L1-8). A later load of the same name replaces the
    entry; a failed one removes it. ``None``: no module spec for ``path``."""
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    return module


def load_stage_module(stage: str) -> ModuleType:
    if stage.startswith("fold."):
        return _load_fold_stage(stage)
    path = stage_module_path(stage)
    if not path.exists():
        raise StageError(f"stage {stage!r}: no module at {path}")
    modname = "_stack_stage__" + stage.replace(".", "__").replace("-", "_")
    module = load_path_module(modname, path)
    if module is None:
        raise StageError(f"stage {stage!r}: cannot load a module spec at {path}")
    if not callable(getattr(module, "run", None)):
        raise StageError(f"stage {stage!r}: {path} must expose run(ctx) -> StageResult")
    return module


def check_options(stage: str, options: Mapping[str, Any], known: Iterable[str] | None) -> None:
    """Refuse an ``options`` key of ``stage``'s line outside ``known`` (the stage module's
    ``OPTIONS``, D434), naming the closest known key; ``known`` None (no ``OPTIONS``): no check."""
    if known is None:
        return
    known = sorted(known)
    unknown = sorted(set(options) - set(known))
    if unknown:
        hints = [f"{k!r} (did you mean {m[0]!r}?)" if (m := difflib.get_close_matches(k, known, n=1)) else repr(k)
                 for k in unknown]
        raise StageError(f"{stage}: unknown options {', '.join(hints)}; known: {known}")


def _validate_only(stage: str, module: ModuleType, only: Sequence[str] | None) -> list[str] | None:
    if only is None:
        return None
    names = list(dict.fromkeys(only))
    known = tuple(getattr(module, "SUBSTAGES", ()))
    if not known:
        raise StageError(f"stage {stage!r} declares no SUBSTAGES; `--only` is not accepted")
    bad = [n for n in names if n not in known]
    if bad:
        raise StageError(f"stage {stage!r}: --only {bad} not in its substages {list(known)} (D32)")
    return names


# ---------------------------------------------------------------- reads, pins, destinations (D373)
#: Collectors that read the ``documents`` head besides the ``*-lookup`` ones (round 4, L2).
DOCUMENT_COLLECTORS: tuple[str, ...] = ("collect.links",)


def stage_reads(stage: str, line: StageLine) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(input families, prior families)`` a stage reads by default (§3.1's "Reads" column,
    the build's read set, D378). A ``--in`` of a prior family is a prior (``ctx.prior``),
    any other an input (``ctx.inputs``); a stage that reads a family may extend it, so a
    base-less full into a non-empty directory of it needs ``--restart`` ([r] R5)."""
    phase = phase_of(stage)
    if phase == "collect":
        docs = ("documents",) if stage.endswith("-lookup") or stage in DOCUMENT_COLLECTORS else ()
        ovr = ("overrides",) if stage == "collect.manual" else ()
        return docs + ovr, tuple(dict.fromkeys(line.prior or ["sources", "coverage"]))
    if phase == "gather":
        return ("sources", "documents", "overrides"), ()
    if phase == "triage":  # D527: title_basis also reads the sources chains (query labels)
        docs = ("documents", "sources") if line.options.get("title_basis") is True else ("documents",)
        return docs, ("triage_evidence",)
    if stage in ("fold.corpus", "fold.compact") or phase in ("acquire",):
        return ("documents",), ()
    if stage == "fold.build":
        return ("corpus",), ()
    if phase == "annotate":
        wf = stage.removeprefix("annotate.")
        return ("corpus", *line.reads, "overrides"), tuple(dict.fromkeys(line.prior or [wf, f"{wf}_evidence"]))
    if phase == "overrides":
        return (), ("overrides",)
    return ("documents",), ()


def restart_guarded(stage: str, line: StageLine) -> frozenset[str]:
    """The output families the restart guard covers ([r] R5, ruling 1): those the stage reads by
    default (:func:`stage_reads`; D373: keyed on the read set, not the stage name).
    fold.corpus reads documents and writes corpus: unguarded by its read set. fold.build reads this
    build's corpus file, never its output directory: exempt. fold.compact reads documents: guarded
    (an ``--out`` into another non-empty documents directory needs ``--restart``)."""
    if stage == "fold.build":  # its corpus read is this build's file, never its output directory
        return frozenset()
    ins, pri = stage_reads(stage, line)
    return frozenset((*ins, *pri))


def new_series_refusal(store_root: str | Path, series: str | None) -> str | None:
    """The series guard (A1, D491): why a build or a collector run into ``series`` would start a new identity,
    ``None`` when it would not. Ids are minted per series (a collector reuses only its own chain's ``source_id``
    of a ``(channel, external_id)``, :meth:`stack.collect.base.Collector.source_id`; a ``doc_id`` is its anchor
    member's ``source_id``), so a series with no ``documents`` file re-mints every id and re-pays every collect,
    triage, fetch and LLM call. Refused when ``series`` has no published ``documents`` file while another
    series of the store has one; not refused into a series that has ``documents``, in a store where no series
    has, or without a series. The callers' flag is ``--new-series``."""
    if series is None:
        return None
    have = [d.split("/")[2] for d in store_index(store_root).series_dirs("documents")]
    if not have or series in have:
        return None
    return (f"series {series!r} has no documents while {', '.join(repr(s) for s in have)} "
            f"{'has' if len(have) == 1 else 'have'}: ids are minted per series, so writing into it re-mints every "
            "source_id and doc_id and re-pays every collect, triage, fetch and LLM call (D491); pass --new-series "
            "to start a new series on purpose, or --series one of those")


def env_limits_refusal(env: Mapping[str, str] | None = None) -> str | None:
    """The env-limits guard (D553, S25-24): why a build or a ``collect.*`` run would be capped or
    sampled by the environment, ``None`` when no cap/sampling variable is set
    (:func:`stack.collect.sampling.cap_env_set`: ``STACK_*_LIMIT``, ``*_FRACTION``, ``*_MAX_QUERIES``,
    non-empty). Each one beats the plan's option and no digest sees it, so a leftover from a smoke
    test silently truncates a full run. The callers' flag is ``--allow-env-limits``; with it the
    values are recorded (the record's ``env_limits``)."""
    caps = cap_env_set(env)
    if not caps:
        return None
    return ("collector cap/sampling environment variables are set (" + ", ".join(f"{k}={v}" for k, v in caps.items())
            + "): each beats the plan's options and truncates or samples the collectors without a trace in any "
            "digest (D553); unset them, or pass --allow-env-limits to run capped on purpose (the values are then "
            "recorded as env_limits)")


def overrides_refusal(store_root: str | Path, stage: str, series: str | None) -> str | None:
    """The overrides guard (A9, D495): why a run of ``stage`` (one that reads ``overrides``: gather,
    ``annotate.<wf>``, ``collect.manual``) into ``series`` without them would silently undo that series' human
    corrections, ``None`` when it would not (no series, or ``data/overrides/<series>/`` has no published file).
    The caller asks only when the run pins no ``overrides`` input; the flag is ``--no-overrides``."""
    if series is None:
        return None
    d = series_dir("overrides", series)
    head = head_of(store_root, d)
    if not head:
        return None
    return (f"{stage}: {d}/ has overrides ({head}) and this run reads none: it would undo every merge, mask and "
            f"field override of series {series!r} until the next build re-applies them (D495); pass --in {d}/, "
            "or --no-overrides to run without them on purpose")


@dataclass(frozen=True)
class OutSpec:
    """One ``--out`` (D373): a destination directory ``data/<family>/<series>`` or
    ``data/<family>`` (no series: a full only), with the run's ``--part``/``--full``,
    ``--comment`` and ``--restart`` applied by :func:`out_specs`."""

    family_dir: str
    series: str | None
    kind: str | None = None  # full | part | None (the default: full)
    restart: bool = False

    @property
    def dir(self) -> str:
        return series_dir(self.family_dir, self.series)

    @property
    def family(self) -> str:
        return family_of_dir(self.family_dir)


def out_specs(outs: Iterable[str | OutSpec], *, series: str | None = None, kind: str | None = None,
              restart: bool = False) -> list[OutSpec]:
    """``--out`` paths (or specs) with the run-level ``--series``, ``--part|--full`` and
    ``--restart``: a path naming a series plus a different ``--series`` is refused ([r] R11);
    a path without a series takes ``--series``; a part needs a series."""
    out: list[OutSpec] = []
    for o in outs:
        if isinstance(o, OutSpec):
            spec = o
        else:
            parts = [p for p in str(o).strip().split("/") if p]
            if len(parts) not in (2, 3) or parts[0] != DATA:
                raise StageError(f"--out {o!r}: expected {DATA}/<family>/[<series>/]")
            try:
                family_of_dir(parts[1])
                s = check_series(parts[2]) if len(parts) == 3 else None
            except ValueError as exc:
                raise StageError(f"--out {o!r}: {exc}") from None
            if s is not None and series is not None and s != series:
                raise StageError(f"--out {o!r} names series {s!r} but --series is {series!r}")
            spec = OutSpec(parts[1], s if s is not None else series)
        spec = dataclasses.replace(spec, kind=spec.kind or kind, restart=spec.restart or restart)
        if spec.kind == "part" and spec.series is None:
            raise StageError(f"--out {spec.dir}/: a file without a series is always full (no --part)")
        if any(x.family_dir == spec.family_dir for x in out):
            raise StageError(f"--out: two destinations for {spec.family_dir}")
        out.append(spec)
    return out


@dataclass(frozen=True)
class _EmptyDir:
    """A series directory given as ``--in`` with no published file yet: an empty prior."""

    family_dir: str
    dir: str

    @property
    def family(self) -> str:
        return family_of_dir(self.family_dir)


def pin_input(store_root: str | Path, ref: str) -> Chain:
    """Resolve one ``--in`` (D373): a series directory (its last published file's version),
    an exact file (that file's version) or a file without a series; ``data/<family>/`` alone is
    refused. :class:`StageError` for a reference that does not resolve."""
    try:
        return resolve(store_root, ref)
    except chain_mod.EmptyChainError:
        raise
    except chain_mod.ChainError as exc:
        raise StageError(f"--in {ref}: {type(exc).__name__}: {exc}") from None


def _pin_all(root: Path, stage: str, line: StageLine, refs: Iterable[str]
             ) -> tuple[dict[str, Chain], dict[str, Chain], list[_EmptyDir]]:
    """``(inputs, prior, empty prior dirs)``: each ``--in`` resolved and sorted by family into
    inputs or priors (:func:`stage_reads`); keys are the family, ``<family>/<family dir>`` when a
    family is given twice. A series directory with no file is an empty prior when its family is
    one the stage reads as a prior, else refused ([r] R11)."""
    _, prior_fams = stage_reads(stage, line)
    pinned: list[Chain] = []
    empty: list[_EmptyDir] = []
    for ref in dict.fromkeys(refs):
        try:
            ch = pin_input(root, ref)
        except chain_mod.EmptyChainError as exc:
            if ref.endswith(".parquet"):
                raise StageError(f"--in {ref}: {exc}") from None
            fam_dir, series = parse_dir_ref(ref)
            if family_of_dir(fam_dir) not in prior_fams:
                raise StageError(f"--in {ref}: no published file, and {stage} reads "
                                 f"{family_of_dir(fam_dir)} as an input, not as a prior it may start") from None
            empty.append(_EmptyDir(fam_dir, series_dir(fam_dir, series)))
            continue
        pinned.append(ch)
    dirs = [c.chain_id for c in pinned] + [e.family_dir for e in empty]
    twice = sorted({d for d in dirs if dirs.count(d) > 1})
    if twice:
        raise StageError(f"--in: {twice} given twice")
    fams = [c.family for c in pinned] + [e.family for e in empty]
    inputs: dict[str, Chain] = {}
    prior: dict[str, Chain] = {}
    for c in pinned:
        key = c.family if fams.count(c.family) == 1 else f"{c.family}/{c.chain_id}"
        (prior if c.family in prior_fams else inputs)[key] = c
    _check_pairs([*inputs.values(), *prior.values()], empty)
    return inputs, prior, empty


def _check_pairs(chains: Sequence[Chain], empty: Sequence[_EmptyDir] = ()) -> None:
    """An answers file and the evidence file read with it come from one run (D373).
    A requested empty prior directory counts: one side of a pair empty and the other not is
    refused (an orphaned half, S25-49); both empty is a first run."""
    by_fam = {c.family: c for c in chains}
    gone = {e.family: e for e in empty}
    for fam in {*by_fam, *gone}:
        if fam.endswith("_evidence") or fam not in FAMILIES or family_spec(fam).kind != "wf":
            continue
        ev_fam = f"{fam}_evidence"
        c, ev = by_fam.get(fam), by_fam.get(ev_fam)
        if c is None or ev is None:
            full, missing = (c, ev_fam) if ev is None else (ev, fam)
            if full is not None and missing in gone:
                raise StageError(f"pair {fam} + {ev_fam}: {full.head_key} (run {full.files[-1].run_id}) is read "
                                 f"with {gone[missing].dir}/, which has no published file; orphan or restore "
                                 "the other half too")
            continue
        if c.files[-1].run_id != ev.files[-1].run_id:
            raise StageError(f"pair {fam} + {fam}_evidence: {c.head_key} (run {c.files[-1].run_id}) and "
                             f"{ev.head_key} (run {ev.files[-1].run_id}) are not from one run")


def overlay(view: pa.Table, part: pa.Table, key: Sequence[str]) -> pa.Table:
    """``view`` with ``part``'s rows replacing those of the same key (union-and-select of a part
    over its base, §7.3), ordered by key: the full file a part would make (``--full`` over a
    stage that emits a part)."""
    cols = list(key)
    idx = view.select(cols).append_column("__i", pa.array(range(view.num_rows), pa.int64()))
    keep = idx.join(part.select(cols), keys=cols, join_type="left anti")["__i"]
    out = pa.concat_tables([view.take(keep), part.select(view.schema.names)])
    return out.take(pc.sort_indices(out, [(k, "ascending") for k in cols])).combine_chunks()


# ---------------------------------------------------------------- the context
class StageStopped(Exception):  # noqa: N818 — a stop, not an error
    """A drained stage hands its run back by raising this (module docstring, Drain); the runner
    calls ``ctx.on_stopped`` for its result."""


def _never() -> bool:
    return False


def _no_deadline() -> float | None:
    return None


@dataclass(frozen=True)
class DrainCheck:
    """What a stage asks of a drain (module docstring, Drain; D570, D571): ``stopping()`` = admit
    no new work; ``deadline()`` = the ``time.monotonic()`` after which in-flight items are abandoned
    as not attempted (``None``: no drain). :data:`NO_DRAIN` never stops (library callers)."""

    stopping: Callable[[], bool] = _never
    deadline: Callable[[], float | None] = _no_deadline

    def expired(self) -> bool:
        """The bounded wait is over (a drain, past its deadline; the stage has then seen the drain)."""
        at = self.deadline()
        return at is not None and time.monotonic() >= at and self.stopping()

    def remaining(self, cap: float) -> float:
        """Seconds to wait for in-flight work now: ``cap`` without a drain, else at most what is left
        of the bounded wait (0 once expired)."""
        at = self.deadline()
        return cap if at is None else max(0.0, min(cap, at - time.monotonic()))


NO_DRAIN = DrainCheck()


#: The cgroup v2 mount and this process's cgroup line, read for the container's task limit
#: (:func:`thread_room`); tests point them at a temporary directory.
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")
#: Tasks (threads and processes) a stage pool leaves to the rest of the container: the scheduler, the
#: memory log, a converter's children, the job's tick and upload threads, library pools (D786).
THREAD_RESERVE = 128
#: An absolute ceiling on each stage pool's threads (a positive integer; unset or empty: none), for a host
#: whose task limit the container cannot read, e.g. a parent cgroup's ``pids.max`` while the container's
#: own shows ``max`` (D786). Read when a pool is made; in no plan, line or config digest.
POOL_MAX_THREADS_ENV = "STACK_POOL_MAX_THREADS"


def _read_tasks(path: Path) -> int | None:
    """An integer cgroup file (``pids.max``, ``pids.current``); ``None`` for ``max`` or unreadable."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def thread_room() -> int | None:
    """How many more tasks (threads and processes) the cgroup limits admit now: the least
    ``pids.max - pids.current`` over ``/sys/fs/cgroup`` (a container's own cgroup) and, on a host, the
    process's cgroup from ``/proc/self/cgroup`` and its ancestors (each limit applies). ``None`` when
    nothing limits or nothing can be read (no cgroup v2; ``max`` everywhere, as a host's root)."""
    dirs = [CGROUP_ROOT]
    try:
        own = next((ln[4:].strip() for ln in PROC_CGROUP.read_text().splitlines() if ln.startswith("0::/")), "")
    except OSError:
        own = ""
    parts = [p for p in own.split("/") if p]
    dirs += [CGROUP_ROOT.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]
    rooms = [top - cur for d in dirs
             if (top := _read_tasks(d / "pids.max")) is not None and (cur := _read_tasks(d / "pids.current")) is not None]
    return min(rooms) if rooms else None


def pool_ceiling() -> int | None:
    """``$STACK_POOL_MAX_THREADS`` (:data:`POOL_MAX_THREADS_ENV`) as an int, ``None`` when unset or empty;
    anything else raises (a pool is not made on a misread ceiling)."""
    raw = os.environ.get(POOL_MAX_THREADS_ENV, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise StageError(f"{POOL_MAX_THREADS_ENV} must be a positive integer, got {raw!r}")
    return int(raw)


class DrainPool(ThreadPoolExecutor):
    """A stage's worker pool whose ``with`` exit does not wait for calls a drain abandoned (D571):
    the stage sets ``abandoned``; queued calls are cancelled, running ones end on their own timeouts
    and their results are dropped (the process may wait for them at exit; a second SIGTERM ends it).

    **Bounded by the container** (D786): ``workers`` is ``max_workers`` capped at
    ``$STACK_POOL_MAX_THREADS`` (:func:`pool_ceiling`) and at the cgroup's task room less
    :data:`THREAD_RESERVE` (:func:`thread_room`); at least 1. So a 1024-worker line does not take a
    container's last tasks (Railway's ``pids.max`` is 1000). Where a thread still cannot be started
    (a limit read by neither), the pool keeps the threads it has and caps ``workers`` there: the call
    stays queued and runs on them. That pool has taken the last free tasks and holds them until it
    ends, so another thread or process the container starts meanwhile can fail too; the ceiling is
    what prevents it where the limit cannot be read. A cap says one line in the run's log, when the
    pool starts its first thread (an empty queue says nothing). ``requested`` keeps ``max_workers``,
    ``started`` counts the threads started. A pool that cannot start its first thread raises, as
    before. Stages admit up to ``workers``."""

    abandoned = False

    def __init__(self, max_workers: int, thread_name_prefix: str = "") -> None:
        room, ceiling = thread_room(), pool_ceiling()
        self.requested = n = max_workers
        why = []
        if ceiling is not None and ceiling < n:
            n = ceiling
            why.append(f"{POOL_MAX_THREADS_ENV}={ceiling}")
        if room is not None and room - THREAD_RESERVE < n:
            n = max(1, room - THREAD_RESERVE)
            why.append(f"the container admits {room} more tasks, {THREAD_RESERVE} kept for the rest")
        self.workers = n
        self._note = (f"{thread_name_prefix or 'pool'}: at most {n} worker threads, not {max_workers}: "
                      f"{'; '.join(why)} (D786)") if why else None
        super().__init__(max_workers=n, thread_name_prefix=thread_name_prefix)

    @property
    def started(self) -> int:
        """The worker threads this pool started (a thread ends only at shutdown)."""
        return len(self._threads)

    def _adjust_thread_count(self) -> None:
        if self._note is not None and not self._threads:
            say(self._note)  # the cap matters once the pool starts threads
            self._note = None
        try:
            super()._adjust_thread_count()  # CPython: starts a thread while fewer than _max_workers, none idle
        except RuntimeError as exc:  # "can't start new thread": the item is queued, the thread not added
            started = len(self._threads)
            if started == 0:
                raise
            self._max_workers = self.workers = started
            say(f"{self._thread_name_prefix or 'pool'}: {started} worker threads, not {self.requested}: "
                f"starting another failed ({exc}); the pool goes on with {started} and holds them until "
                f"it ends (D786)")

    def __exit__(self, *exc: object) -> None:
        self.shutdown(wait=not self.abandoned, cancel_futures=self.abandoned)


@dataclass
class StageResult:
    """What ``run(ctx)`` returns. ``scope_complete=False`` = a planned stop (status stays ``ok``).
    ``rows_touched`` defaults to the row count of the run's chain file (D19's definition is the
    stage's to compute when it can). ``error``: why an ``ok`` run stopped short, when that is not
    in its counts alone (a collector halted by a provider refusal, D583); it becomes the record's
    ``error`` (the status stays ``ok``, no ``error_class``)."""

    scope_complete: bool = True
    counts: dict[str, int] = field(default_factory=dict)
    rows_touched: int | None = None
    error: str | None = None


@dataclass
class Substage:
    name: str
    active: bool
    scope_complete: bool = True


@dataclass
class _Emitted:
    family: str
    table: pa.Table
    blocks_written: Sequence[str] | str
    dest: OutSpec
    kind: str  # the file's kind
    base: str
    after: str


class StageContext:
    """What a stage module's ``run(ctx)`` sees (the module docstring lists the members)."""

    def __init__(self, *, store_root: Path, run_id: str, stage: str, plan: Plan, plan_stage: StageLine,
                 inputs: dict[str, Chain], prior: dict[str, Chain], filter: str | None,
                 only: list[str] | None, pure: Sequence[str], provisional: RunJob,
                 config_digest: str, outs: Sequence[OutSpec] = (), empty: Sequence[_EmptyDir] = (),
                 series: str | None = None, kind: str | None = None, restart: bool = False,
                 lasts: Mapping[str, str] | None = None) -> None:
        self.store_root = store_root
        self.run_id = run_id
        self.stage = stage
        self.plan = plan
        self.plan_stage = plan_stage
        self.inputs = inputs
        self.prior = prior
        self.filter = filter
        self.only = only
        self.counts: dict[str, int] = {}
        self.config_digest = config_digest
        self.prompt_pin: dict[str, Any] | None = None
        self.selection: dict[str, Any] | None = None
        self.writer = stage
        self.fold: FoldInfo | None = None
        self._pure = tuple(pure)
        self._prov = provisional
        self._views: dict[str, pa.Table] = {}
        self._emitted: list[_Emitted] = []
        self._blobs: BlobStore | None = None
        self._outs = list(outs)
        self._empty = list(empty)
        self._series = series
        self._kind = kind
        self._restart = restart
        #: the last published file of each destination or pinned directory at pin time ([r] R4)
        self._lasts: dict[str, str] = dict(lasts or {})
        #: the stage saw a drain (module docstring, Drain): the run ends scope-incomplete, ``stopped.drain``
        self.stopped = False
        #: ``() -> StageResult`` after a :class:`StageStopped` (the collector harness registers its finish)
        self.on_stopped: Callable[[], StageResult] | None = None
        self.drain = DrainCheck(self._stopping, self._drain_deadline)

    def _stopping(self) -> bool:
        if drain_state() is None:
            return False
        self.stopped = True
        return True

    @staticmethod
    def _drain_deadline() -> float | None:
        d = drain_state()
        return None if d is None else d.deadline

    @property
    def blobs(self) -> BlobStore:
        if self._blobs is None:  # deferred: the runner flushes it before the publish (D407)
            self._blobs = open_blobs(self.store_root, deferred=True)
        return self._blobs

    def _flush_blobs(self) -> None:
        """Wait for the run's blob uploads; the first failure raises (D407)."""
        if self._blobs is not None:
            self._blobs.flush()

    def _close_blobs(self) -> None:
        """Stop the run's upload workers (bounded, :data:`stack.store.blobs.CLOSE_TIMEOUT`)."""
        if self._blobs is not None:
            self._blobs.close()

    def view(self, key: str) -> pa.Table:
        """The union-and-select view (§7.3) of the pinned input or prior ``key``."""
        if key not in self._views:
            ch = self.inputs.get(key) or self.prior.get(key)
            if ch is None:
                raise StageError(f"{self.stage}: no input or prior {key!r} "
                                 f"(inputs {sorted(self.inputs)}, prior {sorted(self.prior)})")
            self._views[key] = read_view(ch)
        return self._views[key]

    def _pinned_chain(self, family: str, chain_id: str | None) -> Chain | None:
        cands = [c for c in (*self.inputs.values(), *self.prior.values())
                 if c.family == family and (chain_id is None or c.chain_id == chain_id)]
        ids_ = {c.chain_id for c in cands}
        if len(ids_) > 1:
            raise StageError(f"{self.stage}: {family} output is ambiguous over {sorted(ids_)}; pass chain_id")
        return cands[0] if cands else None

    def _last(self, d: str) -> str:
        if d not in self._lasts:  # a --series destination nobody pinned: read now
            self._lasts[d] = head_of(self.store_root, d)
        return self._lasts[d]

    def _dest(self, family: str, chain_id: str | None, pinned: Chain | None) -> OutSpec:
        """Where ``family``'s file goes (D373): its ``--out``; else the directory of
        the pinned input of that family (or an empty prior directory); else ``data/<family
        dir>/<--series>/``. ``chain_id`` names the family directory (``sources.<p>``)."""
        outs = [o for o in self._outs if o.family == family and (chain_id is None or o.family_dir == chain_id)]
        if len(outs) > 1:
            raise StageError(f"{self.stage}: {family} output matches several --out {[o.dir for o in outs]}; "
                             "pass chain_id")
        if outs:
            return outs[0]
        if pinned is not None:
            return OutSpec(pinned.chain_id, pinned.series, self._kind, self._restart)
        empty = [e for e in self._empty if e.family == family and (chain_id is None or e.family_dir == chain_id)]
        if empty:
            fam_dir, series = parse_dir_ref(empty[0].dir)
            return OutSpec(fam_dir, series, self._kind, self._restart)
        if self._series is not None:
            return OutSpec(chain_id or family, self._series, self._kind, self._restart)
        raise StageError(f"{self.stage}: no destination for its {chain_id or family} output: no --in of it to "
                         "extend; pass --out data/<family>/<series>/ or --series (D373)")

    def _emit(self, family: str, table: pa.Table, blocks_written: Sequence[str] | str, kind: str,
              chain_id: str | None) -> None:
        spec = family_spec(family)
        if any(e.family == family for e in self._emitted):
            raise StageError(f"{self.stage}: a second {family} output in one run")
        one = spec.kind in CHAIN_OF_ONE_KINDS
        if one and kind != "full":
            raise StageError(f"{self.stage}: {family} files are always full (D14): emit_full")
        pinned = self._pinned_chain(family, chain_id)
        dest = self._dest(family, chain_id, pinned)
        d = dest.dir
        after = self._last(d) if dest.series is not None else ""
        read_here = pinned if pinned is not None and pinned.dir == d else None
        want = "full" if one else (dest.kind or "full")
        if want == "part" and kind == "full":
            if after:
                raise StageError(f"{self.stage}: {family} is written as a full file by this stage; --part / "
                                 f"out kind part is not available for it ({d}/ is not empty)")
            want = "full"  # a series' first file is always full
        if want == "full" and kind == "part":
            if pinned is None:
                raise StageError(f"{self.stage}: a {family} part with no pinned {family} version to apply it to")
            table = overlay(read_view(pinned), table, spec.key)  # the full the part would make
            self.counts[f"materialized.{family}"] = table.num_rows
        base = read_here.head_name if read_here is not None else ""
        if want == "part":
            if read_here is None:
                raise StageError(f"{self.stage}: a {family} part goes only into the directory whose last file the run "
                                 f"read; {d}/ was not read (D372)")
        if base and base != after:
            raise StageError(f"{self.stage}: {d}/{base} was read but the directory's last file is {after}: a file "
                             "written on a non-last file would fork the directory")
        if not base and after and not dest.restart and family in self._reads:
            raise StageError(f"{self.stage}: {d}/ is not empty and the run read none of it: a full without a base "
                             "into it is a restart; pass --restart (plan: out: {restart: true}) or --in it")
        self._emitted.append(_Emitted(family, table, blocks_written, dest, want, base, after))

    @property
    def _reads(self) -> frozenset[str]:
        return restart_guarded(self.stage, self.plan_stage)

    def emit_part(self, family: str, table: pa.Table, blocks_written: Sequence[str] | str, *,
                  chain_id: str | None = None) -> None:
        """A part on the pinned version of ``family`` (written as a part with ``--part``, else as
        the full it makes)."""
        self._emit(family, table, blocks_written, "part", chain_id)

    def emit_full(self, family: str, table: pa.Table, blocks_written: Sequence[str] | str = WILDCARD, *,
                  chain_id: str | None = None) -> None:
        """A full file (its base the destination directory's file the run read, if any)."""
        self._emit(family, table, blocks_written, "full", chain_id)

    def writes_part(self, family: str, *, chain_id: str | None = None) -> bool:
        """Whether :meth:`emit_part` of ``family`` would write a part file (its destination's kind
        is ``part``), not the full the part makes; a stage may then leave out a part with no rows
        (S25-52, ``triage``). Raises as :meth:`emit_part` would for no destination."""
        if family_spec(family).kind in CHAIN_OF_ONE_KINDS:
            return False
        return self._dest(family, chain_id, self._pinned_chain(family, chain_id)).kind == "part"

    def heartbeat(self, inflight_keys: Iterable[str] | None = None) -> None:
        """Rewrite the local ``running`` json now; with keys, first replace ``inflight`` (D19). A
        failed save is logged and counted, not raised (:meth:`stack.store.job.RunJob.tolerant`).
        On the main thread, raises :class:`stack.store.job.JobCancelled` when the run's ticks
        gave up (D354)."""
        self._check_cancel()
        self._prov.update_counts(self.counts)
        if inflight_keys is not None:
            keys = list(dict.fromkeys(str(k) for k in inflight_keys))
            self._prov.tolerant(self._prov.set_inflight, keys, INFLIGHT_CAP)
        else:
            self._prov.tolerant(self._prov.save)

    def _check_cancel(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._prov.check_cancel()

    @contextmanager
    def substage(self, name: str) -> Iterator[Substage]:
        """A substage boundary. ``sub.active`` is false when ``--only`` names other substages and
        this one is not pure (D32): skip the work then. Records ``{ran, scope_complete}``."""
        active = self.only is None or name in self.only or name in self._pure
        sub = Substage(name, active, scope_complete=active)
        self._check_cancel()
        self._prov.update_counts(self.counts)
        try:
            yield sub
        except BaseException:
            self._prov.tolerant(self._prov.set_substage, name, ran=active, scope_complete=False)
            raise
        self._prov.tolerant(self._prov.set_substage, name, ran=active,
                            scope_complete=sub.scope_complete if active else False)


# ---------------------------------------------------------------- run_stage
def _output_key(pub: Published, all_pubs: Sequence[Published]) -> str:
    fam = pub.staged.family
    return fam if sum(p.staged.family == fam for p in all_pubs) == 1 else f"{fam}/{pub.staged.chain_id}"


def _in_files(chains: Iterable[Chain]) -> list[InputFile]:
    """``in_files``: each pinned version's resolved file by store path and digest (D373)."""
    out: list[InputFile] = []
    for c in chains:
        o = c.files[-1].output
        if o is not None:
            out.append({"path": str(o["path"]), "file_digest": str(o["file_digest"])})
    return out


def _version(staged: Sequence[StagedFile], rows_touched: int | None) -> VersionInfo | None:
    if not staged:
        return None
    s = next((x for x in staged if x.family == "documents"), staged[0])
    return {"rows_touched": int(s.header["row_count"]) if rows_touched is None else rows_touched}


def _close_spend(run_id: str) -> dict[str, Any]:
    """At a run's end (any status), before its final record: close the run's ledgers (open
    reservations released, batched ones reconciled at the run's ratio; D165 (C), D308) and return
    its ``spend`` (:func:`stack.llm.ledger.close_run`). A run that never loaded the ledger module
    opened no ledger: zero spend."""
    ledger = sys.modules.get("stack.llm.ledger")
    if ledger is None:
        return {"usd_micro": {}, "reserved_open_usd_micro": 0, "batch_ids": [], "abandoned_batch_ids": []}
    return ledger.close_run(run_id)


def _forget_spend(run_id: str) -> None:
    ledger = sys.modules.get("stack.llm.ledger")
    if ledger is not None:
        ledger.forget_run(run_id)


def _llm_calls(run_id: str) -> dict[str, int]:
    """``{"llm_calls": n}``: the LLM transport requests the run's clients sent
    (:func:`stack.llm.run_calls`), when it opened one; ``{}`` otherwise (``stack.llm`` not loaded
    or no client). Read after ``run(ctx)`` returns, so every call of the run is in."""
    llm = sys.modules.get("stack.llm")
    n = llm.run_calls(run_id) if llm is not None else None
    return {} if n is None else {"llm_calls": n}


def _fail(rec: RunRecord, error_class: str, error: str, status: str = "failed") -> None:
    rec.status = status
    rec.error_class = error_class
    rec.error = error[-4000:]
    rec.finished_at = dt.datetime.now(dt.UTC)
    rec.outputs = {}


class _Ending:
    """The end of one run: writes the run's final record exactly once through its job
    (:meth:`stack.store.job.RunJob.finish_record`: tick joined, json local, json and log PUT,
    marker deleted), adds the tick's failure count to ``counts``, and after that record exists
    turns a later error into a warning instead of a rewrite (R2-7)."""

    def __init__(self, rec: RunRecord, job: RunJob, warn: Callable[[str], None]) -> None:
        self.rec, self.job, self.warn = rec, job, warn
        self.committed = False

    def ok(self, outputs: dict[str, OutputInfo] | None = None, **fields: Any) -> None:
        """Set ``ok`` (and ``outputs``, other record fields), write the record."""
        with self.job.lock:
            for k, v in fields.items():
                setattr(self.rec, k, v)
            if outputs is not None:
                self.rec.outputs = outputs
            self.rec.status, self.rec.finished_at = "ok", dt.datetime.now(dt.UTC)
        self._write()

    def failed(self, error_class: str, error: str, status: str = "failed") -> None:
        if self.committed or self.job.final_written:  # an interrupt inside finish: the record stands
            self.warn(f"run {self.rec.run_id}: {error_class} after its final {self.rec.status} record was "
                      f"written; the record is kept: {error.strip().splitlines()[-1] if error.strip() else ''}")
            return
        with self.job.lock:
            _fail(self.rec, error_class, error, status)
        self._write()

    def _write(self) -> None:
        spend = _close_spend(self.rec.run_id)  # settle first, so the final record holds the spend
        with self.job.lock:
            self.rec.spend = spend
            if self.job.tick_failures:
                self.rec.counts["tick_errors"] = self.job.tick_failures
        notes = self.job.finish_record()
        self.committed = True
        for note in notes:
            self.warn(note)

    def close(self) -> None:
        """Stop the tick and leave the active jobs (idempotent; a run that never reached its final
        record stays ``running``, dead once its marker is stale), drop the run's ledgers."""
        self.job.abandon()
        _forget_spend(self.rec.run_id)


def _sayer(warn: Callable[[str], None] | None) -> Callable[[str], None]:
    """A run's warning line: to ``warn`` (default stderr) and into the active job's log."""
    out = warn or _stderr_warn

    def say(message: str) -> None:
        out(message)
        if warn is not None:  # _stderr_warn logs itself
            own_line(f"warning: {message}")

    return say


def _stderr_warn(message: str) -> None:
    sys.stderr.write(f"warning: {message}\n")
    own_line(f"warning: {message}")


def run_stage(store_root: str | Path, plan: Plan, stage: str, ins: Iterable[str] = (),
              outs: Iterable[str | OutSpec] = (), filter: str | None = None,
              only: Sequence[str] | None = None, *,
              warn: Callable[[str], None] | None = None, comment: str | None = None,
              note: str | None = None, series: str | None = None, kind: str | None = None,
              restart: bool = False, build: str | None = None, build_id: str | None = None,
              upload: bool | None = None, new_series: bool = False, no_overrides: bool = False,
              allow_env_limits: bool = False) -> RunRecord:
    """Run one stage end to end (module docstring) and return its final record (``ok``,
    ``failed`` or ``cancelled``). Refusals before dispatch raise :class:`StageError`; a transient S3
    failure in :func:`stack.store.job.prepare` (no record yet) raises
    :class:`stack.store.s3config.S3TransientError`.
    ``ins``: the ``--in`` paths (series directories or exact files, store-relative; D373);
    ``outs``: the ``--out`` destinations; ``series``, ``kind`` (``full``/``part``; default full),
    ``restart``: the run-level ``--series``, ``--full|--part``, ``--restart``. ``comment``: the
    operator's ``--comment`` (``[a-z0-9-]+``), recorded, part of the run's head and of every file
    name; ``note``: free text, recorded.
    ``build``/``build_id``: the build directory name and id of a build's run (its record goes under
    ``builds/<build>/runs/``, D344); ``None`` for a standalone run. ``upload``: the run's job
    uploads (default: the store has a remote; a build passes its own, D352). ``new_series``: ``--new-series``; a
    ``collect.*`` run that pins no ``sources`` prior of its own and writes into a series with no ``documents``
    while another series has some is refused before any record without it (:func:`new_series_refusal`, D491;
    a build's runs pass it, the build having asked the same question of its own series). ``no_overrides``:
    ``--no-overrides``; a run of a stage that reads ``overrides`` (gather, ``annotate.<wf>``, ``collect.manual``)
    that pins none, into a series whose ``data/overrides/<series>/`` has a file, is refused before any record
    without it (:func:`overrides_refusal`, D495; a build passes that directory whenever it has a file).
    ``allow_env_limits``: ``--allow-env-limits``; a ``collect.*`` run started while a collector cap/sampling
    environment variable is set is refused before any record without it (:func:`env_limits_refusal`, D553; a
    build passes its own); with it the values are recorded as the record's ``env_limits``."""
    root = Path(store_root)
    try:
        crash_point()
    except ValueError as exc:
        raise StageError(str(exc)) from None
    say = _sayer(warn)
    if kind not in (None, "full", "part"):
        raise StageError(f"kind {kind!r} is not full|part")
    if comment is not None and not COMMENT_RE.fullmatch(comment):
        raise StageError(f"--comment {comment!r} must match {COMMENT_RE.pattern}")
    if series is not None:
        try:
            check_series(series)
        except ValueError as exc:
            raise StageError(str(exc)) from None
    module = load_stage_module(stage)
    only_ = _validate_only(stage, module, only)
    line = plan.stage_line(stage)
    if line is None:
        line = StageLine.model_validate({"stage": stage})
        if warn is not None:
            warn(f"stage {stage!r} is not in plan {plan.plan!r}; running with a bare stage line")
    check_options(stage, line.options, getattr(module, "OPTIONS", None))  # D472: before any record
    specs = out_specs(outs, series=series, kind=kind, restart=restart)
    upload_ = default_upload(open_store(root), upload)
    prepare(root, upload_)
    pinned_in, pinned_prior, empty = _pin_all(root, stage, line, ins)
    pinned = [*pinned_in.values(), *pinned_prior.values()]
    lasts = {c.dir: c.last for c in pinned if c.dir is not None}
    for o in specs:
        if o.series is not None and o.dir not in lasts:
            lasts[o.dir] = head_of(root, o.dir)
    if phase_of(stage) == "collect" and not new_series and not any(c.family == "sources" for c in pinned_prior.values()):
        target = next((parse_dir_ref(e.dir)[1] for e in empty if e.family == "sources"), None) or next(
            (o.series for o in specs if o.family == "sources" and o.series is not None), None) or series
        if (why := new_series_refusal(root, target)) is not None:  # D491: before any record, like R5 below
            raise StageError(f"{stage}: {why}")
    env_limits = cap_env_set() if phase_of(stage) == "collect" else {}  # D553: only collectors read them
    if env_limits and not allow_env_limits:
        raise StageError(f"{stage}: {env_limits_refusal()}")
    reads = restart_guarded(stage, line)
    for o in specs:  # [r] R5: refused before any work
        read_from = any(c.dir == o.dir for c in pinned) or any(e.dir == o.dir for e in empty)
        if o.series is not None and lasts.get(o.dir) and not read_from and not o.restart and o.family in reads:
            raise StageError(f"--out {o.dir}/: not empty and not read by this run: a full without a base into it "
                             "is a restart; pass --restart, or --in it to extend it")
    from stack.build import stage_writes  # stack.build imports this module

    redirected, writes = {o.family for o in specs}, set(stage_writes(stage)) or set(reads)  # unknown phase: reads
    if ("overrides" in stage_reads(stage, line)[0] and not no_overrides
            and not any(c.family == "overrides" for c in pinned) and not any(e.family == "overrides" for e in empty)):
        target = next((o.series for o in specs if o.series is not None), None) or series or next(
            (c.series for c in pinned if c.family in writes and c.series is not None), None) or next(
            (c.series for c in pinned if c.series is not None), None)
        if (why := overrides_refusal(root, stage, target)) is not None:  # D495: before any record
            raise StageError(why)
    for c in pinned:  # a write back into a dir read at a non-last file would fork it: refused before any work
        if c.dir is not None and not c.is_dir_head and c.family in writes and c.family not in redirected:
            raise StageError(f"--in {c.head_key}: not the last file of {c.dir}/ ({c.last}); writing {c.family} back "
                             "into it would fork the directory; --out another directory (D381)")
    filter_ = filter if filter is not None else line.filter

    started = dt.datetime.now(dt.UTC)
    # [r] R7: one run-wide stamp after every input and every destination dir's last file (D381: a
    # run writing into a dir it did not read must not lose `name_order` to a same-second file)
    newest = max([*(c.head_name[:16] for c in pinned), *(n[:16] for n in lasts.values() if n)], default=None)
    stamp = tie_guard(started, newest)  # started_at stays the true start
    rec = RunRecord(
        run_id=ids.mint("run"), stage=stage, writer=stage, plan=plan.plan, status="running",
        started_at=started, heartbeat_at=started, code_digest=code_digest(),
        config_digest=line.digest(), only=only_, inputs={k: c.head for k, c in pinned_in.items()},
        prior=[c.head for c in pinned_prior.values()], filter=filter_, comment=comment, note=note,
        build_id=build_id, in_files=_in_files(pinned), env_limits=env_limits or None)
    job = RunJob(root, rec, build, upload=upload_)
    ctx = StageContext(store_root=root, run_id=rec.run_id, stage=stage, plan=plan, plan_stage=line,
                       inputs=pinned_in, prior=pinned_prior, filter=filter_, only=only_,
                       pure=tuple(getattr(module, "PURE_SUBSTAGES", ())), provisional=job,
                       config_digest=rec.config_digest, outs=specs, empty=empty, series=series, kind=kind,
                       restart=restart, lasts=lasts)
    end = _Ending(rec, job, say)
    try:
        job.start()
        job.start_tick()  # until the final record (R2-7): staging and publish are covered too
        try:
            result = module.run(ctx)
        except StageStopped:
            if ctx.on_stopped is None:
                raise
            result = ctx.on_stopped()
        if not isinstance(result, StageResult):
            raise StageError(f"{stage}: run(ctx) returned {type(result).__name__}, not StageResult")
        if ctx.stopped:  # D570: what the drain left is not done; the build reason is `stopped`
            d = drain_state()
            say(f"run {rec.run_id} ({stage}): drained ({d.by if d is not None else 'drain'}): publishing "
                "what it completed; the next run does the rest")
        with job.lock:
            rec.counts = {**rec.counts, **ctx.counts, **result.counts, **_llm_calls(rec.run_id)}
            if ctx.stopped:
                rec.counts["stopped.drain"] = max(1, int(rec.counts.get("stopped.drain") or 0))
            rec.scope_complete = bool(result.scope_complete) and not ctx.stopped
            if result.error:
                rec.error = result.error[-4000:]
            rec.config_digest = ctx.config_digest
            rec.prompt_pin = ctx.prompt_pin
            rec.selection = ctx.selection
            rec.writer = ctx.writer
            rec.fold = ctx.fold
        fields = HeaderFields(stage=stage, run_id=rec.run_id, config_digest=rec.config_digest,
                              code_digest=rec.code_digest, plan=plan.plan, inputs=dict(rec.inputs),
                              prior=list(rec.prior), only=only_, scope_complete=rec.scope_complete,
                              writer=ctx.writer, **_fold_header(ctx.fold))
        staged = [stage_file(root, make_data_file(e.dest.family_dir, e.dest.series, stamp, rec.run_id, e.kind,
                                                  comment=comment),
                             e.table, _file_fields(fields, e), base=e.base, after=e.after)
                  for e in ctx._emitted]
        with job.lock:
            rec.version = _version(staged, result.rows_touched)

        def on_published(pubs: list[Published]) -> None:
            end.ok({_output_key(p, pubs): p.as_output() for p in pubs})

        job.check_cancel()  # a cancelled run does not wait for its uploads
        ctx._flush_blobs()  # D407: every blob the run minted is on S3 before its data can be renamed
        if staged:
            publish(staged, on_published)
        else:
            job.check_cancel()
            end.ok()
    except JobStartError as exc:
        end.failed("tick_failed", str(exc))
    except JobCancelled as exc:
        end.failed("tick_failed", str(exc), status="cancelled")
    except HeadMovedError as exc:
        end.failed("head_moved", str(exc))
    except NameOrderError as exc:
        end.failed("name_order", str(exc))
    except EnvironmentRefused as exc:
        end.failed("environment", str(exc))
    except KeyboardInterrupt:
        end.failed("operator", "KeyboardInterrupt", status="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 — any stage error ends the run `failed`, recorded with its traceback
        if is_s3_transient(exc):  # S25-47: an S3 outage, not a bug; the message redacted
            end.failed("s3_transient", redacted_trace(exc))
        else:
            end.failed("code_bug", traceback.format_exc())
    finally:
        ctx._close_blobs()
        end.close()
    return rec


def _with_blocks(fields: HeaderFields, blocks: Sequence[str] | str) -> HeaderFields:
    return dataclasses.replace(fields, blocks_written=blocks)


def _file_fields(fields: HeaderFields, e: _Emitted) -> HeaderFields:
    """One emitted file's header fields: its ``blocks_written`` and, on a ``<wf>`` file, the
    payload registry's ``basis_kind`` (A.6; S7 refuses ``'none'`` there; T5.1a found no runner
    path set it)."""
    out = _with_blocks(fields, e.blocks_written)
    spec = family_spec(e.family)
    if spec.kind == "wf" and spec.workflow is not None:
        from stack.schemas.payloads.registry import REGISTRY as PAYLOADS

        out = dataclasses.replace(out, basis_kind=PAYLOADS[spec.workflow].basis_kind)
    return out


def _fold_header(fold: FoldInfo | None) -> dict[str, Any]:
    """The header's fold keys from a run record's ``fold`` entry (``{}`` = the ``none`` defaults)."""
    if fold is None:
        return {}
    return {"fold_config": fold["config"], "filter": fold["filter"],
            "annotations_folded": list(fold["annotations_folded"]),
            "release_config": fold["release_config"]}


def _fold_from_header(h: Mapping[str, str]) -> FoldInfo | None:
    """The run record's ``fold`` entry from a published fold file's header (string values)."""
    if h.get("fold_config", NONE) == NONE:
        return None
    raw = h.get("annotations_folded", NONE)
    folded = json.loads(raw) if raw not in (None, NONE) else []
    # T5.2: a build's header inputs are {corpus, <wf>…}; the joined <wf> files are the rest.
    ins = json.loads(h.get("inputs") or "{}") if h["fold_config"] == "build" else {}
    return {"config": h["fold_config"], "filter": h["filter"],
            "joined": {str(k): str(v) for k, v in ins.items() if k != "corpus"},
            "annotations_folded": [str(w) for w in folded], "release_config": h["release_config"]}


#: ``body(rec, on_published)`` of :func:`run_recorded`: publish through any driver, passing
#: ``on_published`` as its publish hook (the final ``ok`` record is written there, D38).
RecordedBody = Callable[[RunRecord, Callable[[list[Published]], None]], None]


def run_recorded(store_root: str | Path, *, stage: str, plan_name: str, inputs: Mapping[str, str],
                 body: RecordedBody, writer: str | None = None, filter: str | None = None,
                 config_digest: str = NONE, upload: bool | None = None,
                 warn: Callable[[str], None] | None = None) -> RunRecord:
    """Run ``body`` as one run with :func:`run_stage`'s job: refresh and recover
    (:func:`stack.store.job.prepare`), mint ``run_id``, start the job (standalone:
    ``runs/<head>.<stage>.json.zst``; ``rec.started_at`` is the stamp a driver passes as ``run_at``,
    D322) and its tick, call ``body(rec, on_published)``. ``on_published`` fills ``outputs``,
    ``version`` and, from a single published file's header, ``inputs``, ``config_digest`` and
    ``fold``, then writes the final ``ok`` record (the driver's :func:`stack.store.chain.publish`
    calls it after the data PUTs). A body that publishes nothing ends ``ok`` with no outputs;
    ``HeadMovedError`` ends ``failed[head_moved]``, a failed start upload ``failed[tick_failed]``,
    a self-cancel ``cancelled[tick_failed]``, a transient S3 failure ``failed[s3_transient]`` (in
    :func:`stack.store.job.prepare`: :class:`stack.store.s3config.S3TransientError` raised, no
    record), any other exception ``failed[code_bug]``,
    ``KeyboardInterrupt`` ``cancelled`` (re-raised); none of them rewrites a final record the hook
    already wrote (R2-7). Used by ``stack fold`` (T1.6) and ``stack migrate``. The body owns its
    inputs; it reads them through the records (D345)."""
    root = Path(store_root)
    say = _sayer(warn)
    upload_ = default_upload(open_store(root), upload)
    prepare(root, upload_)
    started = dt.datetime.now(dt.UTC)
    rec = RunRecord(run_id=ids.mint("run"), stage=stage, writer=writer or stage, plan=plan_name,
                    status="running", started_at=started, heartbeat_at=started,
                    code_digest=code_digest(), config_digest=config_digest, inputs=dict(inputs),
                    filter=filter)
    job = RunJob(root, rec, upload=upload_)
    end = _Ending(rec, job, say)

    def on_published(pubs: list[Published]) -> None:
        fields: dict[str, Any] = {}
        if len(pubs) == 1:
            h = pubs[0].staged.header
            fields = {"inputs": {str(k): str(v) for k, v in json.loads(h["inputs"]).items()},
                      "config_digest": h["config_digest"], "fold": _fold_from_header(h)}
        end.ok({_output_key(p, pubs): p.as_output() for p in pubs},
               version=_version([p.staged for p in pubs], None), scope_complete=True, **fields)

    try:
        job.start()
        job.start_tick()
        body(rec, on_published)
        if rec.status == "running":  # nothing published
            job.check_cancel()
            end.ok(scope_complete=True)
    except JobStartError as exc:
        end.failed("tick_failed", str(exc))
    except JobCancelled as exc:
        end.failed("tick_failed", str(exc), status="cancelled")
    except HeadMovedError as exc:
        end.failed("head_moved", str(exc))
    except NameOrderError as exc:
        end.failed("name_order", str(exc))
    except KeyboardInterrupt:
        end.failed("operator", "KeyboardInterrupt", status="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 — recorded with its traceback, as run_stage does
        if is_s3_transient(exc):
            end.failed("s3_transient", redacted_trace(exc))
        else:
            end.failed("code_bug", traceback.format_exc())
    finally:
        end.close()
    return rec


__all__ = [
    "CRASH_ENV",
    "CRASH_POINTS",
    "FOLD_STAGE_MODULE",
    "PHASE_ORDER",
    "WORKFLOWS_ROOT_ENV",
    "NO_DRAIN",
    "DrainCheck",
    "DrainPool",
    "POOL_MAX_THREADS_ENV",
    "THREAD_RESERVE",
    "EnvironmentRefused",
    "RecordedBody",
    "StageContext",
    "StageError",
    "StageResult",
    "StageStopped",
    "Substage",
    "check_options",
    "pool_ceiling",
    "thread_room",
    "code_digest",
    "load_path_module",
    "load_stage_module",
    "DOCUMENT_COLLECTORS",
    "OutSpec",
    "out_specs",
    "overlay",
    "restart_guarded",
    "stage_reads",
    "env_limits_refusal",
    "new_series_refusal",
    "overrides_refusal",
    "phase_of",
    "pin_input",
    "plan_order",
    "run_recorded",
    "run_stage",
    "stage_module_path",
    "topo_order",
]
