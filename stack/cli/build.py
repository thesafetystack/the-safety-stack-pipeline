"""``stack build <plan> [--store] [--json] [--series S] [--comment C] [--note N] [--skip PAT…] [--filter S=SQL…]
[--max-usd S=USD…] [--passes N] [--verify-full] [--new-series] [--allow-env-limits] [--dry-run]`` (§7.9, A.7; T5.1a, T5.5,
round 4): run a plan as one
build through :func:`stack.build.run_build` (or N builds, ``--passes``) and print its record.

``<plan>`` is a plan name (``configs/plans/<plan>.yaml``, under the current directory, then the
repository) or a path to a plan file; a global ``--plan`` on the command line naming another plan
file is refused (``STACK_PLAN`` from the environment yields to ``<plan>``). Prints ``build_id``, ``status`` (with ``error_class``), the
record's path and ``files.corpus``; ``--json`` dumps the whole record. Warnings go to stderr.
Exit status: 0 when the build is ``ok``, **3** when it is ``incomplete`` (verified, but a stage
stopped short: :data:`EXIT_INCOMPLETE`; one stderr line per stage and its reasons; rerun the build
or use ``--passes`` to resume), 1 otherwise (``failed``, an invalid plan); the error text of a
failed build goes to stderr. A non-dry build refuses a store root that does not exist unless
``--create`` (R6-5, :func:`stack.cli.common.require_store`). ``--verify-full``: the verify at the build's
finish is the full one (every check on every file, data files re-hashed) instead of its three parts
(:mod:`stack.checks.verify`, :mod:`stack.cli.verify`).

``--series S`` replaces the plan's ``series:`` (the directories every line reads and writes by
default, D378); ``--note`` is free text in the record. **The series is document identity** (D491): a
build into a series with no ``documents`` while another series of the store has some is refused before any
record (exit 1; the error names the series that have ``documents``) unless ``--new-series``: ids are minted per
series, so it would re-mint every id and re-pay every collect, triage, fetch and LLM call. ``--dry-run`` prints
the verdict as a ``new_series`` line. **Environment limits** (D553): a build started while a collector
cap/sampling variable is set (``STACK_*_LIMIT``, ``*_FRACTION``, ``*_MAX_QUERIES``, e.g. a leftover
``STACK_COLLECT_LIMIT`` from a smoke test) is refused before any record (exit 1; the error lists them) unless
``--allow-env-limits``, which records them as the build's and each collector run's ``env_limits``;
``--dry-run`` prints an ``env_limits`` line. There is no prior build: each line reads
its series directories' heads and this build's own files (:func:`stack.build.stage_inputs`).

**Overrides** (each repeatable; :class:`stack.build.BuildOverrides`): ``--skip PATTERN`` (fnmatch over stage names), ``--filter STAGE=SQL``, ``--max-usd STAGE=USD``. A
refused override ends the build ``failed[override_refused]`` with a record.
**``--dry-run``** prints what the build would pin (:func:`stack.build.dry_run`: the series,
compaction, per stage line and call the ``--in`` paths with the file each resolves to now, the ``--out`` paths, kind, filter, budget,
``max_rows``, repeat, skipped, and the build-start ``preflight``'s refusals, each a line ``preflight
error: environment: …``) as a table, or JSON with ``--json``, and writes nothing; a refused override is an
error (exit 1) with no record.

**``--passes N``** (default 1; :func:`stack.build.run_passes`): N complete builds of the plan in
sequence, each with its own record (``pass``, ``first_pass_build_id``); pass *k+1* starts when
pass *k* ended ``ok`` or ``incomplete`` and reads the heads it left; ``--skip``, ``--filter`` and ``--max-usd`` to every pass (``--max-usd`` is per pass).
The loop stops on a ``failed`` pass and early after a pass that made no call and changed no row of
a chained family (:func:`stack.build.pass_converged`): ``converged`` after an ``ok`` pass,
``stopped: no progress`` after an ``incomplete`` one, and after a pass a drain stopped (``stopped: drained``,
D573; SIGTERM or ``stack stop``: the build in flight ends ``incomplete`` with a ``stopped: …`` stderr line, exit 3,
and the next ``stack build`` resumes). Prints one line per pass (``pass k/N``,
build id, status, calls, carried, new rows) as it ends, then a per-stage table of
calls/carried/new rows per pass (:func:`stack.build.pass_summary`) and ``final\t<status>``;
``--json`` dumps ``{passes: [records], stopped, final_status}``. Exit status as for one build, of
the last pass. ``--dry-run`` with ``--passes`` describes pass 1 (the later passes resolve their
inputs from the pass before, which does not exist yet).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from stack.build import (
    BuildFailed,
    BuildOverrides,
    BuildRecord,
    dry_run,
    find_plan,
    pass_summary,
    run_build,
    run_passes,
)
from stack.cli.common import (
    CommentOpt,
    CreateOpt,
    NoUploadOpt,
    StoreOpt,
    echo_json,
    fail,
    pairs,
    require_store,
    resolve_globals,
    shown,
)
from stack.plan import PlanError, load_plan
from stack.store.layout import check_series
from stack.store.s3config import S3TransientError

MultiOpt = list[str] | None
#: Exit status of a build (or the last pass) that ended ``incomplete``.
EXIT_INCOMPLETE = 3


def _overrides(skip: MultiOpt, filters: MultiOpt, max_usd: MultiOpt) -> BuildOverrides:
    def unique(flag: str, items: list[tuple[str, str]]) -> dict[str, str]:
        keys = [k for k, _ in items]
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            raise typer.BadParameter(f"{flag} given twice for {', '.join(dup)}")
        return dict(items)

    usd: dict[str, float] = {}
    for stage, v in unique("--max-usd", pairs(max_usd, "--max-usd")).items():
        try:
            usd[stage] = float(v)
        except ValueError:
            raise typer.BadParameter(f"--max-usd {stage}={v}: not a number") from None
    return BuildOverrides(skip=tuple(skip or ()),
                          filters=unique("--filter", pairs(filters, "--filter")), max_usd=usd)


def _cell(v: Any) -> str:
    return "-" if v is None or v == [] else str(v)


def _print_dry(d: dict[str, Any]) -> None:
    typer.echo(f"plan\t{d['plan']}")
    typer.echo(f"series\t{_cell(d.get('series'))}")
    comp = d["compaction"]
    typer.echo("compaction\t" + ("-" if comp is None else
                                 f"due ({comp['why']})" if comp["due"] else f"not due (head {comp['documents_head']})"))
    ns = d.get("new_series")  # the series guard (D491)
    typer.echo("new_series\t" + ("-" if ns is None else
                                 f"allowed by --new-series: {ns['why']}" if ns["allowed"] else f"error: refused: {ns['why']}"))
    refusals = d.get("preflight") or []  # the build-start preflight: any refusal fails the build
    el = d.get("env_limits")  # the env-limits guard (D553)
    typer.echo("env_limits\t" + ("-" if el is None else
                                 f"allowed by --allow-env-limits: {el['why']}" if el["allowed"] else f"error: refused: {el['why']}"))
    typer.echo("\n".join(f"preflight\terror: environment: {m}" for m in refusals) or "preflight\t-")
    typer.echo("stage\tcall\trun\tinputs\touts\tkind\tfilter\tmax_usd\tmax_rows\trepeat")
    for s in d["stages"]:
        run = "skipped" if s["skipped"] else ("error: " + s["error"]) if "error" in s else "run"
        ins = " ".join(i["path"] + ("" if i["file"] == i["path"] else f"→{i['file'] or '(empty)'}")
                       for i in s["inputs"])
        typer.echo("\t".join((s["stage"], _cell(s["call"]), run, _cell(ins or None), _cell(" ".join(s["outs"]) or None),
                              _cell(s.get("kind")), _cell(s["filter"]), _cell(s["max_usd"]), _cell(s["max_rows"]),
                              _cell(s["repeat"]))))
    for w in d["warnings"]:
        typer.echo(f"warning: {w}", err=True)


def _pass_line(b: BuildRecord, asked: int) -> str:
    counts = (b.data.get("counts") or {}).values()
    tot = {k: sum(int(c.get(k) or 0) for c in counts) for k in ("calls", "carried", "new_rows")}
    status = b.status + (f"[{b.error_class}]" if b.error_class else "")
    return (f"pass {b.data.get('pass')}/{asked}\t{b.build_id}\t{status}\tcalls {tot['calls']}\t"
            f"carried {tot['carried']}\tnew_rows {tot['new_rows']}")


def _print_summary(builds: list[BuildRecord]) -> None:
    n = len(builds)
    typer.echo("stage\t" + "\t".join(f"pass{i + 1} calls/carried/new_rows" for i in range(n)))
    for s in pass_summary(builds):
        typer.echo(s["stage"] + "\t" + "\t".join(
            f"{p['calls']}/{p['carried']}/{p['new_rows']}" if p["runs"] else "-" for p in s["passes"]))


def _refuse_other_plan(ctx: typer.Context, given: Path | None, plan: str) -> None:
    """The positional PLAN is the build's plan; a global ``--plan`` on the command line that names
    another plan file is refused rather than ignored (S25-51). ``STACK_PLAN`` from the environment
    is a default and yields to PLAN."""
    parent = ctx.parent
    # By member name: typer >= 0.26 vendors click (``typer._click``), so the source is not
    # ``click.core.ParameterSource``, and an identity test against click's enum would never match.
    source = None if parent is None else parent.get_parameter_source("plan")
    if given is None or getattr(source, "name", None) != "COMMANDLINE":
        return
    a, b = find_plan(given), find_plan(plan)
    if a.resolve() != b.resolve():
        fail(f"--plan {given} and PLAN {plan} name different plans ({a} vs {b}); the build runs PLAN: "
             "drop --plan or make them agree")


def build(
    ctx: typer.Context,
    plan: Annotated[str, typer.Argument(help="Plan name (configs/plans/<plan>.yaml) or a plan file path.")],
    as_json: Annotated[bool, typer.Option("--json", help="Dump the whole build record (or the dry run) as JSON.")] = False,
    series: Annotated[str | None, typer.Option(
        "--series", help="The series every line reads and writes by default, over the plan's `series:` "
        "(D378).")] = None,
    note: Annotated[str | None, typer.Option("--note", help="Free text, recorded in the build record.")] = None,
    skip: Annotated[MultiOpt, typer.Option(
        "--skip", help="PATTERN: do not run the plan lines whose stage matches (fnmatch; repeatable).")] = None,
    filters: Annotated[MultiOpt, typer.Option(
        "--filter", help="STAGE=SQL: ANDed with the stage's own filter (not fold.*, not triage; repeatable).")] = None,
    max_usd: Annotated[MultiOpt, typer.Option(
        "--max-usd", help="STAGE=USD: the annotate.<wf> line's budget.max_usd for this build, at most the "
        "plan's (repeatable).")] = None,
    passes: Annotated[int, typer.Option(
        "--passes", min=1, help="Run N complete builds in sequence (each continues from the series heads the one before left); "
        "stops early when a pass makes no call and changes no row.")] = 1,
    dry: Annotated[bool, typer.Option("--dry-run", help="Print what the build would pin; run and write nothing.")] = False,
    new_series: Annotated[bool, typer.Option(
        "--new-series", help="Allow a build into a series with no documents while another series has some: a new "
        "identity, every id re-minted and every call re-paid (D491).")] = False,
    allow_env_limits: Annotated[bool, typer.Option(
        "--allow-env-limits", help="Run although collector cap/sampling environment variables (STACK_*_LIMIT, "
        "*_FRACTION, *_MAX_QUERIES) are set; their values are recorded (D553).")] = False,
    verify_full: Annotated[bool, typer.Option(
        "--verify-full", help="Run the full verify at the build's finish (every check on every file, data files "
        "re-hashed); default the structural checks plus every check on this build's files and its build set.")] = False,
    create: CreateOpt = False,
    comment: CommentOpt = None,
    no_upload: NoUploadOpt = False,
    store: StoreOpt = None,
) -> None:
    """Run PLAN's stages as one build, verify it, and write its build record."""
    g = resolve_globals(ctx, store, None)
    _refuse_other_plan(ctx, g.plan, plan)
    try:
        p = load_plan(find_plan(plan))
    except PlanError as exc:
        fail(str(exc))
    if series is not None:
        try:
            p = p.model_copy(update={"series": check_series(series)})
        except ValueError as exc:
            fail(f"--series: {exc}")
    ov = _overrides(skip, filters, max_usd)
    if dry:
        try:
            d = dry_run(g.store, p, overrides=ov, new_series=new_series, allow_env_limits=allow_env_limits)
        except BuildFailed as exc:
            fail(f"dry run refused [{exc.error_class}]: {exc}")
        if as_json:
            echo_json({**d, "passes": passes} if passes > 1 else d)
        else:
            if passes > 1:
                typer.echo(f"passes\t{passes} (below: pass 1; each later pass reads the series heads the pass before left)")
            _print_dry(d)
        return
    require_store(g.store, create)
    try:
        if passes > 1:
            _build_passes(g.store, p, passes, ov, as_json, verify_full, comment,
                          upload=False if no_upload else None, note=note, new_series=new_series,
                          allow_env_limits=allow_env_limits)
            return
        res = run_build(g.store, p, warn=lambda m: typer.echo(shown(f"warning: {m}"), err=True), overrides=ov,
                        verify_full=verify_full, comment=comment, note=note, upload=False if no_upload else None,
                        new_series=new_series, allow_env_limits=allow_env_limits)
    except S3TransientError as exc:  # S25-47: the build's start prepare, before its record
        fail(f"[s3_transient] {exc}")
    except BuildFailed as exc:  # D491, D553: the series and env-limits guards, before any record
        fail(f"build refused [{exc.error_class}]: {exc}")
    if as_json:
        echo_json(res.data)
    else:
        corpus = res.files.get("corpus") or {}
        status = res.status + (f"[{res.error_class}]" if res.error_class else "")
        typer.echo(f"build_id\t{res.build_id}")
        typer.echo(f"status\t{status}")
        typer.echo(shown(f"record\t{res.path}"))
        typer.echo(f"corpus\t{corpus.get('path') or '-'}")
    _exit_for(res)


def _exit_for(res: BuildRecord, what: str = "build") -> None:
    """Exit 0 for ``ok``; 3 (:data:`EXIT_INCOMPLETE`) for ``incomplete``, one stderr line per stage
    that stopped short; 1 for ``failed`` with its error on stderr."""
    if res.status == "ok":
        return
    if res.status == "incomplete":
        for e in res.data.get("incomplete") or []:
            typer.echo(f"incomplete: {e['stage']} run {e['run_id']} ({', '.join(e['reasons'])})", err=True)
        if stopped := res.data.get("stopped"):  # D573
            typer.echo(f"stopped: drained ({stopped.get('by')}); not run: {', '.join(stopped.get('not_run') or []) or '-'}",
                       err=True)
        typer.echo(f"note: {what} {res.build_id} is incomplete: verified, released only by `stack release "
                   "--allow-incomplete`; rerun the build (or --passes N) to resume", err=True)
        raise typer.Exit(EXIT_INCOMPLETE)
    typer.echo(shown(f"error: {what} {res.build_id} {res.status}[{res.error_class}]: {res.data.get('error')}"),
               err=True)
    raise typer.Exit(1)


def _build_passes(store: Any, p: Any, passes: int, ov: BuildOverrides, as_json: bool,
                  verify_full: bool = False, comment: str | None = None, *, upload: bool | None = None,
                  note: str | None = None, new_series: bool = False, allow_env_limits: bool = False) -> None:
    res = run_passes(store, p, passes, warn=lambda m: typer.echo(shown(f"warning: {m}"), err=True), overrides=ov,
                     on_pass=None if as_json else lambda b: typer.echo(_pass_line(b, passes)),
                     verify_full=verify_full, comment=comment, note=note, upload=upload, new_series=new_series,
                     allow_env_limits=allow_env_limits)
    if as_json:
        echo_json({"passes": [b.data for b in res.builds], "stopped": res.stopped,
                   "final_status": res.final_status})
    else:
        if res.stopped == "converged":
            typer.echo(f"converged\tafter pass {len(res.builds)}: no call, no changed row; "
                       f"passes {len(res.builds) + 1}-{passes} not run")
        elif res.stopped == "stopped":  # D573
            typer.echo(f"stopped: drained\tin pass {len(res.builds)}; passes {len(res.builds) + 1}-{passes} not run")
        elif res.stopped == "no_progress":
            typer.echo(f"stopped: no progress\tafter pass {len(res.builds)}: incomplete, and it made no call "
                       f"and changed no row (a cap that nothing moves?); passes {len(res.builds) + 1}-{passes} not run")
        _print_summary(res.builds)
        typer.echo(f"final\t{res.final_status}")
    last = res.builds[-1]
    _exit_for(last, f"pass {last.data.get('pass')} build")
