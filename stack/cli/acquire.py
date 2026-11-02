"""``stack acquire reopen --lane <lane> [--error-class <c>] [--since <date>] [--filter …] [--dry-run]``
(round 5, R4-2): after a known outage, make the failed fetch roles of one lane due now.

A role that failed (``failed_transient``, ``failed_permanent``, ``quarantined``) in lane ``<lane>``,
of error class ``<c>`` when given (for ``quarantined[cap_reached]`` also the transient class the
quarantine came from) and last attempted on or after ``<date>`` when given, gets ``retry_after :=
now`` (:class:`stack.fetchcycle.Reopen`). Without ``--dry-run`` this is one ``acquire`` run (input
``documents=documents``, the plan's ``acquire`` line plus ``options.reopen``, recorded in the run's
``config_digest``), which fetches those roles and anything else due, and writes its part as any
``acquire`` run does (counts ``reopen.roles``, ``reopen.rows``). ``--dry-run`` reads the
``documents`` head and prints what would be re-opened (by status and error class, a few examples);
it writes nothing and needs no plan.

Without this command a terminal role is rechecked on its own 90 days after it ended
(:data:`stack.fetchcycle.TERMINAL_RECHECK`). Exit 0 on a dry run or an ``ok`` run, 1 otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from typing import Annotated

import typer

from stack.cli.common import (
    PlanOpt,
    StoreOpt,
    echo_json,
    fail,
    load_plan_or_fail,
    require_store,
    resolve_globals,
)
from stack.fetchcycle import Reopen
from stack.plan import StageLine
from stack.stage_runner import StageError, run_stage

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Acquire operations: re-open failed fetch roles after an outage.")
STAGE = "acquire"


def _spec(lane: str, error_class: str | None, since: str | None) -> Reopen:
    raw: dict[str, str] = {"lane": lane}
    if error_class:
        raw["error_class"] = error_class
    if since:
        raw["since"] = since
    try:
        return Reopen.parse(raw)
    except ValueError as exc:
        fail(str(exc))


@app.command("reopen")
def reopen_cmd(
    ctx: typer.Context,
    lane: Annotated[str, typer.Option("--lane", help="The lane whose failed roles to re-open (arxiv, oa, http).")],
    in_: Annotated[str, typer.Option("--in", help="The documents version to re-open on: a series directory "
                                     "data/documents/<series>/ (its last file; the output extends it) or an exact file.")],
    error_class: Annotated[str | None, typer.Option(
        "--error-class", help="Only roles of this error class (dead, network, http_5xx, …; for a quarantined "
        "role also the class it was quarantined for).")] = None,
    since: Annotated[str | None, typer.Option(
        "--since", help="Only roles last attempted on or after this UTC date (YYYY-MM-DD) or ISO instant.")] = None,
    filter: Annotated[str | None, typer.Option("--filter", help="SQL predicate over the row (as stack run).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be re-opened; write nothing.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Make the matching failed roles due now (module docstring)."""
    g = resolve_globals(ctx, store, plan_path)
    spec = _spec(lane, error_class, since)
    if dry_run:
        _dry_run(g.store, in_, spec, filter, as_json)
        return
    plan = load_plan_or_fail(g.plan)
    require_store(g.store, False)
    line = plan.stage_line(STAGE) or StageLine.model_validate({"stage": STAGE})
    line = line.model_copy(update={"options": {**line.options, "reopen": spec.as_option()}})
    stages = [line if s.stage == STAGE else s for s in plan.stages]
    if plan.stage_line(STAGE) is None:
        stages.append(line)
    plan = plan.model_copy(update={"stages": stages})
    try:
        rec = run_stage(g.store, plan, STAGE, [in_], filter=filter,
                        kind=line.out.kind if line.out is not None else None,
                        warn=lambda m: typer.echo(f"warning: {m}", err=True))
    except (StageError, NotImplementedError) as exc:
        fail(str(exc))
    out = {"run_id": rec.run_id, "status": rec.status, "error_class": rec.error_class,
           "scope_complete": rec.scope_complete, "reopen": spec.as_option(),
           "reopen.roles": rec.counts.get("reopen.roles", 0), "reopen.rows": rec.counts.get("reopen.rows", 0),
           "outputs": {k: o["path"] for k, o in rec.outputs.items()}}
    typer.echo(json.dumps(out))
    if rec.status != "ok":
        if rec.error:
            typer.echo(rec.error, err=True)
        raise typer.Exit(1)


def _dry_run(root: object, ref: str, spec: Reopen, filter: str | None, as_json: bool) -> None:
    from pathlib import Path

    from stack.store.chain import ChainError, read_view, resolve

    root = Path(str(root))
    if not (root / "data").is_dir():
        fail(f"{root} is not a store (no data/)")
    try:
        view = read_view(resolve(root, ref))  # older format versions read upcast (D295)
    except (ChainError, FileNotFoundError, ValueError) as exc:
        fail(f"{ref}: {exc}")
    only: set[str] | None = None
    if filter is not None:
        from workflows.acquire.main import filtered_ids

        try:
            only = filtered_ids(view, filter)
        except StageError as exc:
            fail(str(exc))
    by: Counter[str] = Counter()
    rows: set[str] = set()
    examples: list[str] = []
    for row in view.select(["doc_id", "content"]).to_pylist():
        c = row["content"]
        if c is None or (only is not None and row["doc_id"] not in only):
            continue
        for r in c["roles"] or []:
            if spec.matches(r):
                by[f"{r['status']}[{r['error_class']}]"] += 1
                rows.add(row["doc_id"])
                if len(examples) < 5:
                    examples.append(f"{row['doc_id']} {r['source_id']} {r['content_role']} {r['status']}"
                                    f"[{r['error_class']}] last {r['last_attempt_at']}: {(r['error'] or '')[:100]}")
    out = {"dry_run": True, "reopen": spec.as_option(), "roles": sum(by.values()), "rows": len(rows),
           "by_status": dict(sorted(by.items())), "examples": examples,
           "now": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    if as_json:
        echo_json(out)
        return
    typer.echo(f"would re-open {out['roles']} role(s) on {out['rows']} row(s) ({json.dumps(spec.as_option())})")
    for k, v in out["by_status"].items():
        typer.echo(f"  {k}: {v}")
    for e in examples:
        typer.echo(f"  e.g. {e}")


__all__ = ["app", "reopen_cmd"]
