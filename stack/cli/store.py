"""``stack store push <job>`` and ``stack store push --check`` (D358;
``docs/OPERATE.md``, "``stack store push`` and ``--check``"): upload a finished local job, and list
what discarding this machine would lose.

* ``push <job>`` (a run or build id, a run stem ``<head>.<stage>`` or a build directory name):
  uploads the finished local job in publish order (each run: its data files, then its json,
  then its log, then its marker deleted; a build: its runs in start order, then its own json and
  log). Refused (``error: …``, exit 1, nothing of the refused run uploaded) when a run's inputs
  are not named by ``ok`` records on S3, or when an output's base is no longer its chain's head on
  S3 (``claim`` against a fresh index of uploaded records). A record whose S3 copy already
  has its status is skipped.
* ``push --check``: one line per local record whose S3 copy is absent or of another status
  (``upload: true`` jobs that did not finish uploading, ``upload: false`` jobs never pushed);
  exit 1 when there is any, 0 when nothing local is missing on S3.

Both are refused on a local-only store (no S3 remote to push to or compare with).
"""

from __future__ import annotations

from typing import Annotated

import typer

from stack.cli.common import StoreOpt, fail, resolve_globals
from stack.store.chain import ChainError
from stack.store.job import PushError, check_push, push_job

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="An S3 store's local mirror: upload a finished job, check what is not on S3.")


@app.command("push")
def push(
    ctx: typer.Context,
    job: Annotated[str | None, typer.Argument(
        help="Run or build id, run stem or build directory name of a finished local job.")] = None,
    check: Annotated[bool, typer.Option(
        "--check", help="List local records whose S3 copy is absent or of another status; exit 1 if any.")] = False,
    store: StoreOpt = None,
) -> None:
    """Upload a finished local job (JOB) in publish order, or with --check list what S3 lacks."""
    g = resolve_globals(ctx, store, None)
    if not g.store.is_dir():
        fail(f"no store at {g.store}")
    if check == (job is not None):
        fail("give a job to push, or --check (not both)")
    if check:
        try:
            lines = check_push(g.store)
        except PushError as exc:
            fail(str(exc))
        for line in lines:
            typer.echo(line)
        typer.echo(f"{len(lines)} local record(s) not on S3 as they are here" if lines
                   else "nothing local is missing on S3")
        if lines:
            raise typer.Exit(1)
        return
    assert job is not None
    try:
        pushed = push_job(g.store, job)
    except (PushError, ChainError) as exc:
        fail(str(exc))
    for key in pushed:
        typer.echo(f"pushed {key}")
    if not pushed:
        typer.echo(f"{job}: already on S3")


__all__ = ["app", "push"]
