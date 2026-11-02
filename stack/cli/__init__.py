"""``stack``: the operator CLI (§7.9's verbs; D18: one module per verb under ``stack/cli/``).

Global options: ``--store`` (a path or ``s3://<bucket>/<prefix>``, D321; default ``v4/store-dev``
under the repository, whatever the cwd; env ``STACK_STORE``) and ``--plan`` (**no default**; env ``STACK_PLAN``), a relative value relative to
the current directory; every verb also accepts them after the verb. A verb that needs a plan
refuses without one; a writing verb refuses a store root that does not exist unless ``--create``
(R6-5: ``stack/cli/common.py``).
Verbs: ``run`` (T1.3); ``head`` (D375, replaces ``latest``), ``status`` (jobs and liveness from the store index, D365), ``ls``,
``show`` (one document from the series' heads, ``stack/cli/show.py``, D541), ``chain orphan``,
``fold``, ``verify`` (T1.6); ``release`` (T5.2; ``export``, its local-directory alias, D647), ``build`` (T5.1a), ``diff`` (T5.3);
``eval <task>|score|spend`` (track E, ``stack/cli/eval.py``); ``migrate upcast`` (D43, schema
bumps: ``stack/cli/migrate.py``); ``acquire reopen`` (R4-2, ``stack/cli/acquire.py``);
``overrides add`` (D338, ``stack/cli/overrides.py``); ``store push`` (D358, ``stack/cli/store.py``); ``stop``
(D572, ``stack/cli/stop.py``: drain or cancel a running build or run by a stop request; SIGTERM drains too, D570).
``gc`` and ``chain unlock`` are gone (D356, D365: no locks, no data gc).

Rewrite of v3's ``stack/cli.py`` (run/sweep/accept/status over Postgres); nothing in v3 imports
``stack.cli`` (D41). Errors a verb expects (refusals, invalid plans) print ``error: …`` and exit 1, and so
do store-config and S3 client errors no verb handles (:class:`~stack.cli.common.StackGroup`, D544,
masked); anything else is a traceback.
"""

from __future__ import annotations

import contextlib
import signal
from pathlib import Path
from typing import Annotated

import typer

from stack.cli import acquire as acquire_verb
from stack.cli import build as build_verb
from stack.cli import chain as chain_verb
from stack.cli import diff as diff_verb
from stack.cli import eval as eval_verb
from stack.cli import fold as fold_verb
from stack.cli import head as head_verb
from stack.cli import ls as ls_verb
from stack.cli import migrate as migrate_verb
from stack.cli import overrides as overrides_verb
from stack.cli import release as release_verb
from stack.cli import run as run_verb
from stack.cli import show as show_verb
from stack.cli import status as status_verb
from stack.cli import stop as stop_verb
from stack.cli import store as store_verb
from stack.cli import verify as verify_verb
from stack.cli.common import DEFAULT_STORE, PLAN_HELP, STORE_HELP, Globals, StackGroup

app = typer.Typer(cls=StackGroup, add_completion=False, no_args_is_help=True,
                  help="stack: file-based pipeline over a store of parquet chains.")


__all__ = ["Globals", "app", "main", "seed_contact"]


@app.callback()
def _globals(
    ctx: typer.Context,
    store: Annotated[str, typer.Option("--store", envvar="STACK_STORE", help=STORE_HELP)] = str(DEFAULT_STORE),
    plan: Annotated[Path | None, typer.Option("--plan", envvar="STACK_PLAN", help=PLAN_HELP)] = None,
) -> None:
    # the store is resolved by each verb's resolve_globals (an s3:// URI to its mirror, D321)
    ctx.obj = Globals(store=Path(store), plan=plan, store_arg=store)


app.command("run")(run_verb.run)
app.command("fold")(fold_verb.fold)
app.command("verify")(verify_verb.verify)
app.command("head")(head_verb.head)
app.command("status")(status_verb.status)
app.command("stop")(stop_verb.stop)
app.command("ls")(ls_verb.ls)
app.command("show")(show_verb.show)
app.command("release")(release_verb.release)
app.command("export")(release_verb.export)
app.command("build")(build_verb.build)
app.command("diff")(diff_verb.diff)
app.add_typer(chain_verb.app, name="chain")
app.add_typer(eval_verb.app, name="eval")
app.add_typer(migrate_verb.app, name="migrate")
app.add_typer(acquire_verb.app, name="acquire")
app.add_typer(overrides_verb.app, name="overrides")
app.add_typer(store_verb.app, name="store")


def _on_sigterm(signum: int, frame: object) -> None:
    """D570 (was R2-8's plain cancel): the first SIGTERM while a run or build is running **drains**
    it (:func:`stack.store.job.request_drain`: no new work, in-flight work gets a bounded wait, what is
    done is published, the build starts nothing more); a second SIGTERM, or one with no job running,
    ends it like Ctrl-C: ``cancelled[operator]`` records, LLM reservations released. A second one
    within :data:`stack.store.job.SIGTERM_REPEAT_S` of the drain's start is the same stop (``uv run``
    forwards a process-group SIGTERM, so ``stack`` gets it twice). Lock-free and without buffered
    I/O (a handler runs between any two bytecodes of the main thread)."""
    import os
    import time

    from stack.store import job

    if not job.job_active() or job.hard_cancel_requested():  # a `stack stop --now` sends this SIGTERM
        raise KeyboardInterrupt(f"signal {signum}")
    if job.request_drain("signal"):
        msg = (b"stack: SIGTERM: draining: no new work; in-flight work gets a bounded wait, then what is done "
               b"is published (a second SIGTERM after 5 s, or Ctrl-C, cancels at once)\n")
    else:
        d = job.drain_state()
        if d is None or time.monotonic() - d.since >= job.SIGTERM_REPEAT_S:
            raise KeyboardInterrupt(f"signal {signum}")
        msg = b"stack: SIGTERM again within 5 s of the drain: the same stop (still draining)\n"
    with contextlib.suppress(OSError):
        os.write(2, msg)


def seed_contact() -> None:
    """``STACK_MAILTO`` from ``$STACK_ENV_FILE`` (by name, :func:`stack.llm.provider_key`) when the
    environment lacks it, so the collectors, the lookups and acquire's oa lane (Unpaywall refuses an
    anonymous caller) see the contact address under ``STACK_ENV_FILE=.env`` alone. A contact
    address, not a credential; never printed. Keys are never seeded this way (round 5, T-RUN)."""
    import os

    if os.environ.get("STACK_MAILTO") or not os.environ.get("STACK_ENV_FILE"):
        return
    from stack.llm import provider_key

    try:
        val = provider_key("STACK_MAILTO")
    except Exception:  # noqa: BLE001 — an unreadable env file is reported by the stage that needs a key
        return
    if val:
        os.environ["STACK_MAILTO"] = val


def main() -> None:
    """Console-script entry point (``[project.scripts] stack = "stack.cli:main"``). Installs the
    SIGTERM handler (:func:`_on_sigterm`: drain, then cancel; D570), seeds ``STACK_MAILTO`` from
    ``$STACK_ENV_FILE`` (:func:`seed_contact`) and captures fd 2 into the active job's log
    (:func:`stack.store.job.capture_stderr`, D357; never in-process under a test runner)."""
    from stack.store.job import capture_stderr

    signal.signal(signal.SIGTERM, _on_sigterm)
    seed_contact()
    with capture_stderr():
        app()


if __name__ == "__main__":
    main()
