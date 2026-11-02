"""``stack diff <build_a> <build_b> [--family F] [--limit N] [--json]`` (§7.9, A.7, D17): what
changed between two builds, family by family, row by row.

``<build>`` is a build id, a build-record path, or ``latest`` (the newest ``ok`` or ``incomplete``
build of the ``--plan`` file's plan, D703), resolved as ``stack verify --build`` does
(:func:`stack.fold.release.resolve_build`). By id or path any status is accepted (a failed build's
record names files too).

**Units.** One per key of either record's ``files`` (D17): ``documents``,
``sources/<chain_id>``, ``coverage/<chain_id>``, ``corpus``, ``corpus_input``,
``corpus_everything``, ``<wf>``, ``<wf>_evidence``, ``triage_evidence``. ``--family F`` keeps
the keys equal to ``F`` or whose family part (before ``/``) is ``F`` (``--family sources`` = every
``sources`` chain). A chained family's entry names the head file; the unit compares the **view
at that head** (``StoreView.view_at``, the reader's union-and-select), a chain of one compares
the file. A unit present in one record only is ``only_a``/``only_b`` (all its rows count as
removed/added); a file that neither the mirror nor S3 holds, or a view that no longer resolves (a fork), is
``unreadable`` with the reason, and its rows are not compared.

**Rows** are keyed by the family's key (§2.3): ``(doc_id, item_key)`` for ``<wf>`` (``item_key =
''`` at document grain), ``doc_id`` for ``documents``/``corpus``, ``source_id`` for ``sources``,
``(channel, query_digest, window_start_at, window_end_at)`` for ``coverage``, ``evidence_id``
for ``<wf>_evidence``. A row present on both sides is ``changed`` when its **comparison** differs
(:func:`comparison`, pinned per family kind, D133):

* ``<wf>``, ``<wf>_evidence``: ``(status, result_digest)``;
* ``documents``, ``corpus``: ``state``, ``identity.members_digest``, ``metadata.fields_digest``,
  ``content.current_text_status``, ``content.current_text_sha256``, and per annotation present
  (every typed field, every ``other[]`` entry by ``workflow`` and ``payload.item_key``) its
  ``(status, basis_digest)``; no ``run_id``/``updated_at`` stamp is compared, so a row only
  re-written (a compaction, a carried row) is unchanged;
* ``sources``: ``(state, status, record_digest)``;
* ``coverage``: ``(status, seen, kept, is_exhausted, next_cursor)``.

Output: per unit ``added / removed / changed / unchanged`` and up to ``--limit`` (default 20,
0 = all) keys per category with the changed fields' before → after. Exit 0 whatever differs (a
report); an unresolvable build reference is ``error:``, exit 1.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import pyarrow as pa
import typer

from stack.checks.chain import StoreView
from stack.cli.common import (
    PlanOpt,
    StoreOpt,
    echo_json,
    fail,
    load_plan_or_fail,
    resolve_globals,
)
from stack.fold import FoldError
from stack.fold.release import resolve_build
from stack.schemas.families import FAMILIES, FamilySpec
from stack.store.layout import parse_data_key

DEFAULT_LIMIT = 20

#: Top-level columns each family kind's comparison reads (the rest are never decoded).
_COLUMNS: dict[str, tuple[str, ...]] = {
    "wf": ("status", "result_digest"),
    "evidence": ("status", "result_digest"),
    "sources": ("state", "status", "record_digest"),
    "coverage": ("status", "seen", "kept", "is_exhausted", "next_cursor"),
    "documents": ("state", "identity", "metadata", "content", "annotations"),
    "corpus": ("state", "identity", "metadata", "content", "annotations"),
    "overrides": ("target", "key", "item_key", "kind", "field", "payload", "tier", "revokes"),
}


def _plain(v: Any) -> Any:
    return v.isoformat() if isinstance(v, dt.datetime) else v


def _sub(row: Mapping[str, Any], block: str, field: str) -> Any:
    b = row.get(block)
    return None if b is None else b.get(field)


def comparison(kind: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The pinned comparison of one row (module docstring): ``{field: value}``, stamps excluded."""
    if kind in ("documents", "corpus"):
        out: dict[str, Any] = {
            "state": row.get("state"),
            "identity.members_digest": _sub(row, "identity", "members_digest"),
            "metadata.fields_digest": _sub(row, "metadata", "fields_digest"),
            "content.current_text_status": _sub(row, "content", "current_text_status"),
            "content.current_text_sha256": _sub(row, "content", "current_text_sha256"),
        }
        anns = row.get("annotations") or {}
        for name in sorted(k for k in anns if k != "other"):
            a = anns[name]
            if a is not None:
                out[f"annotations.{name}"] = (a.get("status"), a.get("basis_digest"))
        for a in anns.get("other") or []:
            item = (a.get("payload") or {}).get("item_key") or ""
            out[f"annotations.other[{a.get('workflow')}/{item}]"] = (a.get("status"), a.get("basis_digest"))
        return out
    return {c: _plain(row.get(c)) for c in _COLUMNS[kind]}


def _rows(spec: FamilySpec, table: pa.Table) -> dict[tuple[Any, ...], dict[str, Any]]:
    cols = list(spec.key) + [c for c in _COLUMNS[spec.kind] if c not in spec.key]
    sel = table.select(cols).to_pylist()
    return {tuple(_plain(r[k]) for k in spec.key): comparison(spec.kind, r) for r in sel}


def _load(view: StoreView, entry: Mapping[str, Any] | None) -> tuple[FamilySpec | None, dict | None, str | None]:
    """``(spec, rows, reason)``: the entry's rows (the view at a chained head), or ``reason``
    when they cannot be read."""
    if entry is None:
        return None, None, None
    fid = entry.get("file_id")
    df = view.file(str(fid)) if fid else None
    if df is None and entry.get("path"):
        try:
            df = view.file(parse_data_key(str(entry["path"])).file_id)
        except ValueError:
            df = None
    if df is None:
        return None, None, f"file {fid or entry.get('path')} not in data/ (collected or orphaned)"
    spec = df.spec
    if spec is None or spec.kind not in _COLUMNS:
        return None, None, f"file {df.file_id}: family {df.family!r} has no comparison"
    tbl = view.table(df) if df.is_chain_of_one else view.view_at(df.file_id)
    if tbl is None:
        return spec, None, f"file {df.file_id}: view does not resolve (a base collected or missing)"
    return spec, _rows(spec, tbl), None


def _key_text(key: tuple[Any, ...]) -> str:
    return " | ".join("''" if k == "" else str(k) for k in key)


def diff_builds(store_root: str | Path, rec_a: Mapping[str, Any], rec_b: Mapping[str, Any], *,
                families: list[str] | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """The diff of two build records as data (``--json`` prints it as is). Per unit: ``status``
    (``compared``, ``only_a``, ``only_b``, ``unreadable``), ``family``, ``file_a``/``file_b``,
    ``counts`` ``{added, removed, changed, unchanged}`` and the first ``limit`` keys of each
    (``changed`` entries carry ``{field: [a, b]}`` of the fields that differ)."""
    view = StoreView(store_root, build={})
    fa, fb = dict(rec_a.get("files") or {}), dict(rec_b.get("files") or {})
    keys = sorted(set(fa) | set(fb))
    if families:
        keys = [k for k in keys if k in families or k.split("/", 1)[0] in families]
    cap = None if limit <= 0 else limit
    units: dict[str, Any] = {}
    for k in keys:
        ea, eb = fa.get(k), fb.get(k)
        unit: dict[str, Any] = {"file_a": (ea or {}).get("file_id"), "file_b": (eb or {}).get("file_id")}
        spec_a, rows_a, why_a = _load(view, ea)
        if ea is not None and eb is not None and unit["file_a"] == unit["file_b"]:
            spec_b, rows_b, why_b = spec_a, rows_a, why_a
        else:
            spec_b, rows_b, why_b = _load(view, eb)
        spec = spec_a or spec_b
        unit["family"] = spec.name if spec else k.split("/", 1)[0]
        if why_a or why_b:
            unit["status"] = "unreadable"
            unit["reason"] = "; ".join(f"{side}: {w}" for side, w in (("a", why_a), ("b", why_b)) if w)
            units[k] = unit
            continue
        unit["status"] = "compared" if ea is not None and eb is not None else ("only_a" if eb is None else "only_b")
        ra, rb = rows_a or {}, rows_b or {}
        added = sorted(set(rb) - set(ra), key=_key_text)
        removed = sorted(set(ra) - set(rb), key=_key_text)
        changed, unchanged = [], 0
        for key in sorted(set(ra) & set(rb), key=_key_text):
            a, b = ra[key], rb[key]
            if a == b:
                unchanged += 1
                continue
            order = list(a) + [f for f in b if f not in a]  # the comparison's own order
            changed.append({"key": list(key), "fields": {f: [a.get(f), b.get(f)]
                                                         for f in order if a.get(f) != b.get(f)}})
        unit["counts"] = {"added": len(added), "removed": len(removed), "changed": len(changed),
                          "unchanged": unchanged}
        unit["added"] = [list(x) for x in added[:cap]]
        unit["removed"] = [list(x) for x in removed[:cap]]
        unit["changed"] = changed[:cap]
        units[k] = unit
    return {"build_a": rec_a.get("build_id"), "build_b": rec_b.get("build_id"), "limit": limit,
            "units": units}


def _short(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(_short(x) for x in v) + ")"
    s = "NULL" if v is None else str(v)
    return s[:12] + "…" if len(s) == 64 else s  # digests


def _print(d: dict[str, Any]) -> None:
    echo = typer.echo
    echo(f"diff {d['build_a']} -> {d['build_b']}")
    for k, u in d["units"].items():
        if u["status"] == "unreadable":
            echo(f"{k}: unreadable ({u['reason']})")
            continue
        c = u["counts"]
        tag = "" if u["status"] == "compared" else f" [{u['status']}]"
        files = u["file_a"] if u["file_a"] == u["file_b"] else f"{u['file_a']} -> {u['file_b']}"
        echo(f"{k}{tag}: +{c['added']} -{c['removed']} ~{c['changed']} ={c['unchanged']}  ({files})")
        for key in u["added"]:
            echo(f"  + {_key_text(tuple(key))}")
        for key in u["removed"]:
            echo(f"  - {_key_text(tuple(key))}")
        for ch in u["changed"]:
            what = "; ".join(f"{f} {_short(a)} -> {_short(b)}" for f, (a, b) in ch["fields"].items())
            echo(f"  ~ {_key_text(tuple(ch['key']))}  {what}")
        shown = len(u["added"]) + len(u["removed"]) + len(u["changed"])
        if shown < c["added"] + c["removed"] + c["changed"]:
            echo(f"  … {c['added'] + c['removed'] + c['changed'] - shown} more (--limit)")


def diff(
    ctx: typer.Context,
    build_a: Annotated[str, typer.Argument(
        help="Build id, build-record path, or `latest` (the newest ok or incomplete build of --plan's plan).")],
    build_b: Annotated[str, typer.Argument(
        help="Build id, build-record path, or `latest` (the newest ok or incomplete build of --plan's plan).")],
    family: Annotated[list[str] | None, typer.Option(
        "--family", help="Only this files key or family (repeatable; `sources` = every sources chain).")] = None,
    limit: Annotated[int, typer.Option(
        "--limit", help="Keys listed per unit and category (0 = all); counts are always complete.",
        min=0)] = DEFAULT_LIMIT,
    as_json: Annotated[bool, typer.Option("--json", help="Print the diff as JSON.")] = False,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Per family of two builds' files: rows added, removed, changed (status/digests) and unchanged."""
    g = resolve_globals(ctx, store, plan_path)
    if not g.store.exists():
        fail(f"no store at {g.store}")
    plan = load_plan_or_fail(g.plan).plan if "latest" in (build_a, build_b) else None
    try:
        rec_a, rec_b = (resolve_build(g.store, ref, plan) for ref in (build_a, build_b))
    except FoldError as exc:
        fail(str(exc))
    d = diff_builds(g.store, rec_a, rec_b, families=family, limit=limit)
    if as_json:
        echo_json(d)
    else:
        _print(d)


__all__ = ["comparison", "diff", "diff_builds"]
assert set(_COLUMNS) >= {s.kind for s in FAMILIES.values()}, "a family kind without a comparison"
