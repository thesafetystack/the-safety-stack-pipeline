"""``stack build <plan>`` (§0's chain, §3.1, §3.4, §7.4, §7.9, A.7; D16, D17, D19, D22, D67, D112-D115): run a
plan's stage list as one build and write its build record.

**Protocol of one build** (:func:`run_build`):

1. **start**: mint ``build_id`` (id kind ``sweep``, letter ``w``: v3's ``sweep_id`` was the one id
   stamped on every run of a plan pass, the ancestor of a build; :data:`BUILD_ID_KIND`);
   ``started_at`` = ``inputs_pinned_at``; ``plan_digest``, ``gate_digest``; ``series`` = the plan's
   (``stack build --series``). There is no prior build (D373): every line reads its series
   directories' heads and this build's own files (:func:`stage_inputs`). **Preflight** (REH F1, :func:`preflight`): each plan line that
   will run (not ``--skip``ped) whose stage module defines ``preflight(plan, line, store_root) ->
   list[str]`` is asked, without the network and without reading rows, what it would refuse
   ``failed[environment]`` with (acquire: a convert tool absent); any message ends the build ``failed[environment]`` with a record and no run.
   Before it, a line's ``options`` key outside its module's ``OPTIONS`` ends the build ``stage_refused``
   (:func:`stack.stage_runner.check_options`, D434).
   There is no build-start ``head_moved`` check against a prior build: X23 runs per file in verify
   (D375), and a moved head is caught by the publishing stage run (``head_moved``, step 3).
   **Structural verify** (:mod:`stack.checks.verify`, part ``structural``): after the preflight and the
   series' ``documents`` chain resolved (else ``chain_error``), before any run, footers, records, chains and blobs of the whole store; a hard failure ends the
   build ``failed[verify]`` with no run, the record's ``verify_start`` holding the checks either way.
   **Series guard** (A1, D491; :func:`stack.stage_runner.new_series_refusal`): before all of this, before any
   record, a build whose series has no ``documents`` file while another series of the store has one raises
   :class:`BuildFailed` ``new_series`` unless ``new_series`` (``stack build --new-series``): ids are minted per
   series, so such a build re-mints every id and re-pays every call. Its runs then skip the collectors' own
   guard (they get ``new_series=True``). **Env-limits guard** (D553; :func:`stack.stage_runner.env_limits_refusal`):
   first of all, a build started while a collector cap/sampling environment variable is set
   (``STACK_*_LIMIT``, ``*_FRACTION``, ``*_MAX_QUERIES``) raises :class:`BuildFailed` ``env_limits`` before any
   record unless ``allow_env_limits`` (``stack build --allow-env-limits``); with it the build record and each
   collector run record carry the values as ``env_limits``, and the build warns (D445).
2. **compaction** (§7.4, K#11; D373): when ``documents/<series>/`` has more part files after its newest
   full file than ``versions.compaction.max_partials``, or part rows / full rows above
   ``max_partial_rows_ratio``, one ``fold.compact`` run first; ``compacted_from`` = the head it
   superseded (D19);
3. **stages in plan order** (``plan.stages`` as listed), each through
   :func:`stack.stage_runner.run_stage` (every stage is a recorded run; ``fold.*`` through the
   fold adapter), each run pinning the directory heads current at its own start (series
   directories are passed as paths, so run *n+1* pins run *n*'s output; corpus and annotation
   files of this build as exact paths, D373). The default inputs of every phase are
   :func:`stage_inputs`, the one place they are decided. A stage line's ``repeat:`` (``acquire``
   defaults to ``repeat: {until: scope_complete, max_runs: 4}``) reruns it until
   ``scope_complete``, but not after a run that ended ``halted`` with fewer than
   :data:`HALTED_REPEAT_MIN_OK` items ok (:func:`halt_ends_repeat`: a provider refusing a whole lane
   for the run refuses the next run too; a build note says so). A stage refused before dispatch (no module, input not resolvable) ends the
   build ``failed[stage_refused]``; a run ending ``failed`` ends it with **that run's**
   ``error_class`` (``code_bug``, ``head_moved``, ``name_order``, ``environment``, …), the
   error naming stage and run, at once (the later lines do not run). A run that ends ``ok``
   with ``scope_complete = false`` (a planned stop: budget, ``max_rows``, ``max_minutes``,
   held collector units, failed transient windows, a convert stop) does **not**
   stop the build; a collector run with ``failed_permanent`` windows adds a warning (count and
   error classes; those windows keep the scope complete, move no watermark and are asked again
   by the next run, failing the same way until the query or the channel is fixed);
4. **require_complete** (D22, round 5 R6-4): per listed family, every stage that wrote it in this
   build, or whose phase writes it (:data:`PHASE_WRITES`), whose last run ended
   ``scope_complete = false`` is one entry of the record's ``incomplete: [{stage, run_id,
   reasons}]`` (reasons from the run's counts, :func:`incomplete_reasons`: ``budget``,
   ``limit``, ``time``, ``halted``, ``held``, ``windows_failed``, …). Every line
   runs, then verify. A stage line with ``require_complete: false`` (R6 item 1,
   :class:`stack.plan.StageLine`; std-v3's ``collect.s2-lookup``, enrichment that retries next build)
   is left out of ``incomplete``: its shortfall goes to ``incomplete_allowed`` (same entry shape)
   and a warning says the line ended short, is not required complete by the plan, and retries next
   build. A failed run of such a line still fails the build;
5. **verify** over the build's set: :func:`~stack.checks.verify.run_checks` with the record
   being written as the reference build and the families its ``files`` name; any hard check
   ``failed`` ⇒ ``failed[verify]``; ``warn`` (X11) does not fail. Verify passed and
   ``incomplete`` not empty ⇒ the build ends **``incomplete``**, else ``ok``. The verify runs the
   three parts (:mod:`stack.checks.verify`): the structural checks of the whole store again, every
   check on the files this build's runs wrote, and the build's set; ``verify_full`` /
   ``--verify-full`` runs the full verify (every check on every file, data files re-hashed). The
   record's ``verify.mode`` says which;
6. **record**: ``builds/<head>.<plan>/build.json.zst`` (:class:`stack.store.job.BuildJob`;
   ``<head>`` = ``<utc>_<build_id>[_<comment>]``, D344), written ``running`` at the build's start
   and rewritten at the end of every build, ``failed`` ones included (``error_class``,
   ``error``). The build's runs write their records under ``builds/<head>.<plan>/runs/``, each
   named with its own head (no comment). A ``KeyboardInterrupt`` writes ``cancelled[operator]``
   and re-raises.

**Drain** (D570, D573; :mod:`stack.store.job`, "Drain"): once the process drains (the first
SIGTERM, ``stack stop``), the build starts no further run: no later line, no ``repeat`` of the
current one, no later pass (:func:`run_passes`). The run in flight ends ``ok`` with ``scope_complete
= false`` (``stopped.drain``, reason ``stopped``) after publishing what it completed. The build then
verifies as usual and ends **``incomplete``** (verify passing) with ``stopped: {by, requested_at,
wait_s, runs_drained, not_run}`` in its record (``not_run``: the plan's lines, not ``--skip``ped, with
no run in this build). A later ``stack build`` of the plan and series resumes from the heads it
left; the release gate treats it as any ``incomplete`` build (D494). A drain that arrives after the
last run (during verify) changes nothing.

**Build status** (:data:`BUILD_STATUS`): ``ok``, ``incomplete``, ``failed``, ``cancelled``. An
``incomplete`` build is a valid build that stopped short of its scope: it left its rows in the
series directories the next build reads, so its ``ok`` rows are carried and only the rest is
asked; rerunning the build (or ``--passes N``) resumes a capped stage. It can be ``latest``
(the newest ``ok`` or ``incomplete`` build, :func:`stack.runs.newest_finished_build`, D703: ``stack
verify``'s default build, ``--build latest``, ``stack diff … latest``), but ``stack release`` refuses a
corpus file that only an ``incomplete`` build names without ``--allow-incomplete``. Records written before round 5 with
``failed[incomplete]`` keep that meaning: failed.

**The build's job** (D352-D356): the build is a job
(:class:`stack.store.job.BuildJob`): its ``running`` record, log (the orchestrator's own lines: a
start line, one line per stage run, warnings, the end) and marker are written (and PUT when
``upload``) at its start, re-written and re-uploaded by its tick every 30 s (the runs so far),
and replaced by the final record at its end. ``upload`` (default: the store has a remote;
``stack build --no-upload``) is recorded and every run of the build inherits it. There are no
locks (§6.6): one writing container per store at a time; two builds that publish on one base fork
the chain, which the next resolve reports (``chain_error``). Ticks failing for 2.5 min, the
build's or a run's, end the build ``cancelled[tick_failed]``.

**Build error classes** (:data:`BUILD_ERROR_CLASSES`): ``head_moved`` (a stage
run's own ``head_moved``, passed through; no build-start check raises it), ``chain_error`` (a chain the build or a stage reads does not resolve:
a missed file, a fork; X23 leaves those to V11), ``stage_refused``, ``missing_input``, ``verify``,
``code_bug`` (an exception in the orchestration itself), ``operator`` (cancelled), ``tick_failed``
(the build's or a run's ticks gave up, or a start upload failed), ``s3_transient`` (an S3 request
failed transiently past the client's retries: in a run, at a run's start before its record, or in
the orchestration; S25-47; a failure in the build's own start ``prepare`` raises
:class:`stack.store.s3config.S3TransientError`, no record),
``override_refused`` (a ``--skip``/``--filter``/``--max-usd`` the plan does not allow),
``build_running`` (old records only: the build lock is gone, D356), ``environment`` (a stage's
build-start :func:`preflight`, or a stage run's own ``EnvironmentRefused``), plus any run error
class passed through from the failing stage run (``name_order``, ``environment``, …;
``writer_running``, ``lock_held`` and ``unrecorded_head`` only in old records, :mod:`stack.runs`).
``incomplete`` is in the set for the records written before round 5 only (a build no longer
fails ``incomplete``: it ends with the status ``incomplete``). ``new_series`` (the series guard, D491) is raised
before the record and never recorded, so it is not in the set; nor is ``env_limits`` (D553), raised the same way.

**Annotation records** (registry ``produced_by``): ``annotate.<wf>`` also writes one ``<record>``
file per record its plan pin produces (:func:`stage_writes`); ``fold.build`` joins a record named
in ``annotations_folded`` from its producer's run. A plan folding a record that the pinned producer
prompt does not produce is refused at build start, before any run
(:func:`refuse_unproduced_records`: ``failed[missing_input]``; :func:`dry_run` raises it).

**Record** (A.7, D17, D373): ``files`` lists **this build's own outputs** only, the last per key
(a later run's file of a key replaces an earlier one's; a directory this build did not write is
not listed): ``documents``; ``sources/<family dir>`` and ``coverage/<family dir>`` (always the
slash form: one key per collector stays stable when a second collector arrives); ``<wf>``,
``<wf>_evidence``, ``triage_evidence``; ``corpus`` (the ``fold.build`` output, with ``fold``),
``corpus_input`` (the ``fold.corpus`` file), ``corpus_everything`` (the diagnostic ``fold.corpus``
run, D67). Each entry is ``{file_id, path, file_digest, run_id}``, ``run_id`` the run of this build
that wrote it. ``version_chain`` = the walk of this build's last ``documents`` file back to its
newest full file (file ids, oldest first; from the run records, D345), empty when the build wrote
no ``documents`` file; no other directory is walked (X10 checks it). ``runs`` = run ids in execution
order; ``counts`` = per run ``{stage, status, error_class, scope_complete, rows, calls, carried,
new_rows}`` (rows = the run's output row counts summed; the last three: **Passes** below);
``warnings`` = what the build skipped and why; ``incomplete`` = ``[{stage, run_id, reasons}]``
(step 4; on a ``failed`` build too when a stage had stopped short before it failed);
``incomplete_allowed`` = the same for lines with ``require_complete: false`` (present when not empty).

**Overrides** (:class:`BuildOverrides`, ``stack build --skip/--filter/--max-usd``; T5.5): a build
skips lines (``--skip`` fnmatch patterns; each skipped stage is a warning), narrows a line
(``--filter``, ANDed with the run's own filter) and caps a line's spend (``--max-usd``,
``annotate.<wf>`` only). They are validated before any run (:func:`apply_overrides`); a refusal
ends the build ``failed[override_refused]`` with a record. The record gets ``overrides: {skip,
skip_patterns, filter, max_usd}`` when any is given (``skip`` = the stages skipped). Work of
earlier stand-alone runs is carried by the series directories every build reads (D373). :func:`dry_run` resolves what a build would pin, writing
nothing.

**Passes** (round 4, ``stack build --passes N``; :func:`run_passes`): N builds of the plan in
sequence, pass *k+1* only after pass *k* ended ``ok`` or ``incomplete`` (its prior build); records
add ``pass`` and ``first_pass_build_id``. The stages carry their ``ok`` rows, so a later pass calls
only for new rows, due failures and what a capped stage left; the loop stops on a ``failed`` pass,
after a pass that :func:`pass_converged` (no call, no changed row of a chained family: ``converged``
when that pass was ``ok``, ``no_progress`` when it was ``incomplete``, e.g. a run's budget is
reached and nothing moves), or at N. Per run, ``counts`` adds ``calls`` (:data:`CALL_COUNTS`),
``carried`` (:data:`CARRIED_COUNTS`) and ``new_rows`` (:func:`run_new_rows`);
:func:`pass_summary` tabulates them per stage and pass.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import fnmatch
import gc
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pyarrow as pa

from stack.annotate.stage import pinned_records
from stack.checks.verify import BUILD_FINISH_PARTS, STRUCTURAL, run_checks
from stack.collect.sampling import cap_env_set
from stack.core import ids
from stack.plan import Budget, Plan, Repeat, StageLine
from stack.query import resolve_filter
from stack.runs import (
    RunRecord,
    build_dir_name,
)
from stack.schemas.families import FAMILIES
from stack.schemas.payloads.registry import REGISTRY as PAYLOADS
from stack.schemas.payloads.registry import joined_reads, records_of
from stack.stage_runner import (
    StageError,
    check_options,
    env_limits_refusal,
    load_stage_module,
    new_series_refusal,
    phase_of,
    run_stage,
)
from stack.store.chain import (
    CHAIN_OF_ONE_KINDS,
    Chain,
    ChainError,
    head_of,
    last_file,
    resolve,
    resolve_records,
)
from stack.store.index import store_index
from stack.store.job import BuildJob, JobCancelled, JobStartError, default_upload, drain_state, prepare, say
from stack.store.layout import DATA, family_of_dir
from stack.store.parquet import footer_cache
from stack.store.remote import open_store
from stack.store.s3config import S3TransientError, is_s3_transient, redacted_trace

#: ``build_id`` kind (``stack.core.idkinds``): ``sweep`` (``w``), v3's plan-pass id.
BUILD_ID_KIND = "sweep"
BUILD_STATUS: tuple[str, ...] = ("ok", "incomplete", "failed", "cancelled")
#: ``incomplete`` only in records written before round 5 (module docstring, "Build error classes").
BUILD_ERROR_CLASSES: frozenset[str] = frozenset(
    {"head_moved", "chain_error", "stage_refused", "missing_input", "incomplete", "verify", "code_bug",
     "operator", "override_refused", "build_running", "environment", "tick_failed", "s3_transient"})
#: Families a phase writes (§3.1's "Writes" column), for ``require_complete`` (D22): a stage of
#: the phase counts even when its last run emitted nothing. ``annotate.<wf>``: ``<wf>``,
#: ``<wf>_evidence`` (added in :func:`stage_writes`).
PHASE_WRITES: dict[str, tuple[str, ...]] = {
    "collect": ("sources", "coverage"),
    "gather": ("documents",),
    "triage": ("documents", "triage_evidence"),
    "acquire": ("documents",),
    "fold.compact": ("documents",),
    "fold.corpus": ("corpus",),
    "fold.build": ("corpus",),
    "overrides": ("overrides",),
}
#: Chained families whose chains a build record keys ``<family>/<chain_id>`` (D17, D54).
MULTI_CHAIN_FAMILIES: tuple[str, ...] = ("sources", "coverage")
_DEFAULT_REPEAT = {"acquire": Repeat()}  # §3.4: acquire repeats until scope_complete, max 4
#: Why a run ended ``scope_complete = false``, read from its ``counts`` (:func:`incomplete_reasons`):
#: ``(count key, reason)``, a key counting when its value is > 0. The stages count their stops
#: under their own names; acquire's and convert's ``budget`` is ``budget.max_minutes`` (time).
INCOMPLETE_REASONS: tuple[tuple[str, str], ...] = (
    ("stopped.drain", "stopped"),                 # any stage: a drain stopped it (D570; the runner sets it)
    ("not_attempted.stopped", "stopped"),         # annotate.<wf>: rows a drain left unasked
    ("ask.not_attempted.stopped", "stopped"),     # triage's asker: the same
    ("not_attempted.budget", "budget"),           # annotate.<wf>: the run's budget (budget.max_usd or the default)
    ("ask.not_attempted.budget", "budget"),       # triage's asker: the same
    ("not_attempted.deferred", "tier2_allowance"),  # annotate.<wf>: ask deferred the row (screen's tier 2, D706)
    ("ask.limit", "limit"),                       # triage: options.max_rows
    ("not_attempted.limit", "limit"),             # annotate.<wf>: options.max_rows (D709)
    ("halted", "halted"),                         # annotate (a fatal LLM error), a collector halt
    ("ask.not_attempted.halted", "halted"),
    ("not_attempted.unanswered", "unanswered"),   # annotate.<wf>, llm_mode batch: no answer this run (T-BATCH)
    ("ask.not_attempted.unanswered", "unanswered"),  # triage's asker: the same
    ("not_attempted.carried", "not_run"),         # triage: carried unasked, the ask substage did not run (D697)
    ("stopped.limit", "limit"),                   # acquire: options.max_rows
    ("stopped.budget", "time"),                   # acquire: budget.max_minutes
    ("stopped.halted", "halted"),                 # acquire: a lane halted
    ("convert.stopped.limit", "limit"),
    ("convert.stopped.budget", "time"),
    ("fetch_not_run", "not_run"),                 # acquire: fetch substage excluded, due work left
    ("arxiv_pdf.due_next_run", "pending"),        # acquire, arxiv_pdf on_miss: PDFs made due after quality (D482)
    ("held_retry_after", "held"),                 # collectors: a provider retry_after holds units
    ("windows_failed_transient", "windows_failed"),  # collectors (R3-2)
    ("windows_capped", "limit"),                  # collectors: a cap stopped a window short (D492)
    ("capped_not_due", "limit"),                  # collectors: a capped unit not asked again yet (D492)
    ("batches_failed", "windows_failed"),         # fetch/metadata lookups
    ("budget_stopped", "budget"),                 # semantic_scholar page budget
    ("queries_capped", "limit"),                  # arxiv_sweep: a query stopped by its cap (D471)
    ("surfaces_capped", "limit"),                 # cards: a github-org history sweep at max_repos (D555)
    ("openalex_budget_stop_max_usd", "budget"),
    ("openalex_budget_stop_max_requests", "limit"),
    ("x_budget_stop", "budget"),
    ("x_no_credentials", "no_credentials"),
    ("metadata_pending", "pending"),              # v3_import: the next run imports the next slice
)


#: A run that ended ``halted`` ends its line's ``repeat`` unless it still completed at least this many items
#: (``counts["outcome.ok"]``, acquire's ok roles; a stage without that count completed none):
#: :func:`halt_ends_repeat`. Build wtwv0wpnr8hfv (2026-10-27): the two acquire runs before the OA lane's
#: halt ended 26 and 12 roles ok; the four halted runs after it 0, 0, 1 and 3 (5 to 38 min each, ~677 rows due).
HALTED_REPEAT_MIN_OK = 10


class _BuildStopped(Exception):  # noqa: N818 — a stop, not an error
    """The process drains: the build starts no further run (module docstring, Drain)."""


def incomplete_reasons(counts: Mapping[str, Any]) -> list[str]:
    """Why a run ended ``scope_complete = false``, from its ``counts`` (:data:`INCOMPLETE_REASONS`,
    first-seen order, unique); ``["unspecified"]`` when no known count says."""
    out: list[str] = []
    for key, reason in INCOMPLETE_REASONS:
        try:
            hit = int(counts.get(key) or 0) > 0
        except (TypeError, ValueError):
            hit = False
        if hit and reason not in out:
            out.append(reason)
    return out or ["unspecified"]


def halt_ends_repeat(rec: RunRecord) -> str | None:
    """The build note when ``rec`` ends its line's ``repeat`` (module docstring, step 3): the run ended
    ``halted`` (a provider refused a whole lane or the stage for the run: acquire's lane-wide 429 or auth
    failure, ``halted.<lane>``; annotate's fatal LLM error; a collector halt) and completed fewer than
    :data:`HALTED_REPEAT_MIN_OK` items. The next run would meet the same refusal: the rows wait for the
    next build. A halted run that still made progress (another lane's work cut short by the clock, a halt
    late in the run) is repeated as before. ``None``: repeat as usual."""
    if "halted" not in incomplete_reasons(rec.counts):
        return None
    try:
        ok = int(rec.counts.get("outcome.ok") or 0)
    except (TypeError, ValueError):
        ok = 0
    if ok >= HALTED_REPEAT_MIN_OK:
        return None
    lanes = sorted(k.removeprefix("halted.") for k in rec.counts if k.startswith("halted."))
    return (f"{rec.stage} run {rec.run_id} halted ({', '.join(lanes) or 'no lane named'}) with {ok} item(s) ok "
            f"(< {HALTED_REPEAT_MIN_OK}): not repeated; the next build asks its due rows again")


def failed_permanent_note(rec: RunRecord) -> str | None:
    """The build warning for a collector run with ``failed_permanent`` windows (R3-2): they keep
    the scope complete, so the build record says so (count and error classes,
    ``windows_failed_permanent.<class>`` counts of :meth:`stack.collect.base.Collector.finish`).
    They move no watermark, so :meth:`~stack.collect.base.Collector.due` asks them again in the
    next run (S25-94: the text said "not retried")."""
    n = int(rec.counts.get("windows_failed_permanent") or 0)
    if phase_of(rec.stage) != "collect" or n <= 0:
        return None
    prefix = "windows_failed_permanent."
    classes = sorted((k.removeprefix(prefix), int(v)) for k, v in rec.counts.items() if k.startswith(prefix))
    detail = ", ".join(f"{c} x{v}" for c, v in classes) or "error classes not counted"
    return (f"{rec.stage} run {rec.run_id}: {n} coverage window(s) failed_permanent ({detail}); the scope "
            "stays complete; the next run asks those windows again and they fail the same way until "
            "the query or the channel is fixed")


class BuildFailed(Exception):
    """The build ends ``failed[error_class]`` (a record is still written)."""

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class StageCall:
    """One run of a stage (D373): its ``--in`` paths (series directories or exact files,
    store-relative), ``--out`` destinations, ``--series``, ``--part|--full`` (``kind``),
    ``--restart``, ``--comment``, ``--filter``, and the build-record key of a ``corpus`` output
    (``corpus_input``, ``corpus``, ``corpus_everything``)."""

    ins: tuple[str, ...] = ()
    outs: tuple[str, ...] = ()
    series: str | None = None
    kind: str | None = None
    restart: bool = False
    comment: str | None = None
    filter: str | None = None
    corpus_key: str | None = None


@dataclass
class BuildRecord:
    """The build record as written (``data``, A.7) and its path."""

    path: Path
    data: dict[str, Any]

    @property
    def build_id(self) -> str:
        return str(self.data["build_id"])

    @property
    def status(self) -> str:
        return str(self.data["status"])

    @property
    def error_class(self) -> str | None:
        return self.data.get("error_class")

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        return self.data.get("files") or {}


# ---------------------------------------------------------------- input resolution (D373, D378)
#: The family directory of each corpus key a fold writes (D374): ``fold.corpus``
#: into ``corpus/<series>/``, its diagnostic run into ``corpus.everything/<series>/``, ``fold.build``
#: into ``corpus.build/<series>/``.
CORPUS_DIRS: dict[str, str] = {"corpus_input": "corpus", "corpus_everything": "corpus.everything",
                               "corpus": "corpus.build"}


def series_path(family_dir: str, series: str) -> str:
    """``data/<family dir>/<series>/``, the ``--in``/``--out`` form of a series directory."""
    return f"{DATA}/{family_dir}/{series}/"


def _has_files(root: Path, path: str) -> bool:
    return last_file(root, path.rstrip("/")) is not None


def line_series(plan: Plan, line: StageLine) -> str:
    """The series of a line's default ``in``/``out``: its ``out.series``, else the plan's
    ``series:``; neither is :class:`BuildFailed` ``stage_refused`` (nothing is selected by plan
    name, D373)."""
    s = (line.out.series if line.out is not None else None) or plan.series
    if s is None:
        raise BuildFailed("stage_refused", f"{line.stage}: no series: the plan sets no `series:` and the line "
                          "no `out: {series: …}` (D378)")
    return s


def _with_line_ins(defaults: Sequence[str], line: StageLine) -> tuple[str, ...]:
    """The line's ``in:`` paths replace the defaults of the same family directory (D373)."""
    given = list(dict.fromkeys(line.in_))
    def fam_dir(p: str) -> str:  # "" for a dry run's "<this build: …>" placeholder
        return (p.strip("/").split("/") + [""])[1]

    dirs = {fam_dir(p) for p in given if p.count("/") >= 1}
    return tuple([p for p in defaults if fam_dir(p) not in dirs] + given)


def stage_writes(stage: str, plan: Plan | None = None) -> tuple[str, ...]:
    """The families ``stage``'s phase writes (:data:`PHASE_WRITES`; ``annotate.<wf>``: ``<wf>``,
    its records, ``<wf>_evidence``). Records: every registered record of ``<wf>`` (what the stage
    may write), or with ``plan`` the ones its pin produces (:func:`stack.annotate.stage.pinned_records`)."""
    if stage.startswith("annotate."):
        wf = stage.removeprefix("annotate.")
        recs = records_of(wf) if plan is None else pinned_records(plan, wf)
        return (wf, *recs, f"{wf}_evidence")
    return PHASE_WRITES.get(phase_of(stage), ())


def refuse_unproduced_records(plan: Plan) -> None:
    """Refuse a plan whose ``fold.build.annotations_folded`` names an annotation record that the
    pinned prompt of its producer's ``annotate.<producer>`` line does not produce, or one of whose
    ``annotate.<wf>`` lines reads such a record (D235: registry ``reads`` and the line's
    ``reads:``) (:class:`BuildFailed` ``missing_input``; a producer pin that cannot be resolved:
    ``stage_refused``). A folded record whose producer has no stage line is left to
    :func:`stage_inputs` (warned and not joined, as any ``<wf>`` without its stage); a read's
    producer always has one (the plan loader refuses it otherwise)."""
    wanted = [(name, "fold.build.annotations_folded names") for name in plan.fold.build.annotations_folded]
    for line in plan.stages:
        if line.stage.startswith("annotate."):
            wanted += [(name, f"{line.stage} reads")
                       for name in joined_reads(line.stage.removeprefix("annotate."), line.reads)]
    for name, who in wanted:
        spec = PAYLOADS.get(name)
        if spec is None or not spec.is_record:
            continue
        producer = spec.produced_by
        if plan.stage_line(f"annotate.{producer}") is None:
            continue
        try:
            made = pinned_records(plan, producer)
        except Exception as exc:  # noqa: BLE001 - any load failure refuses the plan, loudly
            raise BuildFailed("stage_refused", f"{who} record {name}: the records of "
                              f"annotate.{producer} are not resolvable: {type(exc).__name__}: {exc}") from None
        if name not in made:
            pin = plan.prompts.get(f"annotate.{producer}", {}).get("name")
            raise BuildFailed("missing_input", f"{who} {name!r}, a record of "
                              f"annotate.{producer}, but the plan's pin (name {pin!r}) produces "
                              f"{list(made) or 'no records'}")


#: Collectors that read the ``documents`` head besides the ``*-lookup`` ones (round 4, L2).
DOCUMENT_COLLECTORS: tuple[str, ...] = ("collect.links",)


def preflight(store_root: str | Path, plan: Plan, skipped: Sequence[str] = ()) -> list[str]:
    """The build-start messages (module docstring, Preflight): per plan line not in ``skipped``, its
    stage module's optional ``preflight(plan, line, store_root) -> list[str]`` (each message names
    its stage and the remedy). A module that does not load is left to the line's own refusal; an
    ``options`` key outside the module's ``OPTIONS`` (D434), or a preflight raising
    :class:`StageError` (a malformed line), refuses ``stage_refused``."""
    out: list[str] = []
    for line in plan.stages:
        if line.stage in skipped:
            continue
        try:
            module = load_stage_module(line.stage)
        except (StageError, NotImplementedError):
            continue
        check = getattr(module, "preflight", None)
        try:
            check_options(line.stage, line.options, getattr(module, "OPTIONS", None))
            if check is not None:
                out += [m for m in check(plan, line, Path(store_root)) if m not in out]
        except StageError as exc:
            raise BuildFailed("stage_refused", f"{line.stage}: {exc}") from None
    return out


def target_refusals(plan: Plan, skipped: Sequence[str] = ()) -> list[str]:
    """One message per line not in ``skipped`` whose ``target`` (D758, :mod:`stack.plan` "Accepted configs") is not
    the config its pin gives today (triage: per question): the build refuses ``stage_refused`` at its start, before
    any run, and ``stack build --dry-run`` lists them under ``preflight``. The stage itself refuses the same."""
    from stack import gate
    from stack.annotate.stage import config_digest, resolve_pin, stack_config

    out: list[str] = []
    for line in plan.stages:
        if line.target is None or line.stage in skipped:
            continue
        if line.stage == "triage":
            cur = {q.name: q.config_digest for q in gate.questions(plan)}
            assert isinstance(line.target, dict)
            out += [f"triage: target of {q} {d[:12]}… is not the question's digest {cur.get(q, '?')[:12]}…"
                    for q, d in line.target.items() if cur.get(q) != d]
            continue
        wf = load_stage_module(line.stage).WORKFLOW
        pin = dict(plan.prompts.get(line.stage, {}))
        have = config_digest(wf.spec, pin, resolve_pin(wf, pin, stack_config))
        if have != line.target:
            out.append(f"{line.stage}: target {str(line.target)[:12]}… is not the config its pin gives ({have[:12]}…)")
    return out


def stage_inputs(store_root: str | Path, plan: Plan, line: StageLine, built: Mapping[str, Mapping[str, Any]],
                 warn: Callable[[str], None]) -> list[StageCall]:
    """The ``--in``/``--out``/``--series``/``--filter`` of a stage's runs in a build: **the one
    place the build decides them** (D373, D378; the "Reads" column of §3.1). ``S`` is the
    line's series (:func:`line_series`), ``built`` this build's ``files`` so far (keys as in the
    record); a line's ``in:`` replaces the defaults of the same family directory, its ``out:``
    gives ``--out`` paths, ``--part|--full``, ``--restart`` and ``--comment``. Nothing is selected
    by plan name or prior build: every input is a series directory or this build's exact file.
    Returns the runs to make (``fold.corpus`` with ``fold.diagnostic: true`` makes two).

    ==========================  =======================================================================
    stage                       ``--in`` (``<f>/S/`` = ``data/<f>/S/``, the directory's last file at run start)
    ==========================  =======================================================================
    ``collect.<p>``             ``sources.<p>/S/``, ``coverage.<p>/S/`` (the line's ``prior:``
                                families; empty directories are the empty prior of a first run);
                                ``collect.manual`` also ``overrides/S/`` when it has a file
    ``collect.<p>-lookup``      as ``collect.<p>``, plus ``documents/S/`` (also the
                                :data:`DOCUMENT_COLLECTORS`); with ``documents/S/`` empty not run,
                                with a warning
    ``gather``                  every ``sources.*/S/`` with a file; ``documents/S/`` and
                                ``overrides/S/`` when they have a file
    ``triage``                  ``documents/S/``, ``triage_evidence/S/``; under ``options.title_basis: true``
                                (D527) also every ``sources.*/S/`` with a file, as gather
    ``acquire``, ``fold.compact``, other  ``documents/S/`` (when it has a file)
    ``fold.corpus``             ``documents/S/`` → ``corpus/S/``; with ``fold.diagnostic`` a second
                                run, filter ``everything`` (D67) → ``corpus.everything/S/``
    ``annotate.<wf>``           **this build's** ``fold.corpus`` file (never a head, X2), this build's
                                file of every joined read (D235; one missing fails the build
                                ``missing_input``), ``overrides/S/`` when it has a file, and
                                ``<f>/S/`` for each family of the line's ``prior:`` (default
                                ``<wf>``, ``<wf>_evidence``)
    ``fold.build``              this build's ``fold.corpus`` file and ``annotate.<wf>`` output per
                                ``annotations_folded`` workflow (a missing one fails the build
                                ``missing_input`` when the plan has the line, else it is skipped with
                                a warning) → ``corpus.build/S/``
    ==========================  =======================================================================
    """
    root = Path(store_root)
    stage = line.stage
    phase = phase_of(stage)
    S = line_series(plan, line)
    out = line.out

    def d(family_dir: str) -> str:
        return series_path(family_dir, S)

    def having(family_dir: str) -> tuple[str, ...]:
        return (d(family_dir),) if _has_files(root, d(family_dir)) else ()

    def call(ins: Sequence[str], *, outs: Sequence[str] = (), kind: str | None = None, series: str = S,
             filter: str | None = None, corpus_key: str | None = None) -> StageCall:
        return StageCall(ins=_with_line_ins(ins, line), outs=tuple(out.paths if out is not None else ()) + tuple(outs),
                         series=series, kind=(out.kind if out is not None else None) or kind,
                         restart=out.restart if out is not None else False,
                         comment=out.comment if out is not None else None, filter=filter, corpus_key=corpus_key)

    def this_build(key: str, why: str) -> str:
        entry = built.get(key)
        if entry is None:
            raise BuildFailed("missing_input", f"{stage}: no {key} file in this build ({why})")
        return str(entry["path"])

    docs = having("documents")
    ovr = having("overrides")
    if phase == "collect":
        reads_docs = stage.endswith("-lookup") or stage in DOCUMENT_COLLECTORS
        if reads_docs and not docs:
            # A first build on an empty series (round 4, P): nothing to look up yet; the line runs
            # from the next build (or pass) on, once gather has written documents/S/.
            warn(f"{stage}: {d('documents')} has no file yet (first build): nothing to look up, not run")
            return []
        _, _, suffix = stage.partition(".")
        own = tuple(d(f"{fam}.{suffix}") for fam in dict.fromkeys(line.prior or ["sources", "coverage"])
                    if fam in FAMILIES and FAMILIES[fam].kind not in CHAIN_OF_ONE_KINDS) if suffix else ()
        return [call(own + (docs if reads_docs else ()) + (ovr if stage == "collect.manual" else ()))]
    if phase == "gather":
        srcs = tuple(f"{x}/" for x in store_index(root).series_dirs()
                     if x.split("/")[2] == S and family_of_dir(x.split("/")[1]) == "sources")
        return [call(srcs + docs + ovr)]
    if phase == "triage":
        srcs = ()  # D527: under title_basis the asker reads the members' query labels (matched_by)
        if line.options.get("title_basis") is True:
            srcs = tuple(f"{x}/" for x in store_index(root).series_dirs()
                         if x.split("/")[2] == S and family_of_dir(x.split("/")[1]) == "sources")
        return [call(docs + (d("triage_evidence"),) + srcs)]
    if stage == "fold.corpus":
        calls = [call(docs, outs=(d(CORPUS_DIRS["corpus_input"]),), corpus_key="corpus_input")]
        if plan.fold.diagnostic:
            calls.append(call(docs, outs=(d(CORPUS_DIRS["corpus_everything"]),), filter="everything",
                              corpus_key="corpus_everything"))
        return calls
    if stage == "fold.compact":
        return [call(docs, kind="full")]
    if phase == "annotate":
        wf = stage.removeprefix("annotate.")
        corpus = this_build("corpus_input", "annotate reads the build's fold.corpus file; "
                            "the plan needs fold.corpus before it")
        reads = tuple(this_build(name, f"{stage} reads {name} (D235); its producer "
                                 f"annotate.{PAYLOADS[name].produced_by} wrote none")
                      for name in joined_reads(wf, line.reads))
        prior = tuple(d(f) for f in dict.fromkeys(line.prior or [wf, f"{wf}_evidence"]) if f in FAMILIES)
        return [call((corpus, *reads, *ovr, *prior))]
    if stage == "fold.build":
        corpus = this_build("corpus_input", "fold.build reads the build's fold.corpus file")
        joins: list[str] = []
        for wf in plan.fold.build.annotations_folded:
            entry = built.get(wf)
            producer = PAYLOADS[wf].produced_by if wf in PAYLOADS else wf  # a record: its producer's run
            if entry is not None:
                joins.append(str(entry["path"]))
            elif plan.stage_line(f"annotate.{producer}") is not None:
                raise BuildFailed("missing_input", f"fold.build: annotate.{producer} is in the plan but "
                                  f"wrote no {wf} file in this build")
            else:
                warn(f"fold.build: {wf} is in fold.build.annotations_folded but the plan has no "
                     f"annotate.{producer} stage; not joined")
        return [call((corpus, *joins), outs=(d(CORPUS_DIRS["corpus"]),), corpus_key="corpus")]
    return [call(docs)]


def repeat_of(line: StageLine) -> Repeat | None:
    """The line's ``repeat:``; ``acquire`` without one repeats up to 4 runs (§3.4)."""
    return line.repeat or _DEFAULT_REPEAT.get(phase_of(line.stage))


# ---------------------------------------------------------------- overrides (T5.5)
@dataclass(frozen=True)
class BuildOverrides:
    """Operator overrides of one build (module docstring), all empty by default.

    * ``skip``: fnmatch patterns over the plan's stage names; a pattern matching no line is
      refused (typo guard);
    * ``filters``: ``stage -> SQL`` ANDed with the run's own filter as ``(<line filter>) AND
      (<override>)`` (named filters expanded); refused for ``fold.*`` (fold filters are plan
      config), ``triage`` (its scope is §3.1's, it refuses ``--filter``) and a stage not in the
      plan;
    * ``max_usd``: ``stage -> USD``, the line's ``budget.max_usd`` for this build, setting or
      lowering it, never raising; ``annotate.<wf>`` only: ``triage``'s ``config_digest`` includes
      its line's digest (:func:`stack.gate.triage_config_digest`), so a budget change would rescope
      every row, and the other phases do not read ``budget.max_usd`` (their ``config_digest`` is
      the line's digest, so it would only change that). ``annotate.<wf>``'s ``config_digest``
      excludes the line (:func:`stack.annotate.stage.config_digest`)."""

    skip: Sequence[str] = ()
    filters: Mapping[str, str] = field(default_factory=dict)
    max_usd: Mapping[str, float] = field(default_factory=dict)

    @property
    def given(self) -> bool:
        return bool(self.skip or self.filters or self.max_usd)

    def requested(self) -> dict[str, Any]:
        """The record's ``overrides`` as asked (``skip`` filled in by :func:`apply_overrides`)."""
        return {"skip": [], "skip_patterns": list(self.skip),
                "filter": dict(self.filters), "max_usd": {k: float(v) for k, v in self.max_usd.items()}}


@dataclass(frozen=True)
class AppliedOverrides:
    """:class:`BuildOverrides` validated against the plan: ``skipped`` = the stages not run, ``filters`` =
    ``stage -> override``, ``lines`` = ``stage -> line`` with the overridden budget, ``record`` =
    the build record's ``overrides`` (``None`` when nothing was given)."""

    skipped: tuple[str, ...] = ()
    filters: Mapping[str, str] = field(default_factory=dict)
    lines: Mapping[str, StageLine] = field(default_factory=dict)
    record: Mapping[str, Any] | None = None

    def plan_for(self, plan: Plan, stage: str) -> Plan:
        """``plan`` with ``stage``'s line replaced by its overridden copy (the run's
        ``ctx.plan_stage``); ``plan`` itself when the stage has no budget override."""
        line = self.lines.get(stage)
        if line is None:
            return plan
        return plan.model_copy(update={"stages": [line if s.stage == stage else s for s in plan.stages]})

    def line_of(self, line: StageLine) -> StageLine:
        return self.lines.get(line.stage, line)

    def call_of(self, line: StageLine, call: StageCall) -> StageCall:
        """``call`` with its filter ANDed with the ``--filter`` of ``line.stage`` (if any)."""
        ov = self.filters.get(line.stage)
        if ov is None:
            return call
        base = call.filter if call.filter is not None else line.filter
        both = f"({resolve_filter(base)}) AND ({resolve_filter(ov)})" if base else ov
        return replace(call, filter=both)


def apply_overrides(store_root: str | Path, plan: Plan, overrides: BuildOverrides) -> AppliedOverrides:
    """Validate ``overrides`` (:class:`BuildOverrides`) against ``plan``. Raises
    :class:`BuildFailed` ``override_refused``."""
    if not overrides.given:
        return AppliedOverrides()
    names = [s.stage for s in plan.stages]

    def refuse(message: str) -> BuildFailed:
        return BuildFailed("override_refused", message)

    skipped: list[str] = []
    for pat in overrides.skip:
        hit = [n for n in names if fnmatch.fnmatchcase(n, pat)]
        if not hit:
            raise refuse(f"--skip {pat!r} matches no stage of plan {plan.plan!r} (stages: {', '.join(names)})")
        skipped += [n for n in hit if n not in skipped]
    skipped = [n for n in names if n in skipped]  # plan order

    filters: dict[str, str] = {}
    for stage, sql in overrides.filters.items():
        if stage not in names:
            raise refuse(f"--filter {stage}=…: {stage!r} is not a stage of plan {plan.plan!r}")
        if stage.startswith("fold."):
            raise refuse(f"--filter {stage}=…: a fold's filter is plan config (fold.corpus.filter, "
                         "fold.build.filter), not a build override")
        if phase_of(stage) == "triage":
            raise refuse("--filter triage=…: triage's scope is §3.1's; it refuses a filter")
        if not str(sql).strip():
            raise refuse(f"--filter {stage}=: an empty filter")
        filters[stage] = str(sql).strip()

    lines: dict[str, StageLine] = {}
    for stage, usd in overrides.max_usd.items():
        line = plan.stage_line(stage)
        if line is None:
            raise refuse(f"--max-usd {stage}=…: {stage!r} is not a stage of plan {plan.plan!r}")
        if phase_of(stage) == "triage":
            raise refuse(f"--max-usd {stage}=…: refused for triage: its config_digest includes the stage "
                         "line's digest (stack.gate.triage_config_digest), so a budget change would make "
                         "every triage row config-stale and rescope it; edit the plan's budget instead")
        if not stage.startswith("annotate."):
            raise refuse(f"--max-usd {stage}=…: only annotate.<wf> lines take a budget override "
                         f"({phase_of(stage)} does not read budget.max_usd)")
        usd = float(usd)
        if not usd > 0:
            raise refuse(f"--max-usd {stage}={usd}: must be > 0")
        have = line.budget.max_usd if line.budget is not None else None
        if have is not None and usd > have:
            raise refuse(f"--max-usd {stage}={usd}: raises the plan's budget.max_usd {have}; "
                         "a build override may only set or lower it")
        budget = (line.budget or Budget.model_validate({})).model_copy(update={"max_usd": usd})
        lines[stage] = line.model_copy(update={"budget": budget})

    record = {**overrides.requested(), "skip": skipped}
    return AppliedOverrides(skipped=tuple(skipped), filters=filters, lines=lines, record=record)


# ---------------------------------------------------------------- the build
def release_memory() -> None:
    """Hand the memory a finished run freed back to the OS before the build's next stage (D780): every stage of a
    build runs in the build's one process, so what a run's allocators keep after it (Arrow's pool, glibc's free
    lists) would otherwise add to the next run's peak. A garbage collection, Arrow's ``release_unused`` and glibc's
    ``malloc_trim`` (where there is one)."""
    gc.collect()
    pa.default_memory_pool().release_unused()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # not glibc
        pass


def compaction_due(plan: Plan, chain: Chain) -> str | None:
    """Why ``fold.compact`` is due on ``chain`` (§7.4, K#11), or ``None``: part files with rows
    after the newest full file above ``max_partials`` (a zero-row part, which older ``triage``
    runs wrote, is not counted; S25-52), or part rows / full rows above
    ``max_partial_rows_ratio``."""
    comp = plan.versions.compaction
    parts = [f for f in chain.files[1:] if f.row_count > 0]
    if len(parts) > comp.max_partials:
        return f"{len(parts)} part files > max_partials {comp.max_partials}"
    full_rows = chain.files[0].row_count
    part_rows = sum(f.row_count for f in parts)
    if parts and full_rows > 0 and part_rows / full_rows > comp.max_partial_rows_ratio:
        return f"part rows {part_rows} / full rows {full_rows} > {comp.max_partial_rows_ratio}"
    return None


@dataclass
class _Build:
    root: Path
    plan: Plan
    warn: Callable[[str], None]
    name: str | None = None  # builds/<name>/ (D344)
    build_id: str | None = None
    runs: list[RunRecord] = field(default_factory=list)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    applied: AppliedOverrides = field(default_factory=AppliedOverrides)
    job: BuildJob | None = None
    upload: bool | None = None
    progress: Callable[[], None] = lambda: None
    allow_env_limits: bool = False  # D553: the build was allowed to start with env caps; its runs inherit it

    def note(self, message: str) -> None:
        self.warnings.append(message)
        self.warn(message)

    def run(self, stage: str, call: StageCall, *, by_rule: bool = False) -> RunRecord:
        """One recorded run; its chain-of-one outputs go into ``files``. A refusal or a run that
        does not end ``ok`` raises :class:`BuildFailed`. ``by_rule``: a run the build adds itself
        (``fold.compact``), so the runner's "not in the plan" warning is dropped. The build's
        cancel flag (its ticks gave up) ends the build before the stage starts; a drain
        (:class:`_BuildStopped`) starts no run."""
        if drain_state() is not None:
            raise _BuildStopped(stage)
        if self.job is not None:
            try:
                self.job.check_cancel()  # D493: raising makes the cancel stand
            except JobCancelled as exc:
                raise BuildFailed("tick_failed", str(exc) or "the build's ticks gave up") from None
        try:
            rec = run_stage(self.root, self.applied.plan_for(self.plan, stage), stage, call.ins, call.outs,
                            call.filter, warn=(lambda _m: None) if by_rule else self.note,
                            comment=call.comment, series=call.series, kind=call.kind, restart=call.restart,
                            build=self.name, build_id=self.build_id, upload=self.upload,
                            new_series=True,  # D491: the build asked it of its series (run_build)
                            allow_env_limits=self.allow_env_limits)  # D553: as the build was
        except S3TransientError as exc:  # S25-47: the run's prepare, before its record
            raise BuildFailed("s3_transient", f"stage {stage}: {exc}") from None
        except (StageError, NotImplementedError) as exc:
            raise BuildFailed("stage_refused", f"stage {stage} refused before dispatch: {exc}") from None
        except ChainError as exc:
            raise BuildFailed("chain_error", f"stage {stage}: an input chain does not resolve: "
                              f"{type(exc).__name__}: {exc}") from None
        finally:
            release_memory()
        self.runs.append(rec)
        say(f"build {self.build_id}: {stage} run {rec.run_id} {rec.status}"
            + (f"[{rec.error_class}]" if rec.error_class else ""))
        self.progress()
        if rec.status != "ok":
            first = (rec.error or "").strip().splitlines()
            raise BuildFailed(rec.error_class or rec.status,
                              f"stage {stage} run {rec.run_id} ended {rec.status}[{rec.error_class}]: "
                              f"{first[-1] if first else ''}")
        for key, out in rec.outputs.items():
            fam = key.split("/", 1)[0]
            entry: dict[str, Any] = {"file_id": out["file_id"], "path": out["path"],
                                     "file_digest": out["file_digest"], "run_id": rec.run_id}
            if fam == "corpus":
                entry["schema_digest"] = out["schema_digest"]  # R1-2: a later format change is visible
                if "family_version" in out:
                    entry["family_version"] = out["family_version"]  # D295
                if call.corpus_key is None:
                    raise BuildFailed("code_bug", f"stage {stage} wrote a corpus file outside a fold")
                if rec.fold is not None:
                    entry["fold"] = dict(rec.fold)
                self.files[call.corpus_key] = entry
            elif fam in MULTI_CHAIN_FAMILIES:
                self.files[f"{fam}/{out['chain_id']}"] = entry
            else:
                self.files[fam] = entry  # a later run's file of the family replaces an earlier one's
        return rec

    def run_line(self, line: StageLine) -> None:
        """All runs of one stage line. A line whose last run left a ``require_complete`` family
        short (``scope_complete = false``) is a warning and the build goes on (module docstring,
        step 4); a collector run with ``failed_permanent`` windows is a warning (R3-2). A line
        skipped by ``--skip`` does not run and is a warning; a ``--filter`` is ANDed into each of
        its calls."""
        if line.stage in self.applied.skipped:
            self.note(f"skipped by --skip: {line.stage}")
            return
        rep = repeat_of(line)
        for i, call in enumerate(stage_inputs(self.root, self.plan, line, self.files, self.note)):
            call = self.applied.call_of(line, call)
            n = 1
            rec = self.run(line.stage, call)
            while i == 0 and rep is not None and not rec.scope_complete and n < rep.max_runs:
                if (why := halt_ends_repeat(rec)) is not None:
                    self.note(why)
                    break
                n += 1
                rec = self.run(line.stage, call)
        for rec in self.runs:
            if rec.stage == line.stage and (msg := failed_permanent_note(rec)) is not None:
                self.note(msg)
        for e in self.short(line.stage):
            if e["required"]:
                self.note(f"{e['stage']} run {e['run_id']} ended scope_complete = false "
                          f"({', '.join(e['reasons'])}; require_complete {', '.join(e['families'])}): the "
                          "build goes on and ends incomplete; the next build or pass carries its ok rows "
                          "and asks the rest")
            else:
                self.note(f"{e['stage']} run {e['run_id']} ended scope_complete = false "
                          f"({', '.join(e['reasons'])}; writes {', '.join(e['families'])}): the plan line "
                          "has require_complete: false, so the build does not end incomplete for it; "
                          "the next build retries what it left")

    def version_chain(self) -> list[str]:
        """The record's ``version_chain``: the header walk (newest full file to the file) of this
        build's last ``documents`` file, from the run records (D345); empty without one."""
        entry = self.files.get("documents")
        if entry is None:
            return []
        return list(resolve_records(self.root, entry["path"]).version_chain)

    def short(self, stage: str | None = None) -> list[dict[str, Any]]:
        """D22 over this build's runs (of ``stage`` only, when given): per stage that writes a
        ``require_complete`` family (actual outputs, or the phase's :func:`stage_writes`) and
        ended its last run ``ok`` with ``scope_complete = false``, ``{stage, run_id, reasons, families,
        required}`` (:func:`incomplete_reasons`; ``required`` = the plan line's ``require_complete``,
        true for a stage without a line), in stage order of first run."""
        last: dict[str, RunRecord] = {}
        wrote: dict[str, set[str]] = {}
        for rec in self.runs:
            last[rec.stage] = rec
            for key in rec.outputs:
                wrote.setdefault(key.split("/", 1)[0], set()).add(rec.stage)
        fams: dict[str, list[str]] = {}
        for fam in self.plan.require_complete:
            for s in wrote.get(fam, set()) | {s for s in last if fam in stage_writes(s)}:
                if (stage is None or s == stage) and last[s].status == "ok" and not last[s].scope_complete:
                    fams.setdefault(s, []).append(fam)  # a failed run is the build's failure, not a stop
        return [{"stage": s, "run_id": last[s].run_id, "reasons": incomplete_reasons(last[s].counts),
                 "families": fams[s], "required": self._required(s)} for s in last if s in fams]

    def _required(self, stage: str) -> bool:
        line = self.plan.stage_line(stage)
        return line is None or line.require_complete

    def record_short(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        """Set the record's ``incomplete`` and ``incomplete_allowed`` lists (each only when not
        empty, entries ``{stage, run_id, reasons}``); return the ``incomplete`` entries."""
        short = self.short()
        for key, required in (("incomplete", True), ("incomplete_allowed", False)):
            got = [{k: e[k] for k in ("stage", "run_id", "reasons")} for e in short if e["required"] is required]
            if got:
                rec[key] = got
        return [e for e in short if e["required"]]


def _check_entry(r: Any) -> dict[str, Any]:
    return {"code": r.code, "name": r.name, "level": r.level, "status": r.status,
            "error_class": r.error_class, "n": r.n, "items": list(r.all_items)}


def _verify(root: Path, rec: dict[str, Any], key: str, **kw: Any) -> None:
    """Run :func:`~stack.checks.verify.run_checks` (``kw``) and put its entry ``{status, mode,
    checks}`` in ``rec[key]`` (``verify_start``, ``verify``); a hard check failed ⇒ :class:`BuildFailed`
    ``verify``, naming ``key``, after the entry is in ``rec``."""
    results = run_checks(root, **kw)
    hard = [r for r in results if r.level == "hard" and r.status == "failed"]
    rec[key] = {"status": "failed" if hard else "ok", "mode": results.mode,
                "checks": [_check_entry(r) for r in results]}
    if hard:
        raise BuildFailed("verify", f"{key}: hard check(s) failed: "
                          + "; ".join(f"{r.name} ({r.n}): {'; '.join(r.all_items[:3])}" for r in hard))


@footer_cache()  # the build's verifies (start, finish) parse each footer once (D731)
def run_build(store_root: str | Path, plan: Plan, *, warn: Callable[[str], None] | None = None,
              now: Callable[[], dt.datetime] | None = None,
              overrides: BuildOverrides | None = None, pass_no: int | None = None,
              first_pass_build_id: str | None = None, verify_full: bool = False,
              comment: str | None = None, upload: bool | None = None, note: str | None = None,
              new_series: bool = False, allow_env_limits: bool = False) -> BuildRecord:
    """Run ``plan`` as one build over the store (module docstring) and return the record as
    written (status ``ok``, ``incomplete``, ``failed``). Never raises for a failed build (the
    record says ``failed``); re-raises
    ``KeyboardInterrupt`` after writing a ``cancelled`` record. ``now`` = the build's clock for
    its own stamps (``started_at``, ``finished_at``); runs stamp themselves. ``overrides``:
    :class:`BuildOverrides`, validated before any run. ``pass_no`` (a build of
    :func:`run_passes`): the record gets ``pass`` and ``first_pass_build_id`` (pass 1's build id;
    pass 1 names itself). ``verify_full``: the full verify at the finish instead of the three parts
    (module docstring, step 5). ``comment``: the operator's ``--comment``, recorded as
    ``comment`` (D322). ``upload``: the build's job and its runs upload (default: the store has a
    remote; recorded as ``upload``, D352). ``new_series``: ``--new-series``; without it a build into a
    series with no ``documents`` while another series has some raises :class:`BuildFailed`
    ``new_series`` **before any record** (module docstring, step 1, D491). ``allow_env_limits``:
    ``--allow-env-limits``; without it a set collector cap/sampling environment variable raises
    :class:`BuildFailed` ``env_limits`` before anything else (D553); with it the record gets ``env_limits``.
    The whole build is one :func:`stack.store.parquet.footer_cache` block (D731)."""
    if (env_why := env_limits_refusal()) is not None and not allow_env_limits:
        raise BuildFailed("env_limits", env_why)
    root = Path(store_root)
    clock = now or (lambda: dt.datetime.now(dt.UTC))
    upload_ = default_upload(open_store(root), upload)
    prepare(root, upload_)
    if not new_series and (why := new_series_refusal(root, plan.series)) is not None:
        raise BuildFailed("new_series", why)
    b = _Build(root, plan, warn or (lambda _m: None), allow_env_limits=allow_env_limits)
    started = clock()
    rec: dict[str, Any] = {
        "build_id": ids.mint(BUILD_ID_KIND), "plan": plan.plan, "plan_digest": plan.plan_digest,
        "gate_digest": plan.gate_digest, "status": "running", "started_at": started.isoformat(),
        "finished_at": None, "inputs_pinned_at": started.isoformat(),
        "series": plan.series, "version_chain": [], "files": {}, "runs": [], "verify": {"status": "not_run", "checks": []},
        "require_complete": list(plan.require_complete), "counts": {}, "warnings": b.warnings,
        "comment": comment, "note": note}
    if (caps := cap_env_set()):
        rec["env_limits"] = caps  # D553: allowed (else refused above); also in each collector run's record
    if pass_no is not None:
        rec["pass"] = pass_no
        rec["first_pass_build_id"] = first_pass_build_id or rec["build_id"]
    ov = overrides or BuildOverrides()
    if ov.given:
        rec["overrides"] = ov.requested()
    b.name, b.build_id, b.upload = build_dir_name(rec), rec["build_id"], upload_
    job = BuildJob(root, b.name, rec, upload=upload_)
    b.job = job

    def progress() -> None:
        rec["runs"] = [r.run_id for r in b.runs]
        job.set_record(rec)

    b.progress = progress

    def finish(status: str, error_class: str | None = None, error: str | None = None) -> BuildRecord:
        try:
            vchain = b.version_chain()
        except Exception as exc:  # noqa: BLE001 - recorded: the record still gets written
            vchain = []
            error = f"{error or ''}\nversion_chain: does not resolve: {type(exc).__name__}: {exc}".strip()
            status, error_class = "failed", error_class or "code_bug"
        rec.update(status=status, files=dict(b.files), version_chain=vchain,
                   runs=[r.run_id for r in b.runs], counts=_counts(b.runs))
        b.record_short(rec)  # on a failed build too: what had stopped short before it failed
        if error_class is not None:
            rec.update(error_class=error_class, error=(error or "")[-4000:])
        rec["finished_at"] = clock().isoformat()
        say(f"build {rec['build_id']} ({plan.plan}): {status}" + (f"[{error_class}]" if error_class else ""))
        for msg in job.finish(rec):
            b.note(msg)
        return BuildRecord(job.json_path, rec)

    try:
        job.start()  # running (D344), PUT when uploading (D352)
        job.start_tick()
        say(f"build {rec['build_id']} ({plan.plan}): started, upload {str(upload_).lower()}")
        caps = cap_env_set()
        if caps:  # D445: a leftover smoke-test cap truncates collectors and no digest sees it
            b.note("collector cap/sampling env set, it wins over the plan's options and is counted "
                   "`env_override.<VAR>` in each run it caps: "
                   + ", ".join(f"{k}={v}" for k, v in caps.items()))
        refuse_unproduced_records(plan)
        b.applied = apply_overrides(root, plan, ov)
        if b.applied.record is not None:
            rec["overrides"] = dict(b.applied.record)
        refusals = preflight(root, plan, b.applied.skipped)
        if refusals:
            raise BuildFailed("environment", "build-start preflight: " + " | ".join(refusals))
        if targets := target_refusals(plan, b.applied.skipped):
            raise BuildFailed("stage_refused", "accepted-config targets: " + " | ".join(targets))
        docs = series_path("documents", plan.series) if plan.series is not None else None
        ch = None
        if docs is not None and _has_files(root, docs):
            try:
                ch = resolve(root, docs)
            except ChainError as exc:
                raise BuildFailed("chain_error", f"{docs} does not resolve at build start: "
                                  f"{type(exc).__name__}: {exc}") from None
        _verify(root, rec, "verify_start", parts={STRUCTURAL})  # before any run (module docstring, step 1)
        if docs is not None and ch is not None:
            why = compaction_due(plan, ch)
            if why is not None:  # an ordinary fold.compact run, a full on the head (D373)
                b.note(f"fold.compact at build start: {why}")
                rec["compacted_from"] = ch.head_key
                b.run("fold.compact", StageCall(ins=(docs,), series=plan.series, kind="full"), by_rule=True)
        drain = None
        try:
            for line in plan.stages:
                b.run_line(line)
        except _BuildStopped:
            drain = drain_state()
        drained = [r.run_id for r in b.runs if int(r.counts.get("stopped.drain") or 0) > 0]
        if drain is not None or drained:  # D573: a stopped build is incomplete, whatever its runs say
            ran = {r.stage for r in b.runs}
            d = drain or drain_state()
            rec["stopped"] = {**(d.to_json() if d is not None else {"by": "drain"}), "runs_drained": drained,
                              "not_run": [ln.stage for ln in plan.stages
                                          if ln.stage not in ran and ln.stage not in b.applied.skipped]}
            say(f"build {rec['build_id']}: stopped by a drain; not run: "
                f"{', '.join(rec['stopped']['not_run']) or '-'}")
        rec.update(files=dict(b.files), version_chain=b.version_chain(), runs=[r.run_id for r in b.runs],
                   counts=_counts(b.runs))
        short = b.record_short(rec)  # X3 checks the record's lists against the runs
        final = "incomplete" if short or "stopped" in rec else "ok"
        families = sorted({k.split("/", 1)[0] for k in rec["files"]})
        _verify(root, rec, "verify", families=families, build=dict(rec, status=final),
                parts=None if verify_full else BUILD_FINISH_PARTS)
    except JobStartError as exc:
        return finish("failed", "tick_failed", str(exc))
    except BuildFailed as exc:
        return finish("cancelled" if exc.error_class == "tick_failed" else "failed", exc.error_class, str(exc))
    except KeyboardInterrupt:
        finish("cancelled", "operator", "KeyboardInterrupt")
        raise
    except Exception as exc:  # noqa: BLE001 - any orchestration error ends the build failed, recorded
        if is_s3_transient(exc):  # S25-47
            return finish("failed", "s3_transient", redacted_trace(exc))
        return finish("failed", "code_bug", traceback.format_exc())
    else:
        return finish(final)  # an error here is not a build failure to record
    finally:
        job.abandon()


#: Run ``counts`` keys that count network or model calls (summed into a build record's per-run
#: ``calls``): the LLM transport requests (the runner's ``llm_calls``), a collector's HTTP requests,
#: acquire's lane ``fetch`` calls and its arXiv history and version-sweep requests.
CALL_COUNTS: tuple[str, ...] = ("llm_calls", "http_requests", "lane_calls", "arxiv.history.requests",
                                "arxiv.atom_requests")
#: Run ``counts`` keys that count units done without a call (summed into ``carried``): rows or
#: answers carried (gather, triage, annotate), acquire's rows with no due fetch work, a collector's
#: units already covered or not due.
CARRIED_COUNTS: tuple[str, ...] = ("carried", "not_due", "already_covered")


def run_calls(counts: Mapping[str, Any]) -> int:
    """The calls a run made (:data:`CALL_COUNTS` summed; a missing key is 0)."""
    return sum(int(counts.get(k) or 0) for k in CALL_COUNTS)


def run_carried(counts: Mapping[str, Any]) -> int:
    """The units a run did without a call (:data:`CARRIED_COUNTS` summed)."""
    return sum(int(counts.get(k) or 0) for k in CARRIED_COUNTS)


def run_new_rows(rec: RunRecord) -> int:
    """Rows a run changed in a chained family (``documents``, ``sources``, ``coverage``): the
    record's ``version.rows_touched`` when the run wrote such a file and is not a fold (a fold
    derives, ``fold.compact`` rewrites what is there), else 0."""
    if rec.stage.startswith("fold.") or rec.version is None:
        return 0
    chained = any(FAMILIES[k.split("/", 1)[0]].kind not in CHAIN_OF_ONE_KINDS for k in rec.outputs)
    return int(rec.version.get("rows_touched") or 0) if chained else 0


def _counts(runs: list[RunRecord]) -> dict[str, dict[str, Any]]:
    return {r.run_id: {"stage": r.stage, "status": r.status, "error_class": r.error_class,
                       "scope_complete": r.scope_complete,
                       "rows": sum(int(o["row_count"]) for o in r.outputs.values()),
                       "calls": run_calls(r.counts), "carried": run_carried(r.counts),
                       "new_rows": run_new_rows(r)} for r in runs}


# ---------------------------------------------------------------- passes (round 4)
@dataclass
class PassesResult:
    """What :func:`run_passes` did: the build of each pass run, in order, and why it stopped:
    ``None`` (all asked passes ran), ``converged`` (an ``ok`` pass made no call and changed no
    row, so the rest would repeat it), ``no_progress`` (the same of an ``incomplete`` pass: what is
    left cannot move, e.g. a run's budget), ``failed`` (the last pass ended ``failed``) or ``stopped``
    (a drain stopped the last pass, D573)."""

    builds: list[BuildRecord]
    asked: int
    stopped: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.builds) and all(b.status == "ok" for b in self.builds)

    @property
    def final_status(self) -> str | None:
        """The last pass's build status (``ok``, ``incomplete``, ``failed``)."""
        return self.builds[-1].status if self.builds else None


def pass_converged(build: BuildRecord) -> bool:
    """A pass that a further pass would repeat: the build ended ``ok`` or ``incomplete`` and every
    run in it made no call (``calls`` 0, :data:`CALL_COUNTS`) and changed no row of a chained
    family (``new_rows`` 0, :func:`run_new_rows`: folds excluded). Read from the record's per-run
    ``counts``. An ``incomplete`` pass that converged made no progress (:class:`PassesResult`)."""
    if build.status not in ("ok", "incomplete"):
        return False
    return all(int(c.get("calls") or 0) == 0 and int(c.get("new_rows") or 0) == 0
               for c in (build.data.get("counts") or {}).values())


def run_passes(store_root: str | Path, plan: Plan, passes: int, *, warn: Callable[[str], None] | None = None,
               now: Callable[[], dt.datetime] | None = None, overrides: BuildOverrides | None = None,
               on_pass: Callable[[BuildRecord], None] | None = None, verify_full: bool = False,
               comment: str | None = None, upload: bool | None = None, note: str | None = None,
               new_series: bool = False, allow_env_limits: bool = False) -> PassesResult:
    """``stack build <plan> --passes N``: up to ``passes`` complete builds of ``plan`` in sequence,
    each with its own record (``pass``, ``first_pass_build_id``). Every pass runs the whole plan
    (no skip rule: the stages are idempotent on ``ok`` rows, so a pass only does what is new or
    due, and window collectors' failures get their retry). Pass *k+1* starts when pass *k* ended
    ``ok`` or ``incomplete`` (a capped stage resumes in the next pass); its prior build is pass
    *k*'s (every line reads its series directories' heads, D373). ``overrides``:
    ``--skip``/``--filter``/``--max-usd`` apply to every pass (the budget is per pass). Stops on a
    ``failed`` pass, and early after a pass that :func:`pass_converged` (``converged`` or
    ``no_progress``, :class:`PassesResult`). ``on_pass`` is called with each pass's record as it
    ends (the CLI prints a line). ``verify_full``: every pass's verify (:func:`run_build`). ``new_series``:
    every pass's (a pass 1 that wrote no ``documents`` leaves the series new for pass 2); without it pass 1
    raises the series guard's :class:`BuildFailed` (D491). ``allow_env_limits``: every pass's (D553)."""
    if passes < 1:
        raise ValueError(f"--passes {passes}: at least 1")
    ov = overrides or BuildOverrides()
    out = PassesResult(builds=[], asked=passes)
    first: str | None = None
    for k in range(1, passes + 1):
        b = run_build(store_root, plan, warn=warn, now=now, overrides=ov,
                      pass_no=k, first_pass_build_id=first, verify_full=verify_full, comment=comment,
                      upload=upload, note=note, new_series=new_series, allow_env_limits=allow_env_limits)
        first = first or b.build_id
        out.builds.append(b)
        if on_pass is not None:
            on_pass(b)
        if b.status not in ("ok", "incomplete"):
            out.stopped = "failed"
            break
        if b.data.get("stopped"):  # D573: a drained pass starts no further pass
            out.stopped = "stopped"
            break
        if k < passes and pass_converged(b):
            out.stopped = "converged" if b.status == "ok" else "no_progress"
            break
    return out


def pass_summary(builds: Sequence[BuildRecord]) -> list[dict[str, Any]]:
    """Per stage (plan order of first appearance), per pass: ``{stage, passes: [{calls, carried,
    new_rows, runs}]}`` summed over the stage's runs in that pass's record (``counts``)."""
    order: list[str] = []
    per: dict[str, list[dict[str, int]]] = {}
    for i, b in enumerate(builds):
        for c in (b.data.get("counts") or {}).values():
            st = str(c["stage"])
            if st not in per:
                order.append(st)
                per[st] = [{"calls": 0, "carried": 0, "new_rows": 0, "runs": 0} for _ in builds]
            cell = per[st][i]
            cell["runs"] += 1
            for key in ("calls", "carried", "new_rows"):
                cell[key] += int(c.get(key) or 0)
    return [{"stage": st, "passes": per[st]} for st in order]


# ---------------------------------------------------------------- dry run (T5.5)
def placeholder(key: str) -> str:
    """How :func:`dry_run` names a file this build would write (``files.<key>``)."""
    return f"<this build: {key}>"


def _dry_writes(line: StageLine, calls: Sequence[StageCall], plan: Plan) -> list[str]:
    """The ``files`` keys a stage line's runs would fill (for :func:`dry_run`'s placeholders)."""
    keys = [c.corpus_key for c in calls if c.corpus_key is not None]
    if line.stage.startswith("annotate."):
        keys += list(stage_writes(line.stage, plan))
    elif phase_of(line.stage) == "triage":
        keys.append("triage_evidence")
    return keys


def dry_run(store_root: str | Path, plan: Plan, *, overrides: BuildOverrides | None = None,
            new_series: bool = False, allow_env_limits: bool = False) -> dict[str, Any]:
    """What :func:`run_build` would run, resolved against the store now, **writing nothing** (no
    run, record, blob or file): whether compaction is due on ``documents/<series>/``, and per stage
    line and call its ``--in`` paths (``path`` and ``file`` = the directory's last published file
    now, ``""`` for an empty directory, or a :func:`placeholder` for this build's own outputs), its
    ``--out`` paths, series, kind, the effective filter, ``budget.max_usd``, ``options.max_rows``,
    ``repeat`` and whether ``--skip`` skips it. A line whose inputs would not resolve gets ``error``
    (the build would fail there); ``preflight`` lists what :func:`preflight` finds; ``new_series`` is the
    series guard's verdict (D491): ``None`` when the series has ``documents`` (or no series has), else
    ``{why, allowed}`` (``allowed`` = the ``new_series`` flag given: the build would run; else it would be
    refused); ``env_limits`` the env-limits guard's (D553): ``None`` when no collector cap/sampling variable
    is set, else ``{why, values, allowed}``. Invalid overrides raise :class:`BuildFailed` (:func:`apply_overrides`)."""
    root = Path(store_root)
    refuse_unproduced_records(plan)
    applied = apply_overrides(root, plan, overrides or BuildOverrides())
    out: dict[str, Any] = {
        "plan": plan.plan, "plan_digest": plan.plan_digest, "gate_digest": plan.gate_digest,
        "series": plan.series, "overrides": dict(applied.record) if applied.record is not None else None,
        "compaction": None, "warnings": [], "stages": [],
        "preflight": preflight(root, plan, applied.skipped) + target_refusals(plan, applied.skipped),
        "new_series": (None if (why := new_series_refusal(root, plan.series)) is None
                       else {"why": why, "allowed": new_series}),
        "env_limits": (None if (env_why := env_limits_refusal()) is None
                       else {"why": env_why, "values": cap_env_set(), "allowed": allow_env_limits})}
    docs = series_path("documents", plan.series) if plan.series is not None else None
    if docs is not None and _has_files(root, docs):
        try:
            ch = resolve(root, docs)
        except ChainError as exc:
            raise BuildFailed("chain_error", f"{docs} does not resolve: {type(exc).__name__}: {exc}") from None
        why = compaction_due(plan, ch)
        out["compaction"] = {"documents_head": ch.head_key, "due": why is not None, "why": why}
    built: dict[str, dict[str, Any]] = {}
    for line in plan.stages:
        eff = applied.line_of(line)
        rep = repeat_of(line)
        base = {"stage": line.stage, "skipped": line.stage in applied.skipped,
                "max_usd": eff.budget.max_usd if eff.budget is not None else None,
                "max_rows": line.options.get("max_rows"), "repeat": rep.max_runs if rep is not None else None}
        if base["skipped"]:
            out["stages"].append({**base, "call": None, "inputs": [], "outs": [], "filter": None})
            continue
        try:
            calls = [applied.call_of(line, c) for c in stage_inputs(root, plan, line, built, out["warnings"].append)]
        except (BuildFailed, StageError) as exc:
            cls = exc.error_class if isinstance(exc, BuildFailed) else "stage_refused"
            out["stages"].append({**base, "call": None, "inputs": [], "outs": [], "filter": None,
                                  "error": f"{cls}: {exc}"})
            continue
        for i, call in enumerate(calls):
            out["stages"].append({
                **base, "call": i, "repeat": base["repeat"] if i == 0 else None,
                "inputs": [{"path": p, "file": p if p.startswith("<") or p.endswith(".parquet")
                            else head_of(root, p.rstrip("/"))} for p in call.ins],
                "outs": list(call.outs), "series": call.series, "kind": call.kind or "full",
                "filter": call.filter if call.filter is not None else line.filter})
        built.update({k: {"path": placeholder(k), "file_id": placeholder(k)} for k in _dry_writes(line, calls, plan)})
    return out


def find_plan(ref: str | Path, *, plans_dirs: tuple[Path, ...] | None = None) -> Path:
    """``<plan>`` of ``stack build``: a path to a plan file, or a plan name resolved to
    ``configs/plans/<name>.yaml`` under the current directory, then under the repository."""
    p = Path(ref)
    if p.suffix in (".yaml", ".yml") or len(p.parts) > 1:
        return p
    from stack.stage_runner import REPO_ROOT

    dirs = plans_dirs or (Path("configs/plans"), REPO_ROOT / "configs" / "plans")
    for d in dirs:
        cand = d / f"{ref}.yaml"
        if cand.exists():
            return cand
    return dirs[0] / f"{ref}.yaml"


__all__ = [
    "BUILD_ERROR_CLASSES",
    "BUILD_ID_KIND",
    "BUILD_STATUS",
    "CALL_COUNTS",
    "CARRIED_COUNTS",
    "HALTED_REPEAT_MIN_OK",
    "INCOMPLETE_REASONS",
    "PHASE_WRITES",
    "AppliedOverrides",
    "BuildFailed",
    "BuildOverrides",
    "BuildRecord",
    "PassesResult",
    "StageCall",
    "apply_overrides",
    "compaction_due",
    "dry_run",
    "failed_permanent_note",
    "find_plan",
    "halt_ends_repeat",
    "incomplete_reasons",
    "line_series",
    "pass_converged",
    "pass_summary",
    "placeholder",
    "preflight",
    "repeat_of",
    "run_build",
    "run_calls",
    "run_carried",
    "run_new_rows",
    "run_passes",
    "series_path",
    "stage_inputs",
    "stage_writes",
    "target_refusals",
]
