"""``stack verify [--build REF | --series S] [--no-build-set] [--structural] [--full] [--in data/<family>/<series>/]…
[--run RUN_ID]… [--names N]… [--json]`` (§7.9, Appendix B, D78): run :func:`stack.checks.verify.run_checks` and
print one row per check (``code name level status error_class n items``, Appendix B order), the
:func:`~stack.checks.verify.summary` line and the :func:`~stack.checks.verify.mode_line`.

**What runs** (:mod:`stack.checks.verify`, its parts). By default **a build's check**: every check
on the files the build's runs wrote (the file checks, the pair checks against their base views, V7,
V8; part ``files``) and the build checks on that build (X2-X25 less the structural ones; part
``build_set``, dropped by ``--no-build-set``). The build is ``--build`` (a build id, a record path
or ``latest``: the newest finished build, ``ok`` or ``incomplete``, D703), else the newest finished build
of ``--series S``, else the newest finished build (with ``--in``, of those directories' series). ``--run RUN_ID`` (repeatable) checks those runs' files
instead of the build's. ``--structural``: only the store-wide structural checks (S1 from footers,
V11, X1 without a re-hash, X5, X23), which every build also runs at its start and its finish.
``--full``: every check on every file, every local data file re-hashed; slow, run it explicitly
(after changing a check or the registry, before a release), never during a build. A check of a part
not run is ``skipped[<part>]``.

``--in`` (D379) restricts the file and chain checks to the given series directories
(repeatable); without it every series directory of the store is checked. X1 (record↔file
consistency) always runs over the whole store; X5 without a build reads the one ``documents``
directory selected (several: none). A family directory without a series is refused. The reference
build of the build checks (X2, X10, X14, ...) is ``--build``, else the newest finished build: with
``--in``, the newest of those directories' series (a build record's ``series``), and ``--in``
directories of several series are refused; without ``--in``, the newest of the store (S25-39).
``--json`` carries the mode as ``mode`` and the files part's file count as ``scope_files``.

``--release DIR`` (D647) checks one release directory instead (an export's ``--out`` directory or a
store's ``releases/<tag>/``) with :func:`stack.checks.fold_checks.validate_release`: one line per problem,
then ``release DIR: ok`` or ``release DIR: N problem(s)`` (exit 1); no store is read.

Exit status: 1 when any ``hard`` check is ``failed`` (an unknown check name, or a store root that
does not exist, is an ``error:``, exit 1 too); 0 otherwise (``warn`` and ``skipped`` do not fail the verb).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from stack.checks.chain import MixedSeriesError
from stack.checks.verify import BUILD_SET, CHECK_PARTS, STRUCTURAL, mode_line, run_checks, summary
from stack.cli.common import StoreOpt, echo_json, fail, require_store, resolve_globals
from stack.fold import FoldError
from stack.fold.release import resolve_build
from stack.runs import newest_finished_build
from stack.store.layout import family_of_dir, parse_dir_ref, series_dir

#: Items shown per row in the table (``--json`` carries all of them).
TABLE_ITEMS = 2


def _verify_release(path: Path, as_json: bool) -> None:
    from stack.checks.fold_checks import validate_release

    if not path.is_dir():
        fail(f"--release {path}: not a directory")
    problems = validate_release(path)
    if as_json:
        echo_json({"release": str(path), "status": "failed" if problems else "ok", "problems": problems})
    else:
        for item in problems:
            typer.echo(item)
        typer.echo(f"release {path}: " + (f"{len(problems)} problem(s)" if problems else "ok"))
    if problems:
        raise typer.Exit(1)


def verify(
    ctx: typer.Context,
    ins: Annotated[list[str] | None, typer.Option(
        "--in", help="Check only this series directory's files and chain, data/<family>/<series>/; repeatable "
        "(X1 always runs over all).")] = None,
    build: Annotated[str | None, typer.Option(
        "--build", help="Reference build of the build checks (X2, X10, X14, ...): a build id, a record path "
        "(builds/<build>/build.json.zst) or latest (the newest ok or incomplete one); default latest (with --in, of those directories' "
        "one series). Its runs' files are the ones checked.")] = None,
    series: Annotated[str | None, typer.Option(
        "--series", help="Check the newest ok or incomplete build of this series (instead of --build).")] = None,
    runs: Annotated[list[str] | None, typer.Option(
        "--run", help="Check the files this run wrote instead of the build's; repeatable.")] = None,
    no_build_set: Annotated[bool, typer.Option(
        "--no-build-set", help="Leave out the build checks (X2-X25): only the checks of the build's files.")] = False,
    structural: Annotated[bool, typer.Option(
        "--structural", help="Only the store-wide structural checks: footers, records, chains, blobs.")] = False,
    names: Annotated[list[str] | None, typer.Option(
        "--names", help="Run only these checks (Appendix B names, e.g. schema_equal); repeatable or "
        "comma-separated.")] = None,
    full: Annotated[bool, typer.Option(
        "--full", help="Every check on every file, data files re-hashed (slow; never during a build).")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print every result as JSON (all items).")] = False,
    release: Annotated[Path | None, typer.Option(
        "--release", help="Check one release or export directory (release.json, corpus.parquet, blobs/) "
        "instead of the store.")] = None,
    store: StoreOpt = None,
) -> None:
    """Run the checks of Appendix B over the store; exit 1 when a hard check failed."""
    if release is not None:
        _verify_release(release, as_json)
        return
    g = resolve_globals(ctx, store, None)
    require_store(g.store, create=False)  # S25-51: a store that does not exist is not "all ok"
    wanted = [n for v in names or [] for n in v.split(",") if n] or None
    try:
        dirs = [parse_dir_ref(d) for d in ins or []]
    except ValueError as exc:
        fail(f"--in: {exc}")
    if (full and structural) or ((full or structural) and (no_build_set or runs)):
        fail("--full and --structural run alone (not with each other, --no-build-set or --run)")
    if build is not None and series is not None:
        fail("--build or --series, not both")
    rec = None
    if series is not None:
        hit = newest_finished_build(Path(g.store), series=series)
        if hit is None:
            fail(f"--series {series}: no ok or incomplete build of that series")
        rec = hit[1]
    try:
        if build is not None:
            rec = resolve_build(Path(g.store), build)
    except FoldError as exc:
        fail(f"--build: {exc}")
    parts = None if full else {STRUCTURAL} if structural else CHECK_PARTS - ({BUILD_SET} if no_build_set else set())
    try:
        results = run_checks(g.store, families=sorted({family_of_dir(f) for f, _ in dirs}) if dirs else None,
                             dirs=[series_dir(f, s) for f, s in dirs] if dirs else None, build=rec,
                             names=wanted, parts=parts, runs=runs or None)
    except KeyError as exc:
        fail(str(exc.args[0]) if exc.args else str(exc))
    except MixedSeriesError as exc:
        fail(str(exc))
    hard_failed = [r for r in results if r.level == "hard" and r.status == "failed"]
    if as_json:
        echo_json({"summary": summary(results), "mode": results.mode,
                   "scope_files": len(results.scope) if results.scope is not None else None,
                   "checks": [{"code": r.code, "name": r.name, "level": r.level, "status": r.status,
                               "error_class": r.error_class, "n": r.n, "items": r.all_items}
                              for r in results]})
    else:
        rows = [("code", "name", "level", "status", "error_class", "n", "items")]
        for r in results:
            items = "; ".join(r.items[:TABLE_ITEMS]) + (" …" if len(r.all_items) > TABLE_ITEMS else "")
            rows.append((r.code, r.name, r.level, r.status, r.error_class or "-", str(r.n), items))
        widths = [max(len(row[i]) for row in rows) for i in range(6)]
        for row in rows:
            typer.echo("  ".join(c.ljust(w) for c, w in zip(row[:6], widths, strict=True)) + "  " + row[6])
        typer.echo(summary(results))
        typer.echo(mode_line(results))
    if hard_failed:
        raise typer.Exit(1)
