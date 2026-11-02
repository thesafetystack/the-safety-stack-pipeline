"""``stack run <stage> [--in PATH …] [--out DIR …] [--series S] [--part|--full] [--restart] [--new-series] [--no-overrides]
[--allow-env-limits] [--comment C] [--note N] [--filter …] [--only …]`` (D373, D379; §3.1, D32): one run of one stage
through :func:`stack.stage_runner.run_stage`, standalone: whatever a build passes, these flags pass.

``--in`` is a store path: a series directory ``data/<family>/<series>/`` (its last published file's
version), an exact file (that file's version) or a file without a series; ``data/<family>/`` alone
is refused. ``--out`` is a destination ``data/<family>/[<series>/]`` (default: the directory of the
run's input of that family, else ``data/<family>/<--series>/``). ``--part``/``--full`` (default
full), ``--restart`` (a full without a base into a non-empty directory), ``--new-series`` (a ``collect.*``
run with no ``sources`` prior of its own into a series with no ``documents`` while another series has some:
refused without it, D491, since it would re-mint every ``source_id``), ``--no-overrides`` (gather,
``annotate.<wf>`` or ``collect.manual`` without ``--in data/overrides/<series>/`` into a series whose overrides
directory has a file: refused without it, D495, since it would undo the series' corrections),
``--allow-env-limits`` (a ``collect.*`` run while a collector cap/sampling variable, ``STACK_*_LIMIT``,
``*_FRACTION`` or ``*_MAX_QUERIES``, is set: refused without it, D553, since each silently caps the run; with it
the values are recorded as the run's ``env_limits``), ``--comment``
(``[a-z0-9-]+``, in the record and every file name) and ``--note`` (free text, recorded).

Exit status: 0 when the run's final record is ``ok``; 1 when it is ``failed``/``cancelled`` or the
run was refused before dispatch (an input that does not resolve, unknown stage, bad ``--only``,
invalid plan). **No default plan** (R6-5): ``--plan`` or ``STACK_PLAN`` is required; a store root
that does not exist is refused unless ``--create`` (:func:`stack.cli.common.require_store`).
The final record's status and outputs are printed as one JSON line on stdout.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from stack.cli.common import (
    CommentOpt,
    CreateOpt,
    NoUploadOpt,
    PlanOpt,
    StoreOpt,
    load_plan_or_fail,
    require_store,
    resolve_globals,
    shown,
)
from stack.stage_runner import StageError, run_stage
from stack.store.s3config import S3TransientError


def run(
    ctx: typer.Context,
    stage: Annotated[str, typer.Argument(help="Stage name, e.g. gather, collect.arxiv_sweep, echo_test.")],
    ins: Annotated[list[str] | None, typer.Option(
        "--in", help="A series directory data/<family>/<series>/ or an exact data file (repeatable).")] = None,
    outs: Annotated[list[str] | None, typer.Option(
        "--out", help="A destination data/<family>/[<series>/] (repeatable; one per family).")] = None,
    series: Annotated[str | None, typer.Option(
        "--series", help="The series of an output with no --in of its family and no --out naming one.")] = None,
    part: Annotated[bool | None, typer.Option(
        "--part/--full", help="Write parts (on the directory head the run read) or fulls (default).")] = None,
    restart: Annotated[bool, typer.Option(
        "--restart", help="Allow a full without a base into a non-empty directory (D373).")] = False,
    no_overrides: Annotated[bool, typer.Option(
        "--no-overrides", help="Run a stage that reads overrides (gather, annotate.<wf>, collect.manual) without "
        "the series' data/overrides/<series>/ on purpose (D495).")] = False,
    new_series: Annotated[bool, typer.Option(
        "--new-series", help="Allow a collector run that mints ids into a series with no documents while another "
        "series has some (D491).")] = False,
    allow_env_limits: Annotated[bool, typer.Option(
        "--allow-env-limits", help="Run a collector although cap/sampling environment variables (STACK_*_LIMIT, "
        "*_FRACTION, *_MAX_QUERIES) are set; their values are recorded (D553).")] = False,
    note: Annotated[str | None, typer.Option("--note", help="Free text, recorded in the run record.")] = None,
    filter: Annotated[str | None, typer.Option("--filter", help="SQL predicate over the row (stage-defined).")] = None,
    only: Annotated[list[str] | None, typer.Option(
        "--only", help="Run only these substages (D32); repeatable.")] = None,
    create: CreateOpt = False,
    comment: CommentOpt = None,
    no_upload: NoUploadOpt = False,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Run one stage: pin its --in files, dispatch, publish (or fail leaving data/ untouched)."""
    g = resolve_globals(ctx, store, plan_path)
    plan = load_plan_or_fail(g.plan)
    require_store(g.store, create)
    try:
        rec = run_stage(g.store, plan, stage, ins or (), outs or (), filter, only or None,
                        warn=lambda m: typer.echo(shown(f"warning: {m}"), err=True), comment=comment, note=note,
                        series=series, kind=None if part is None else "part" if part else "full",
                        restart=restart, upload=False if no_upload else None, new_series=new_series,
                        no_overrides=no_overrides, allow_env_limits=allow_env_limits)
    except (StageError, NotImplementedError) as exc:
        typer.echo(shown(f"error: {exc}"), err=True)
        raise typer.Exit(1) from None
    except S3TransientError as exc:  # S25-47: the run's prepare, before its record
        typer.echo(shown(f"error: [s3_transient] {exc}"), err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps({"run_id": rec.run_id, "status": rec.status, "error_class": rec.error_class,
                           "scope_complete": rec.scope_complete,
                           "outputs": {k: o["path"] for k, o in rec.outputs.items()}}))
    if rec.status != "ok":
        if rec.error:
            typer.echo(shown(rec.error), err=True)
        raise typer.Exit(1)
