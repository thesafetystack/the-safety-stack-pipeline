"""Run records for data files a test placed by hand (T-STORE A1, D345).

Chains resolve from the ``ok`` run records' ``outputs`` (``stack.store.chain``), so a file a test
writes outside the runner is in no chain until a record names it. :func:`record_files` writes
that record: one ``ok`` final record per run naming the given files, each output built from the
file's own footer (the chain link, digests, row count), as the runner's publish hook would.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from stack.runs import OutputInfo, RunRecord, file_digest, write_final_record
from stack.store.parquet import read_header

AT = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)
#: The series the test worlds write (``tests.synth.rows.header``'s default).
SERIES = "main"


def sdir(family_dir: str, series: str = SERIES) -> str:
    """``data/<family_dir>/<series>/``: a series directory as ``--in``/``--out`` take it."""
    return f"data/{family_dir}/{series}/"


def run_line(store: str | Path, plan: Any, stage: str, *, series: str = SERIES,
             built: dict[str, dict[str, Any]] | None = None, ins: Sequence[str] | None = None,
             add: Sequence[str] = (), **kw: Any) -> RunRecord | None:
    """Run ``stage`` standalone with exactly the ``--in``/``--out``/``--series``/``--part|--full``
    a build line of ``plan`` with ``series:`` set to ``series`` (default the test worlds' ``main``,
    over the plan's own) would pass
    (:func:`stack.build.stage_inputs`; ``built``: that build's files so far, e.g. ``{"corpus_input":
    {"path": ...}}`` for annotate). ``ins`` replaces the computed ``--in``s, ``add`` extends them;
    ``kw`` goes to :func:`stack.stage_runner.run_stage` and overrides the line's flags. ``None``
    when the build would not run the line."""
    from stack.build import stage_inputs
    from stack.plan import StageLine
    from stack.stage_runner import run_stage

    p = plan.model_copy(update={"series": series})  # the test world's series, over the plan's own
    line = p.stage_line(stage) or StageLine.model_validate({"stage": stage})
    calls = stage_inputs(store, p, line, built or {}, lambda _m: None)
    if not calls:
        return None
    c = calls[0]
    args: dict[str, Any] = {"filter": c.filter, "series": c.series, "kind": c.kind, "restart": c.restart,
                            "comment": c.comment}
    args.update(kw)
    return run_stage(store, p, stage, [*(c.ins if ins is None else ins), *add], c.outs, **args)


def write_series(store: str | Path, family_dir: str, family: str, table: Any, fields: Any, *, kind: str = "full",
                 series: str | None = SERIES, at: dt.datetime | None = None, comment: str | None = None,
                 restart: bool = False, finished_at: dt.datetime | None = None, on_published: Any = None) -> Any:
    """Publish ``table`` as a ``kind`` file into ``data/<family_dir>/<series>/`` on the directory's
    last file, as a run that read that directory would (``base`` = ``after`` = the head; an empty
    directory: neither): the replacement of v4.1's ``write_full``/``write_part(…, expected_head=)``.
    ``restart``: a full without a base (``after`` still the head); ``series=None``: a file without a
    series (``data/<family_dir>/``, full only). The name's stamp is ``at`` (default ``finished_at``,
    else now) after the tie guard; ``family`` must be ``family_dir``'s schema family."""
    from stack.store.chain import head_of, write_file
    from stack.store.layout import family_of_dir, make_data_file, series_dir, tie_guard

    assert family_of_dir(family_dir) == family, f"{family_dir} is not a {family} directory"
    head = head_of(store, series_dir(family_dir, series)) if series is not None else ""
    stamp = tie_guard(at or finished_at or dt.datetime.now(dt.UTC), head[:16] or None)
    dest = make_data_file(family_dir, series, stamp, fields.run_id, kind, comment=comment)
    return write_file(store, dest, table, fields, base="" if restart else head, after=head,
                      finished_at=finished_at, on_published=on_published)


def output_of(store: Path, path: Path) -> OutputInfo:
    """The ``outputs`` entry of the data file ``path`` (under ``store/data/``), from its footer."""
    h = read_header(path)
    out: OutputInfo = {"file_id": h["file_id"], "path": path.relative_to(store).as_posix(),
                       "file_digest": file_digest(path), "row_count": int(h["row_count"]),
                       "schema_digest": h["schema_digest"], "chain_id": h["chain_id"],
                       "series": h.get("series", ""), "kind": h["kind"],
                       "base": h.get("base", ""), "after": h.get("after", ""), "comment": h.get("comment", "")}
    if "family_version" in h:
        out["family_version"] = int(h["family_version"])
    return out


def record_files(store: Path, paths: Sequence[Path], *, stage: str | None = None, run_id: str | None = None,
                 at: dt.datetime = AT, status: str = "ok", plan: str = "test", build: str | None = None,
                 build_id: str | None = None) -> RunRecord:
    """Write one final run record (``status``, default ``ok``) whose ``outputs`` name ``paths``
    (keyed by family, ``<family>/<chain_id>`` when a family repeats); ``stage`` and ``run_id``
    default to the first file's header."""
    heads = [read_header(p) for p in paths]
    outs = [output_of(store, p) for p in paths]
    fams = [h["family"] for h in heads]
    outputs = {(f if fams.count(f) == 1 else f"{f}/{o['chain_id']}"): o for f, o in zip(fams, outs, strict=True)}
    first = heads[0]
    stage_ = stage or first["stage"]
    rec = RunRecord(run_id=run_id or first["run_id"], stage=stage_, writer=first.get("writer", stage_),
                    plan=plan, status=status, started_at=at, heartbeat_at=at, finished_at=at,
                    code_digest=first.get("code_digest", "none"), config_digest=first.get("config_digest", "none"),
                    scope_complete=True, outputs=outputs if status == "ok" else {}, build_id=build_id,
                    error_class="code_bug" if status == "failed" else None)
    write_final_record(store, rec, build)
    return rec


def rewrite_record(store: Path, rec: RunRecord) -> Path:
    """Rewrite the existing record of ``rec.run_id`` in place with ``rec`` (a test editing a final
    record, which the store never does; ``write_final_record`` refuses)."""
    from stack.runs import write_json_zst
    from stack.store.index import store_index

    idx = store_index(store)
    entry = idx.run(rec.run_id)
    assert entry is not None, f"no record of run {rec.run_id}"
    return write_json_zst(idx.store.path(entry.key), rec.to_json())


def record_hook(store: Path, *, at: dt.datetime = AT, plan: str = "test"):
    """An ``on_published`` hook for :func:`write_series`/``write_file``/``publish``/``run_fold`` called
    outside the runner: one ``ok`` record naming the published files (run id and stage from
    their header), as the runner's hook would write it."""

    def hook(pubs: list) -> None:
        record_files(store, [p.path for p in pubs], at=at, plan=plan)

    return hook


def build_json_path(store: Path, at: dt.datetime, build_id: str, plan: str, comment: str | None = None) -> Path:
    """``builds/<head>.<plan>/build.json.zst`` for a hand-written build record (directory made)."""
    from stack.store.layout import StoreLayout, build_name, make_head

    path = StoreLayout(store).build_json(build_name(make_head(at, build_id, comment), plan))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
