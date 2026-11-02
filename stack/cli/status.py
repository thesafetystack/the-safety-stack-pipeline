"""``stack status [--limit N] [--json]`` (§7.9; D365; ``docs/OPERATE.md``, "``stack status``"):
the store's jobs, read from the store index alone (:mod:`stack.store.index`; no pid or lock files).

A **job** is a build or a standalone run (``stack run``, ``release``, ``overrides add``, ``chain
orphan``, ``migrate upcast``), newest first by start; ``--limit`` (default 20, 0 = all) caps the
list, the summary line counts all of them. Per job:

- **state**: the record's status (``failed[<error_class>]`` when failed or cancelled; ``incomplete[stopped]``
  for a build a drain stopped and ``ok[stopped]`` for a drained standalone run, D573); a ``running``
  record adds its liveness: ``live <age>`` (the age of its marker, i.e. of the last good tick),
  ``DEAD <age>`` (marker older than 5 min: a crash, a kill or a lost network; nothing finalises it,
  its last json is its state) or ``unknown`` (no marker: it never ticked, or a marker delete
  raced);
- **spend** from the run records' ``spend`` (a build: summed over its runs): settled
  ``usd_micro``, plus the open reservations of in-flight calls and batches (a dead run's last tick);
- **upload**: on a mirror, ``s3`` (the record is on S3), ``local-only`` (an ``upload: false`` job
  not pushed: ``stack store push``) or ``push pending`` (an ``upload: true`` final record whose
  json S3 lacks or whose marker S3 still holds: a crash before its end PUTs; the next uploading
  job's recovery or ``stack store push`` sends it). Nothing on a local-only store.

A build also lists its runs that are not finished. Then ``warning: repeated_inflight (X11)``
for a key in flight in two dead runs, and the free disk of the store's volume.

On a mirror the index is refreshed first: one LIST of ``builds/`` and ``runs/``, and a GET of each
record json not local or changed; nothing else is downloaded. Garbage and lost data files are
``stack verify``'s X1, blobs its X5.

Exit status 0 whatever it finds (a report, not a check); an unreadable record is an ``error:``,
exit 1.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any

import typer

from stack.cli.common import PlanOpt, StoreOpt, echo_json, fail, resolve_globals
from stack.store.index import BuildEntry, RecordIndexError, RunEntry, store_index
from stack.store.layout import parse_job_name
from stack.store.remote import mask_buckets, open_store, shown_uri

LimitOpt = Annotated[int, typer.Option("--limit", min=0, help="Show the N newest jobs (0 = all).")]


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def age(seconds: float) -> str:
    """``42s``, ``7m``, ``3h05m``, ``2d04h``."""
    s = max(0, int(seconds))
    if s < 120:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def usd(micro: int) -> str:
    return f"${micro / 1e6:,.2f}"


def _spend(entries: list[RunEntry]) -> tuple[int, int]:
    """Settled and open micro-USD summed over the records' ``spend``."""
    settled = sum(int(v) for e in entries for v in ((e.record.spend or {}).get("usd_micro") or {}).values())
    open_ = sum(int((e.record.spend or {}).get("reserved_open_usd_micro") or 0) for e in entries)
    return settled, open_


def _state(status: str | None, error_class: str | None, liveness: str, marker_at: dt.datetime | None,
           now: dt.datetime) -> str:
    if status != "running":
        return f"{status}[{error_class}]" if error_class else str(status)
    if liveness == "live" and marker_at is not None:
        return f"running, live {age((now - marker_at).total_seconds())}"
    if liveness == "dead" and marker_at is not None:
        return f"running, DEAD {age((now - marker_at).total_seconds())}"
    return "running, unknown"


def _upload(final: bool, upload: bool, uploaded: bool, marker_key: str,
            remote: dict[str, Any] | None) -> str | None:
    """The job's S3 state (module docstring); ``None`` on a local-only store."""
    if remote is None:
        return None
    if upload and final and (not uploaded or marker_key in remote):
        return "push pending"
    return "s3" if uploaded else "local-only"


def _started(value: Any, name: str) -> dt.datetime:
    """A build record's ``started_at``, else the minute its name carries."""
    if isinstance(value, str) and value:
        at = dt.datetime.fromisoformat(value)
        return at if at.tzinfo is not None else at.replace(tzinfo=dt.UTC)
    return dt.datetime.strptime(parse_job_name(name).head.utc, "%Y%m%dT%H%MZ").replace(tzinfo=dt.UTC)


def _run_job(e: RunEntry, now: dt.datetime, remote: dict[str, Any] | None) -> dict[str, Any]:
    r = e.record
    settled, open_ = _spend([e])
    marker = e.key.removesuffix(".json.zst") + "-running.json"
    return {"kind": "run", "name": e.stem, "id": r.run_id, "what": r.stage, "status": r.status,
            "error_class": r.error_class, "liveness": e.liveness,
            "marker_at": e.marker_at.isoformat() if e.marker_at else None,
            "state": _state(r.status, r.error_class or ("stopped" if r.counts.get("stopped.drain") else None),
                            e.liveness, e.marker_at, now),
            "started_at": r.started_at.isoformat(), "spend_usd_micro": settled, "open_usd_micro": open_,
            "upload": r.upload, "uploaded": e.uploaded,
            "s3": _upload(r.is_final, r.upload, e.uploaded, marker, remote)}


def _build_job(b: BuildEntry, runs: list[RunEntry], now: dt.datetime,
               remote: dict[str, Any] | None) -> dict[str, Any]:
    rec = b.record
    settled, open_ = _spend(runs)
    final = b.status != "running"
    marker = b.key.removesuffix("build.json.zst") + "build-running.json"
    active = [_run_job(e, now, remote) for e in runs if e.liveness != "finished"]
    return {"kind": "build", "name": b.name, "id": b.build_id, "what": rec.get("plan"), "status": b.status,
            "error_class": rec.get("error_class"), "liveness": b.liveness,
            "marker_at": b.marker_at.isoformat() if b.marker_at else None,
            "state": _state(b.status, rec.get("error_class") or ("stopped" if rec.get("stopped") else None),
                            b.liveness, b.marker_at, now),
            "started_at": _started(rec.get("started_at"), b.name).isoformat(), "spend_usd_micro": settled,
            "open_usd_micro": open_, "upload": bool(rec.get("upload")), "uploaded": b.uploaded,
            "s3": _upload(final, bool(rec.get("upload")), b.uploaded, marker, remote),
            "runs": len(runs), "active_runs": active}


def collect_status(store_root: Path, limit: int = 0, now: dt.datetime | None = None) -> dict[str, Any]:
    """The report as data (``--json`` prints it as is); ``limit`` 0 = every job. A mirror's index
    is refreshed first (module docstring)."""
    now = now or dt.datetime.now(dt.UTC)
    store = open_store(store_root)
    idx = store_index(store_root, refresh=store.remote is not None)
    remote = idx.remote_objects()
    by_build: dict[str, list[RunEntry]] = defaultdict(list)
    for e in idx.runs:
        if e.build is not None:
            by_build[e.build].append(e)
    jobs = [_build_job(b, by_build.get(b.name, []), now, remote) for b in idx.builds]
    jobs += [_run_job(e, now, remote) for e in idx.runs if e.build is None]
    jobs.sort(key=lambda j: (j["started_at"], j["id"]), reverse=True)
    by_liveness = {k: sum(1 for j in jobs if j["liveness"] == k) for k in ("live", "dead", "unknown", "finished")}
    seen: dict[str, list[str]] = {}
    for e in idx.runs:
        if e.liveness == "dead":
            for key in dict.fromkeys(e.record.inflight):
                seen.setdefault(key, []).append(e.run_id)
    return {
        "store": shown_uri(store.remote.uri) if store.remote is not None else str(store_root),
        "root": mask_buckets(str(store_root)),
        "mirror": store.remote is not None,
        "jobs_total": len(jobs),
        "by_liveness": by_liveness,
        "local_only": sum(1 for j in jobs if j["s3"] == "local-only"),
        "push_pending": sum(1 for j in jobs if j["s3"] == "push pending"),
        "jobs": jobs[:limit] if limit else jobs,
        "repeated_inflight": [{"key": k, "runs": ids} for k, ids in sorted(seen.items()) if len(ids) >= 2],
        "free_bytes": shutil.disk_usage(store_root).free,
    }


def _line(j: dict[str, Any], indent: str) -> str:
    spend = usd(j["spend_usd_micro"]) + (f" +{usd(j['open_usd_micro'])} open" if j["open_usd_micro"] else "")
    utc = j["name"].split("_", 1)[0]
    parts = [f"{indent}{utc}  {j['kind']:<5}  {j['id']}  {j['what']:<22}  {j['state']:<26}  {spend}"]
    if j["s3"] is not None:
        parts.append(f"  {j['s3']}")
    return "".join(parts).rstrip()


def _print(s: dict[str, Any]) -> None:
    echo = typer.echo
    where = f" (mirror {s['root']})" if s["mirror"] else ""
    echo(f"store {s['store']}{where}")
    lv = s["by_liveness"]
    extra = ""
    if s["mirror"]:
        extra = f"; {s['local_only']} local-only, {s['push_pending']} push pending"
    shown = len(s["jobs"])
    more = f"; newest {shown} shown (--limit)" if shown < s["jobs_total"] else ""
    echo(f"jobs: {s['jobs_total']} ({lv['live']} live, {lv['dead']} dead, {lv['unknown']} unknown, "
         f"{lv['finished']} finished){extra}{more}")
    for j in s["jobs"]:
        echo(_line(j, "  "))
        for r in j.get("active_runs", []):
            echo(_line(r, "      "))
    for w in s["repeated_inflight"]:
        echo(f"warning: repeated_inflight (X11): key {w['key']!r} inflight in dead runs {', '.join(w['runs'])}")
    echo(f"free disk {human(s['free_bytes'])}")


def status(
    ctx: typer.Context,
    limit: LimitOpt = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Jobs (builds and standalone runs), newest first: status, liveness (live/DEAD with the last tick's age),
    spend, S3 state; a mirror refreshes its index first (one LIST, record jsons only)."""
    g = resolve_globals(ctx, store, plan_path)
    if not g.store.exists():
        fail(f"no store at {g.store}")
    try:
        s = collect_status(g.store, limit)
    except RecordIndexError as exc:
        fail(str(exc))
    if as_json:
        echo_json(s)
    else:
        _print(s)


__all__ = ["collect_status", "status"]
