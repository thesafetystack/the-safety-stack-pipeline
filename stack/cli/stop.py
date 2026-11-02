"""``stack stop [--build ID | --run ID] [--now] [--wait SECONDS]`` (D572; ``docs/OPERATE.md``, "Stopping,
cancelling and resuming"): ask a running build or run to stop, from any shell, without its pid.

It writes a **stop request** beside the job's ``-running.json`` marker (:func:`stack.store.job.request_stop`:
``<stem>-stop.json``, ``build-stop.json`` for a build): the local file, and on an S3 store also the S3
object when the job uploads, so a job on another machine reads it. The job reads it at its next tick
(every 30 s). Without ``--now`` it **drains**, as a first SIGTERM does: no new work, in-flight work gets
up to ``--wait`` seconds (default 120), what is done is published, the run ends ``ok`` with
``scope_complete = false`` (reason ``stopped``) and the build ends ``incomplete`` with ``stopped`` in its
record, starting no further run or pass. ``--now`` is the hard cancel (a second SIGTERM, Ctrl-C): the run
in flight publishes nothing and ends ``cancelled[operator]``. Targets: ``--run`` (a run id or stem, a
build's or standalone), ``--build`` (a build id or directory name; its running run drains with it), or
neither: every live build and live standalone run of the store. A finished, dead or unknown target is
refused (``error: …``, exit 1) before anything is written.
"""

from __future__ import annotations

from typing import Annotated

import typer

from stack.cli.common import StoreOpt, fail, resolve_globals
from stack.store.job import DRAIN_WAIT_S, PushError, request_stop


def stop(
    ctx: typer.Context,
    build: Annotated[str | None, typer.Option("--build", help="The build to stop (build id or directory name).")] = None,
    run: Annotated[str | None, typer.Option("--run", help="The run to stop (run id or stem).")] = None,
    now: Annotated[bool, typer.Option(
        "--now", help="Hard cancel: the run in flight publishes nothing and ends cancelled (operator).")] = False,
    wait: Annotated[float | None, typer.Option(
        "--wait", min=0, help=f"Seconds in-flight work may take after the stop (default {DRAIN_WAIT_S:.0f}); "
        "then it is abandoned as not attempted.")] = None,
    store: StoreOpt = None,
) -> None:
    """Ask a running build or run to drain (publish what it completed and stop), or with --now to cancel."""
    g = resolve_globals(ctx, store, None)
    if not g.store.is_dir():
        fail(f"no store at {g.store}")
    if now and wait is not None:
        fail("--wait is the drain's bounded wait; --now waits for nothing")
    try:
        done = request_stop(g.store, build=build, run=run, mode="now" if now else "drain", wait_s=wait)
    except PushError as exc:
        fail(str(exc))
    for label, key in done:
        typer.echo(f"stop requested ({'now' if now else 'drain'}): {label} ({key}); its next tick (within 30 s) "
                   "reads it")


__all__ = ["stop"]
