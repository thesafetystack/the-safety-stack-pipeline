"""``stack migrate upcast --family F [--file ID | --in data/<family>/<series>/ ...] [--dry-run]`` (D208, amended by D295;
:mod:`stack.migrate`): materialise stored files at their family's current format version.

**Optional.** Every reader reads an older format version upcast on its own (D295: the version
table :mod:`stack.schemas.upcasters`, the reader :func:`stack.store.parquet.read_family`), and a
chain may mix versions. Run a migration to drop the per-read upcast cost of a large older version,
or to hand a duckdb consumer current files (a raw read sees a file's own version). The older files
stay (no data gc, D356).

* a chained family (``documents``, ``sources``, ``coverage``): every chain of the family with a
  file not at the current format gets one ``fold.compact`` run writing its view as a full file on
  its head (chains already current are reported and skipped); ``--in data/<family dir>/<series>/``
  (repeatable; S25-38) plans and migrates only those series directories, so one series that does
  not read no longer blocks the others;
* a chain-of-one family (``<wf>``, ``<wf>_evidence``): ``--file <file_id>`` names the old file;
  one ``migrate.upcast`` run writes a new full file of the family, the old one stays. ``corpus``
  files are not migrated (they read upcast; a new build writes current ones).

``stack migrate rekey [--apply] [--report FILE]`` (url-normalisation.md §3; :func:`stack.migrate.plan_rekey`):
re-type every ``sources`` row's keys and ``canonical_url`` under the current URL rules. Default is
the dry run: the report (rows re-keyed per channel and kind, the merges and splits a gather over the
re-keyed sources would make against the ``documents`` head) as JSON to ``--report`` (or stdout),
one summary line on stderr; nothing written. ``--apply`` writes the changed rows as one part per
``sources`` chain, one ``migrate.rekey`` run each, and prints their JSON lines; a re-key with nothing to
change writes no run. Run ``gather`` afterwards so ``documents`` reflects the new keys.

``--dry-run`` prints one JSON line per target (per file ``v<k> → v<N>`` and the step kinds, the old
and new schema digests, the added and relaxed field paths of the additive steps, the row count) and
writes nothing. A real run prints one JSON line per run (``run_id``, ``status``, ``file_id`` of the
new file, ``outputs``); exit 1 when a run is not ``ok``. A file that does not read (newer, unknown
format, behind an ``Incompatible`` step), an unknown family or file, a single file already
current, or a ``--file`` (or its pair sibling) that is not its directory's current non-excluded
head (D421) is an ``error:`` (exit 1) before any run exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from stack.cli.common import StoreOpt, fail, require_store, resolve_globals, shown
from stack.migrate import (
    RekeyError,
    UpcastError,
    apply_rekey,
    plan_rekey,
    plan_upcast,
    rekey_report,
    upcast_chain,
    upcast_file,
)
from stack.runs import RunRecord
from stack.store.chain import ChainError
from stack.store.parquet import SchemaMismatch

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Migrate stored files: format upcasts (optional: reads upcast on their own), URL re-keys.")


def _emit(rec: RunRecord) -> bool:
    out = {"run_id": rec.run_id, "stage": rec.stage, "status": rec.status, "error_class": rec.error_class,
           "file_id": next((o["file_id"] for o in rec.outputs.values()), None),
           "outputs": {k: o["path"] for k, o in rec.outputs.items()}}
    typer.echo(json.dumps(out))
    if rec.status != "ok" and rec.error:
        typer.echo(rec.error, err=True)
    return rec.status == "ok"


@app.command("upcast")
def upcast(
    ctx: typer.Context,
    family: Annotated[str, typer.Option("--family", help="The family to migrate (documents, screen_evidence, …).")],
    file: Annotated[str | None, typer.Option(
        "--file", help="File id of the old file (chain-of-one families: <wf>, <wf>_evidence).")] = None,
    ins: Annotated[list[str] | None, typer.Option(
        "--in", help="A series directory data/<family>/<series>/ to migrate (chained families; repeatable; "
                     "default: every series).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would change; write nothing.")] = False,
    store: StoreOpt = None,
) -> None:
    """Materialise FAMILY's older-format files at the current format (optional: every reader upcasts on read)."""
    g = resolve_globals(ctx, store, None)
    require_store(g.store, create=False)  # R6-5: a migration never creates a store
    try:
        plans = plan_upcast(g.store, family, file, ins or ())
    except (UpcastError, ChainError, SchemaMismatch) as exc:
        fail(str(exc))
    if dry_run:
        for p in plans:
            typer.echo(json.dumps(p.as_json(), sort_keys=True))
        return
    todo = [p for p in plans if not p.current]
    for p in plans:
        if p.current:
            what = f"chain {p.chain_id}" if p.chain_id else p.file_id
            typer.echo(f"{family}: {what} is already at the current format; skipped", err=True)
    if not todo:
        fail(f"{family}: nothing to upcast")
    ok = True
    for p in todo:
        try:
            rec = upcast_chain(g.store, p.chain_id) if p.chain_id else upcast_file(g.store, family, str(p.file_id))
        except (UpcastError, ChainError, SchemaMismatch) as exc:
            fail(str(exc))
        ok = _emit(rec) and ok
    if not ok:
        raise typer.Exit(1)


@app.command("rekey")
def rekey(
    ctx: typer.Context,
    apply: Annotated[bool, typer.Option("--apply", help="Write the re-keyed rows (default: dry run).")] = False,
    report: Annotated[Path | None, typer.Option(
        "--report", help="Write the JSON report here (default: stdout on a dry run).")] = None,
    store: StoreOpt = None,
) -> None:
    """Re-key every sources row under the current URL rules (stack.urls); dry run by default."""
    g = resolve_globals(ctx, store, None)
    require_store(g.store, create=False)  # R6-5: a migration never creates a store
    try:
        plan = plan_rekey(g.store)
    except (RekeyError, ChainError) as exc:
        fail(str(exc))
    rep = rekey_report(plan)
    text = json.dumps(rep, indent=1, sort_keys=True, default=str)
    if report is not None:
        report.write_text(text + "\n")
    elif not apply:
        typer.echo(shown(text))
    b, a = rep["baseline"], rep["rekeyed"]
    typer.echo(f"rekey {shown(g.store)}: {rep['rows_rekeyed']} of {rep['sources_rows']} sources rows change; "
               f"documents {rep['head_documents_live']} live in the head, {b['documents']} predicted before "
               f"({len(b['merges'])} merges, {len(b['splits'])} splits vs head), {a['documents']} after "
               f"({len(a['merges'])} merges, {len(a['splits'])} splits vs head)", err=True)
    if not apply:
        return
    if not rep["rows_rekeyed"]:
        typer.echo("rekey: nothing to change; no run written", err=True)
        return
    ok = True
    for rec in apply_rekey(plan):
        ok = _emit(rec) and ok
    if not ok:
        raise typer.Exit(1)
