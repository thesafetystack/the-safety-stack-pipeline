"""``stack show <doc_id|source_id> [--series S] [--record] [--text] [--json]`` (D541-D543): one
document as the series' current heads hold it. Read-only: nothing is written, created or uploaded.

**What it reads.** The head version of ``data/documents/<series>/`` (:func:`stack.store.chain.resolve`,
so on an S3 mirror the version's files are downloaded first: pull on read), and of every
``data/sources.<collector>/<series>/`` for the members' source rows (``external_id``, ``matched_by``,
``observed_at``, ``record_digest``). Sizes are the ones the rows record (``BLOB.bytes``, D690);
``--text`` and ``--record`` download what they print (:mod:`stack.store.blobs`). ``--series`` defaults to the store's only ``documents`` series; with several,
it is required.

**Which document.** The argument is a ``doc_id`` when the view has that key (a retired or masked row
too); else a ``source_id``: every row whose ``identity.member_ids`` holds it (a duckdb scan of the
version's files for candidates, then the view's rows by key; a stale member in an older file does
not match). Neither: a source row of that id in the series' ``sources`` heads is printed on its own
(a source no document holds yet), else ``error:``, exit 1.

**What it prints**, per document: state and stamps; identity (anchor, keys, channels, mask, merge);
the merged metadata (title, abstract, authors, published_at, doc_type, withdrawn, sensitivity,
which member gave each field); the members (``source_id``, channel, ``external_id``, edge, live,
``matched_by`` query ids) and, under each, its in-band entries by role (the raw and text BLOBs: media type,
size, sha256);
the fetch roles (lane, status, attempts, error class and message as the reason, retry_after,
snapshot) and the snapshots (kind, role, via, the raw BLOB: media type, size, sha256; text status, quality bucket);
the current text pointer; every annotation present (each registry entry through
:func:`stack.annotations.annotation`, typed or ``other[]``; unregistered ``other[]`` entries listed
by name): status and a one-line summary of the answer (decision and questions, verdict and
confidence, buckets, items with their org slugs). An annotation the documents row does not hold is
read from the series' ``data/<wf>/<series>/`` head, where ``annotate.<wf>`` writes its selected rows
(screen, its records, affiliation, …), and is marked with that directory (D581). The triage answer
carries its **basis** per question (D527: ``title`` or ``fields``, from the ``triage_evidence``
row the question names; ``?`` when that row is not in the store).

``--record`` and ``--text`` print only that content, the blob's bytes as stored, nothing else, so
they pipe (D545); they exclude each other and ``--json``. ``--record``: the provider's record of the
queried source (a ``doc_id`` query: its anchor's): its newest in-band entry whose role is
:data:`PROVIDER_RECORD_ROLE`, on the document row or the member's sources row (D546; a file written
before that role reads upcast to that entry, D524); no role is special-cased in the report's listing. ``--text``: the
current text (the blob ``current_text_sha256`` names) of the one document the id names; an id that
several documents hold is refused (pass a ``doc_id``). ``--json``: the report as data (``{query,
series, dir, head, matched, documents: [...], sources: [...]}``; full digests, the payloads; each
annotation's ``from``, triage's ``basis`` as ``{question: basis}``).

**``stack show runs [<build>] [--series S] [--counts P,…] [--json]``** (D543): one build's run
records as a table, from the store index alone (no data file or blob is read; a mirror's index is
refreshed first, as ``stack status`` does). A build's runs are the records under its ``builds/``
directory, plus any record its ``runs`` list or whose ``build_id`` names it. The build is
``<build>`` (a build id or its directory name), else the newest build record (by ``started_at``, any status) of ``--series``, else the newest
of the store. Per run, in start order: stage, run id, state (``failed[<class>]``; a running record
with its liveness), ``scope_complete``, reasons (the error class of a failed run; for an ``ok`` run
with ``scope_complete = false`` :func:`stack.build.incomplete_reasons` of its counts), wall time
(``finished_at`` or, for a running record, its marker's or heartbeat's stamp, minus ``started_at``),
rows (``version.rows_touched``, else the outputs' ``row_count``), rows per minute, settled spend and
its split per model (``spend.usd_micro``, keys ``openrouter/<model>``, ``x``, ``openalex``) and the
open reservations; then the counts whose key starts with one of ``--counts``'s prefixes
(:data:`COUNT_PREFIXES` by default, ``all`` for every count). Totals: runs by state, the runs' wall
time, the build's, rows, and spend per model. ``--json`` carries every count unless ``--counts`` is
given.

Errors a reader expects (no store, no documents series, several and no ``--series``, a chain that
does not resolve, an unknown id, a missing blob for ``--text``, an unknown build) are ``error:``,
exit 1. No output names the S3 bucket (D540).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import duckdb
import typer

from stack import duck
from stack.annotations import annotation
from stack.cli.common import StoreOpt, echo_json, fail, resolve_globals, shown
from stack.cli.status import _started, _state, human, usd
from stack.schemas.families.wf import payload_columns
from stack.schemas.payloads.registry import REGISTRY, payload
from stack.store.blobs import BlobCorrupt, BlobNotFound, BlobStore, open_blobs
from stack.store.chain import Chain, ChainError, EmptyChainError, read_view, resolve
from stack.store.index import (
    BuildEntry,
    RecordIndexError,
    RunEntry,
    StoreIndex,
    store_index,
)
from stack.store.layout import series_dir
from stack.store.remote import open_store

#: The in-band role that holds a source's own record of an item (a JSON or Atom blob; not content).
PROVIDER_RECORD_ROLE = "provider_record"
#: Characters of the abstract and of an annotation payload shown in the text report.
ABSTRACT_CHARS = 300
PAYLOAD_CHARS = 160
#: Items of an item-grain answer listed in the text report.
ITEMS_SHOWN = 6
DOCUMENTS = "documents"
#: The triage evidence family: its rows' ``basis`` (D527) is what a question was asked on.
TRIAGE_EVIDENCE = "triage_evidence"
#: ``show runs``: the counts shown per run by default, by key prefix (the e2e kit's rate table's set;
#: D584: the arXiv lane's requests, waits, give-ups and the roles it answered without a request beside
#: ``lane_calls``, and the halts, so "called" and "requested" can be told apart).
COUNT_PREFIXES: tuple[str, ...] = (
    "asked", "answered", "rows", "lane_calls", "convert", "stopped", "not_attempted", "failed", "tier2", "cache",
    "unreached", "scope", "requests", "http", "429", "retries", "halted", "arxiv.requests", "arxiv.html.requests",
    "arxiv.atom", "arxiv.limited", "arxiv.downloads", "arxiv.html.downloads")
RUNS = "runs"


# ---------------------------------------------------------------- store reading
def documents_series(idx: StoreIndex) -> list[str]:
    """The series that have a published ``documents`` file, sorted."""
    return [d.rsplit("/", 1)[1] for d in idx.series_dirs(DOCUMENTS)]


def pick_series(idx: StoreIndex, series: str | None) -> str:
    """``series``, else the store's only ``documents`` series; :func:`fail` when there is none or
    several."""
    if series:
        return series
    have = documents_series(idx)
    if len(have) == 1:
        return have[0]
    if not have:
        fail("no data/documents/<series>/ in this store")
    fail(f"several documents series ({', '.join(have)}): pass --series")


def _resolve(root: Path, key: str) -> Chain:
    try:
        return resolve(root, key + "/", verify_ids=False)
    except EmptyChainError:
        fail(f"{key}/: no published file")
    except ChainError as exc:
        fail(f"{key}/: {type(exc).__name__}: {exc}")


def _rows(chain: Chain, keys: Sequence[Any]) -> list[dict[str, Any]]:
    if not keys:
        return []
    return read_view(chain, keys=list(keys)).to_pylist()


def docs_with_member(chain: Chain, source_id: str) -> list[dict[str, Any]]:
    """The view's rows whose ``identity.member_ids`` holds ``source_id``: candidates by a duckdb
    scan of the version's files (any file's row; a read the scan cannot do falls back to the
    view's ``identity`` column), then the view's current rows, kept only when they still hold it."""
    paths = [str(p) for p in chain.paths]
    try:
        con = duck.connect()
        try:
            hits = con.execute(
                "SELECT DISTINCT doc_id FROM read_parquet($paths, union_by_name := true) "
                "WHERE list_contains(identity.member_ids, $sid)", {"paths": paths, "sid": source_id}).fetchall()
        finally:
            con.close()
        cands = sorted(h[0] for h in hits)
    except duckdb.Error:
        ident = read_view(chain, columns=["identity"])
        cands = [r["doc_id"] for r in ident.to_pylist() if source_id in (r["identity"]["member_ids"] or [])]
    return [r for r in _rows(chain, cands) if source_id in (r["identity"]["member_ids"] or [])]


def _wf_keys(chain: Chain, doc_ids: Sequence[str]) -> list[tuple[str, str]]:
    """The ``(doc_id, item_key)`` keys of ``doc_ids`` in a ``<wf>`` chain's files (a duckdb scan, as
    :func:`docs_with_member`; a read the scan cannot do falls back to the view's key columns)."""
    paths = [str(p) for p in chain.paths]
    try:
        con = duck.connect()
        try:
            hits = con.execute(
                "SELECT DISTINCT doc_id, item_key FROM read_parquet($paths, union_by_name := true) "
                "WHERE list_contains($ids, doc_id)", {"paths": paths, "ids": list(doc_ids)}).fetchall()
        finally:
            con.close()
        return sorted((h[0], h[1]) for h in hits)
    except duckdb.Error:
        keys = read_view(chain, columns=["doc_id", "item_key"]).to_pylist()
        return sorted((k["doc_id"], k["item_key"]) for k in keys if k["doc_id"] in set(doc_ids))


def annotation_reads(root: Path, idx: StoreIndex, series: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the documents rows do not hold of their annotations (D581): per registered workflow or
    record with a ``data/<wf>/<series>/`` head, its selected rows of ``rows``' documents
    (``families``: name -> ``(dir, {doc_id: [rows]})``); and the ``triage_evidence`` rows named by
    the rows' triage questions (``evidence``: ``evidence_id`` -> row). A directory that does not
    resolve is skipped, as :func:`source_rows` does."""
    doc_ids = sorted({r["doc_id"] for r in rows})
    have = set(idx.series_dirs())
    out: dict[str, Any] = {"families": {}, "evidence": {}}
    if not doc_ids:
        return out
    for name in REGISTRY:
        d = series_dir(name, series)
        if d not in have:
            continue
        try:
            ch = resolve(root, d + "/", verify_ids=False)
        except ChainError:
            continue
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for r in _rows(ch, _wf_keys(ch, doc_ids)):
            by_doc.setdefault(r["doc_id"], []).append(r)
        out["families"][name] = (d, by_doc)
    eids = sorted({q["evidence_id"] for r in rows for q in (((r.get("annotations") or {}).get("triage") or {})
                                                             .get("payload") or {}).get("questions") or []
                   if q.get("evidence_id")})
    d = series_dir(TRIAGE_EVIDENCE, series)
    if eids and d in have:
        try:
            ch = resolve(root, d + "/", verify_ids=False)
        except ChainError:
            return out
        out["evidence"] = {r["evidence_id"]: r for r in _rows(ch, eids)}
    return out


def sources_dirs(idx: StoreIndex, series: str) -> list[str]:
    """``data/sources.<collector>/<series>`` directories with a published file."""
    return [d for d in idx.series_dirs() if d.split("/")[1].startswith("sources.") and d.endswith(f"/{series}")]


def source_rows(root: Path, dirs: Iterable[str], source_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Per source id, its row in the head views of ``dirs`` (``_dir`` added: where it was read). A
    directory that does not resolve is skipped (``stack ls`` shows why)."""
    out: dict[str, dict[str, Any]] = {}
    if not source_ids:
        return out
    for d in dirs:
        try:
            ch = resolve(root, d + "/", verify_ids=False)
        except ChainError:
            continue
        for r in _rows(ch, source_ids):
            out[r["source_id"]] = {**r, "_dir": d}
    return out


def read_blob(blobs: BlobStore, digest: str) -> bytes:
    """The blob's bytes, downloaded first on a mirror; :func:`fail` when no side holds it."""
    try:
        return blobs.get(digest)
    except (BlobNotFound, ValueError):
        fail(f"blob {digest[:12]}…: not in this store")
    except BlobCorrupt as exc:
        fail(str(exc))


# ---------------------------------------------------------------- the report as data
def _member(m: Mapping[str, Any], src: Mapping[str, Any] | None) -> dict[str, Any]:
    ident = (src or {}).get("identity") or {}
    md = (src or {}).get("metadata") or {}
    return {
        "source_id": m["source_id"], "channel": m["channel"], "external_id": ident.get("external_id"),
        "key": f"{m['key_kind']}:{m['key_value']}", "canonical_url": m.get("canonical_url"),
        "edge_kind": m["edge_kind"], "edge_to_source_id": m.get("edge_to_source_id"), "is_live": m["is_live"],
        "withdrawn_reason": m.get("withdrawn_reason"), "observation_count": m.get("observation_count"),
        "last_observed_at": m.get("last_observed_at"),
        "matched_by": [x["query_id"] for x in (md.get("matched_by") or [])],
        "sources_dir": (src or {}).get("_dir"), "source_state": (src or {}).get("state"),
        "record_digest": (src or {}).get("record_digest"),
        "provider_records": [{"sha256": (e["raw"] or e["text"])["sha256"],
                              "media_type": (e["raw"] or e["text"])["media_type"], "captured_at": e["captured_at"]}
                             for e in ((src or {}).get("inband") or {}).get("entries") or []
                             if e["content_role"] == PROVIDER_RECORD_ROLE],
    }


def _inband(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = [{"source_id": e["source_id"], "content_role": e["content_role"], "raw": e["raw"], "text": e["text"],
            "captured_at": e.get("captured_at")} for e in entries]
    return sorted(out, key=lambda x: (x["source_id"], x["content_role"]))


_ANN_KEYS = ("status", "error_class", "version", "decided_at", "origin", "config_digest", "is_basis_current")


def _wf_record(name: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One document's selected ``<wf>`` rows as an annotation record (``{ANN fields…, payload}``): the
    flat payload columns back into the payload (item grain: one item per row with an ``item_key``; the
    ``item_key = ''`` row carries the status of a document with no items); a payload with no value is
    ``None``."""
    spec = payload(name)
    cols = [f.name for f in payload_columns(spec)]
    first = min(rows, key=lambda r: r["item_key"])
    if spec.grain == "item":
        (items_field,) = list(spec.payload_schema)
        items = [{c: r.get(c) for c in cols} for r in sorted(rows, key=lambda r: r["item_key"]) if r["item_key"]]
        pl: dict[str, Any] | None = {items_field.name: items} if items or first.get("status") in ("ok", "empty") \
            else None
    else:
        pl = {c: first.get(c) for c in cols}
        pl = pl if any(v is not None for v in pl.values()) else None
    return {k: first.get(k) for k in _ANN_KEYS} | {"payload": pl}


def triage_basis(rec: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, str | None]:
    """Per triage question with an evidence row: what it was asked on (D527: ``title`` or ``fields``; a row
    written before D527 has none and was ``fields``); ``None`` when that evidence row is not in the store."""
    out: dict[str, str | None] = {}
    for q in (rec.get("payload") or {}).get("questions") or []:
        if q.get("evidence_id"):
            ev = evidence.get(q["evidence_id"])
            out[q["question"]] = None if ev is None else (ev.get("basis") or "fields")
    return out


def _annotations(row: Mapping[str, Any], reads: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The row's annotations in registry order: its own (``from: documents``), else a ``data/<wf>/``
    head's selected rows (``from`` that directory; :func:`annotation_reads`); triage with its ``basis``."""
    reads = reads or {"families": {}, "evidence": {}}
    out = []
    for name in REGISTRY:
        rec, src = annotation(row, name), DOCUMENTS
        if rec is None and name in reads["families"]:
            d, by_doc = reads["families"][name]
            if row["doc_id"] in by_doc:
                rec, src = _wf_record(name, by_doc[row["doc_id"]]), d
        if rec is not None:
            entry = {"workflow": name, **{k: rec.get(k) for k in _ANN_KEYS}, "from": src,
                     "summary": summary(rec.get("payload")), "payload": rec.get("payload")}
            if name == "triage":
                entry["basis"] = triage_basis(rec, reads["evidence"])
            out.append(entry)
    for o in (row.get("annotations") or {}).get("other") or []:
        if o.get("workflow") not in REGISTRY:
            out.append({"workflow": o.get("workflow"), "status": o.get("status"), "error_class": o.get("error_class"),
                        "version": o.get("version"), "decided_at": o.get("decided_at"), "origin": o.get("origin"),
                        "config_digest": o.get("config_digest"), "is_basis_current": o.get("is_basis_current"),
                        "from": DOCUMENTS,
                        "summary": f"unregistered; item {(o.get('payload') or {}).get('item_key')!r}",
                        "payload": o.get("payload")})
    return out


def summary(payload: Any) -> str:
    """One line of an answer, by the payload's fields (no workflow is special-cased): a
    ``decision`` with its questions' verdicts, a ``verdict`` with its confidence, ``items`` (an
    item-grain answer: names and org slugs when present), else compact JSON, cut."""
    if not isinstance(payload, Mapping):
        return "-" if payload is None else str(payload)[:PAYLOAD_CHARS]
    parts: list[str] = []
    if payload.get("decision") is not None:
        parts.append(f"decision {payload['decision']}")
        if payload.get("wanted_by"):
            parts.append("wanted_by " + ",".join(payload["wanted_by"]))
        for q in payload.get("questions") or []:
            conf = f" ({q['confidence']:.2f})" if q.get("confidence") is not None else ""
            parts.append(f"{q['question']}={q.get('verdict') or q.get('status')}{conf}")
    elif payload.get("verdict") is not None:
        conf = f" ({payload['confidence']:.2f})" if payload.get("confidence") is not None else ""
        parts.append(f"verdict {payload['verdict']}{conf}")
        if payload.get("buckets"):
            parts.append("buckets " + ",".join(payload["buckets"]))
    elif isinstance(payload.get("items"), list):
        items = payload["items"]
        shown_items = []
        for it in items[:ITEMS_SHOWN]:
            name = it.get("author_name") or it.get("item_key")
            orgs = sorted({s for a in it.get("affiliations") or [] for s in a.get("org_slugs") or []})
            shown_items.append(f"{name} -> {','.join(orgs) or '-'}" if "affiliations" in it else str(name))
        more = f", +{len(items) - ITEMS_SHOWN} more" if len(items) > ITEMS_SHOWN else ""
        parts.append(f"{len(items)} item(s): " + "; ".join(shown_items) + more)
    if parts:
        return "; ".join(parts)
    text = json.dumps(payload, default=str, sort_keys=True)
    return text if len(text) <= PAYLOAD_CHARS else text[:PAYLOAD_CHARS] + "…"


def provider_records(rep: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The provider records of one document or source report: its ``provider_record`` in-band
    entries, then those of each member's sources row (a document gathered before the role, D546),
    deduplicated by sha256."""
    out, seen = [], set()
    for e in rep["inband"]:
        b = e["raw"] or e["text"]
        if e["content_role"] == PROVIDER_RECORD_ROLE and b and b["sha256"] not in seen:
            seen.add(b["sha256"])
            out.append({"source_id": e["source_id"], "from": f"inband {PROVIDER_RECORD_ROLE}", "sha256": b["sha256"],
                        "media_type": b["media_type"], "captured_at": e["captured_at"]})
    for m in rep.get("members", []):
        for r in m["provider_records"]:
            if r["sha256"] not in seen:
                seen.add(r["sha256"])
                out.append({"source_id": m["source_id"], "from": f"sources inband {PROVIDER_RECORD_ROLE}"} | r)
    return out


def pick_record(rep: Mapping[str, Any]) -> dict[str, Any]:
    """The one record ``--record`` prints (module docstring): of the queried source (a ``doc_id``
    query: its anchor), the newest ``provider_record`` entry; :func:`fail` when it has none."""
    src = rep["query"]
    if rep["matched"] == "doc_id":
        src = rep["documents"][0]["identity"]["anchor_source_id"] or src
    recs = [r for d in [*rep["documents"], *rep["sources"]] for r in provider_records(d) if r["source_id"] == src]
    if not recs:
        fail(f"{src}: no provider record (no {PROVIDER_RECORD_ROLE} entry)")
    return max(recs, key=lambda r: r["captured_at"])


def doc_report(row: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]],
               reads: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One document's report (module docstring) as data; ``reads``: :func:`annotation_reads`."""
    ident, md, content = row["identity"], row["metadata"], row.get("content") or {}
    current = {k.removeprefix("current_"): content.get(k) for k in (
        "current_snapshot_id", "current_source_id", "current_role", "current_text_sha256", "current_text_status",
        "current_text_media", "current_text_quality", "current_revision")} if content else None
    if current is not None:
        cur = next((s for s in content.get("snapshots") or [] if s["snapshot_id"] == current["snapshot_id"]), None)
        current["size"] = ((cur or {}).get("text") or {}).get("bytes") if current["text_sha256"] else None
    snaps = []
    for s in content.get("snapshots") or []:
        q = s.get("quality") or {}
        snaps.append({k: s.get(k) for k in (
            "snapshot_id", "source_id", "kind", "content_role", "via", "url", "http_status",
            "captured_at", "raw", "text", "text_status", "text_error_class")}
            | {"quality": q.get("bucket"), "quality_flags": q.get("flags") or []})
    roles = [{k: r.get(k) for k in (
        "source_id", "content_role", "lane", "status", "attempts", "error_class", "error", "last_outcome",
        "last_attempt_at", "retry_after", "snapshot_id", "tried_urls")} for r in content.get("roles") or []]
    return {
        "doc_id": row["doc_id"], "state": row["state"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "run_id": row["run_id"],
        "identity": {k: ident.get(k) for k in (
            "anchor_source_id", "channels", "former_doc_ids", "merged_into", "is_masked", "mask_reason",
            "override_ids", "members_digest")} | {"keys": list(dict.fromkeys(f"{k['kind']}:{k['value']}" for k in ident["keys"] or []))},
        "metadata": {k: md.get(k) for k in (
            "title", "abstract", "published_at", "provider_updated_at", "doc_type", "is_withdrawn", "sensitivity",
            "fields_digest")} | {
            "authors": [a["name"] for a in md.get("authors") or []],
            "field_sources": [{"field": f["field"], "source_id": f["source_id"], "origin": f["origin"]}
                              for f in md.get("field_sources") or []]},
        "members": [_member(m, sources.get(m["source_id"])) for m in ident["members"] or []],
        "inband": _inband((row.get("inband") or {}).get("entries") or []),
        "roles": roles, "snapshots": snaps, "current": current,
        "same_raw_doc_ids": content.get("same_raw_doc_ids") or [] if content else [],
        "annotations": _annotations(row, reads),
    }


def source_report(src: Mapping[str, Any]) -> dict[str, Any]:
    """A source row no document holds (module docstring), as data."""
    ident, md = src.get("identity") or {}, src.get("metadata") or {}
    return {"source_id": src["source_id"], "state": src.get("state"), "status": src.get("status"),
            "observed_at": src.get("observed_at"), "run_id": src.get("run_id"), "sources_dir": src.get("_dir"),
            "channel": ident.get("channel"), "external_id": ident.get("external_id"),
            "canonical_url": ident.get("canonical_url"), "title": md.get("title"),
            "matched_by": [x["query_id"] for x in (md.get("matched_by") or [])],
            "record_digest": src.get("record_digest"),
            "inband": _inband((src.get("inband") or {}).get("entries") or [])}


def show_report(root: Path, ident: str, series: str | None) -> dict[str, Any]:
    """The whole report for ``ident`` (module docstring); :func:`fail` on what a reader expects."""
    idx = store_index(root)
    s = pick_series(idx, series)
    key = series_dir(DOCUMENTS, s)
    chain = _resolve(root, key)
    rows = _rows(chain, [ident])
    matched = "doc_id"
    if not rows:
        rows, matched = docs_with_member(chain, ident), "member"
    sdirs = sources_dirs(idx, s)
    members = sorted({m["source_id"] for r in rows for m in r["identity"]["members"] or []})
    srcs = source_rows(root, sdirs, members or [ident])
    reads = annotation_reads(root, idx, s, rows)
    out: dict[str, Any] = {"query": ident, "series": s, "dir": key, "head": chain.head_name, "matched": matched,
                           "documents": [doc_report(r, srcs, reads) for r in rows], "sources": []}
    if not rows:
        if ident not in srcs:
            fail(f"{ident}: no document (doc_id or member) in {key}/ and no source row in its sources series")
        out["matched"] = "source"
        out["sources"] = [source_report(srcs[ident])]
    return out


# ---------------------------------------------------------------- show runs
def pick_build(idx: StoreIndex, ref: str | None, series: str | None) -> BuildEntry:
    """The build ``ref`` names (id or directory name), else the newest of ``series``, else the
    newest of the store (by ``started_at``, any status); :func:`fail` when there is none."""
    if ref is not None:
        try:
            b = idx.build(ref)
        except RecordIndexError as exc:
            fail(str(exc))
        if b is None:
            fail(f"no build {ref!r} in this store (a build id or a builds/ directory name)")
        return b
    hits = [b for b in idx.builds if series is None or b.record.get("series") == series]
    if not hits:
        fail(f"no build record of series {series!r}" if series else "no build record in this store")
    return max(hits, key=lambda b: (_started(b.record.get("started_at"), b.name), b.name))


def _minutes(seconds: float | None) -> float | None:
    return None if seconds is None else seconds / 60


def run_row(e: RunEntry, prefixes: Sequence[str] | None, now: dt.datetime) -> dict[str, Any]:
    """One run of the table (module docstring) as data; ``prefixes`` ``None`` = every count."""
    from stack.build import incomplete_reasons

    r = e.record
    end = r.finished_at or (e.marker_at if r.status == "running" else None) or r.heartbeat_at
    wall = (end - r.started_at).total_seconds() if end is not None else None
    rows = (r.version or {}).get("rows_touched")
    if rows is None and r.outputs:
        rows = sum(int(o.get("row_count") or 0) for o in r.outputs.values())
    if r.status in ("failed", "cancelled"):
        reasons = [r.error_class or "unspecified"]
    elif r.status == "ok" and not r.scope_complete:
        reasons = incomplete_reasons(r.counts)
    else:
        reasons = []
    spend = r.spend or {}
    models = {str(k): int(v) for k, v in sorted((spend.get("usd_micro") or {}).items())}
    counts = {k: v for k, v in sorted(r.counts.items()) if prefixes is None or k.startswith(tuple(prefixes))}
    return {"stage": r.stage, "run_id": r.run_id, "status": r.status, "error_class": r.error_class,
            "state": _state(r.status, r.error_class, e.liveness, e.marker_at, now),
            "scope_complete": r.scope_complete, "reasons": reasons, "started_at": r.started_at,
            "finished_at": r.finished_at, "wall_s": wall, "rows": rows,
            "rows_per_min": rows / (wall / 60) if rows and wall else None,
            "spend_usd_micro": sum(models.values()), "spend_by_model": models,
            "open_usd_micro": int(spend.get("reserved_open_usd_micro") or 0), "counts": counts,
            "error": (r.error or "").strip().splitlines()[-1][:200] if r.error else None}


def runs_report(root: Path, ref: str | None, series: str | None, prefixes: Sequence[str] | None,
                now: dt.datetime | None = None) -> dict[str, Any]:
    """``show runs`` (module docstring) as data."""
    now = now or dt.datetime.now(dt.UTC)
    try:
        idx = store_index(root, refresh=open_store(root).remote is not None)
    except RecordIndexError as exc:
        fail(str(exc))
    b = pick_build(idx, ref, series)
    listed = set(b.record.get("runs") or [])
    entries = sorted((e for e in idx.runs if e.build == b.name or e.run_id in listed
                      or (e.record.build_id is not None and e.record.build_id == b.build_id)),
                     key=lambda e: (e.record.started_at, e.run_id))
    runs = [run_row(e, prefixes, now) for e in entries]
    rec = b.record
    started = _started(rec.get("started_at"), b.name)
    fin = rec.get("finished_at")
    finished = dt.datetime.fromisoformat(fin) if isinstance(fin, str) and fin else None
    by_model: dict[str, int] = {}
    for r in runs:
        for k, v in r["spend_by_model"].items():
            by_model[k] = by_model.get(k, 0) + v
    states: dict[str, int] = {}
    for r in runs:
        key = r["status"] + (f"[{r['error_class']}]" if r["error_class"] else "")
        states[key] = states.get(key, 0) + 1
    walls = [r["wall_s"] for r in runs if r["wall_s"] is not None]
    rows = [r["rows"] for r in runs if r["rows"] is not None]
    return {
        "build": {"build_id": b.build_id, "name": b.name, "plan": rec.get("plan"), "series": rec.get("series"),
                  "status": b.status, "error_class": rec.get("error_class"), "liveness": b.liveness,
                  "started_at": started.isoformat(), "finished_at": fin,
                  "wall_s": (finished - started).total_seconds() if finished is not None else None,
                  "incomplete": rec.get("incomplete") or [], "comment": rec.get("comment")},
        "runs": runs,
        "totals": {"runs": len(runs), "by_state": states, "runs_wall_s": sum(walls), "rows": sum(rows),
                   "spend_usd_micro": sum(by_model.values()), "spend_by_model": dict(sorted(by_model.items())),
                   "open_usd_micro": sum(r["open_usd_micro"] for r in runs)},
    }


def _m(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds / 60:.1f}m"


def print_runs(rep: Mapping[str, Any]) -> None:
    echo = typer.echo
    b, t = rep["build"], rep["totals"]
    st = b["status"] + (f"[{b['error_class']}]" if b["error_class"] else "")
    echo(f"build {b['build_id']}  plan {b['plan']}  series {b['series'] or '-'}  {st}  started {b['started_at'][:19]}  "
         f"finished {(b['finished_at'] or '-')[:19]}  wall {_m(b['wall_s'])}")
    for e in b["incomplete"]:
        echo(f"  incomplete: {e.get('stage')} run {e.get('run_id')} ({', '.join(e.get('reasons') or [])})")
    echo(f"  {'stage':<26} {'run':<14} {'state':<26} {'compl':<5} {'wall':>7} {'rows':>8} {'rows/min':>9} "
         f"{'spend':>9}  reasons")
    for r in rep["runs"]:
        rpm = "-" if r["rows_per_min"] is None else f"{r['rows_per_min']:.1f}"
        spend = usd(r["spend_usd_micro"]) + (f" +{usd(r['open_usd_micro'])} open" if r["open_usd_micro"] else "")
        echo(f"  {r['stage']:<26} {r['run_id']:<14} {r['state']:<26} {'yes' if r['scope_complete'] else 'no':<5} "
             f"{_m(r['wall_s']):>7} {'-' if r['rows'] is None else r['rows']:>8} {rpm:>9} {spend:>9}  "
             f"{', '.join(r['reasons']) or '-'}")
        if r["error"]:
            echo(f"      error: {shown(r['error'])}")
        if r["counts"]:
            echo("      counts: " + ", ".join(f"{k}={v}" for k, v in r["counts"].items()))
        if any(r["spend_by_model"].values()):
            echo("      spend: " + ", ".join(f"{k} {usd(v)}" for k, v in r["spend_by_model"].items()))
    states = ", ".join(f"{n} {k}" for k, n in sorted(t["by_state"].items()))
    opened = f" (+{usd(t['open_usd_micro'])} open)" if t["open_usd_micro"] else ""
    echo(f"totals: {t['runs']} run(s) ({states or 'none'}); runs' wall {_m(t['runs_wall_s'])}; build wall "
         f"{_m(b['wall_s'])}; rows {t['rows']}; spend {usd(t['spend_usd_micro'])}{opened}")
    for k, v in t["spend_by_model"].items():
        echo(f"  {k:<40} {usd(v):>10}")


# ---------------------------------------------------------------- text
def _dg(d: str | None) -> str:
    return d[:12] if d else "-"


def _size(n: int | None) -> str:
    return "missing" if n is None else human(n)


def _ts(v: Any) -> str:
    return "-" if v is None else str(v)[:19]


def _print_inband(echo: Any, entries: Sequence[Mapping[str, Any]], indent: str) -> None:
    for e in entries:
        parts = [f"{part} {b['media_type']} {_size(b['bytes'])} {_dg(b['sha256'])}"
                 for part, b in (("raw", e["raw"]), ("text", e["text"])) if b]
        echo(f"{indent}{e['content_role']:<16} {'  '.join(parts)}")


def print_doc(d: Mapping[str, Any]) -> None:
    echo = typer.echo
    i, md = d["identity"], d["metadata"]
    echo(f"document {d['doc_id']}  state {d['state']}  created {_ts(d['created_at'])}  updated {_ts(d['updated_at'])}")
    echo(f"  anchor {i['anchor_source_id']}  channels {','.join(i['channels'] or []) or '-'}  "
         f"keys {', '.join(i['keys']) or '-'}")
    if i["is_masked"]:
        echo(f"  MASKED: {i['mask_reason']}  overrides {','.join(i['override_ids'] or []) or '-'}")
    if i["merged_into"]:
        echo(f"  merged into {i['merged_into']}")
    if i["former_doc_ids"]:
        echo(f"  former doc ids {', '.join(i['former_doc_ids'])}")
    echo("metadata")
    echo(f"  title: {md['title'] or '-'}")
    abstract = md["abstract"] or "-"
    echo(f"  abstract: {abstract[:ABSTRACT_CHARS]}{'…' if len(abstract) > ABSTRACT_CHARS else ''}")
    echo(f"  authors: {', '.join(md['authors']) or '-'}")
    echo(f"  published {_ts(md['published_at'])}  doc_type {md['doc_type'] or '-'}  withdrawn "
         f"{'yes' if md['is_withdrawn'] else 'no'}  sensitivity {','.join(md['sensitivity'] or []) or '-'}")
    if md["field_sources"]:
        echo("  fields from: " + ", ".join(f"{f['field']}<-{f['source_id']} ({f['origin']})"
                                          for f in md["field_sources"]))
    echo(f"members ({len(d['members'])})")
    for m in d["members"]:
        edge = m["edge_kind"] + (f"->{m['edge_to_source_id']}" if m["edge_to_source_id"] else "")
        echo(f"  {m['source_id']}  {m['channel']}  external_id {m['external_id'] or '-'}  {edge}  "
             f"{'live' if m['is_live'] else 'NOT LIVE'}" + (f" ({m['withdrawn_reason']})" if m["withdrawn_reason"] else ""))
        echo(f"    matched_by {', '.join(m['matched_by']) or '-'}  "
             f"source row {(m['sources_dir'] or 'not found') + ('/' if m['sources_dir'] else '')}")
        mine = [e for e in d["inband"] if e["source_id"] == m["source_id"]]
        if mine:
            echo("    in-band:")
            _print_inband(echo, mine, "      ")
    others = [e for e in d["inband"] if e["source_id"] not in {m["source_id"] for m in d["members"]}]
    if others:
        echo("  in-band entries of no member:")
        _print_inband(echo, others, "    ")
    echo(f"fetch roles ({len(d['roles'])})")
    for r in d["roles"]:
        why = f"  {r['error_class']}" + (f": {r['error']}" if r["error"] and r["error"] != r["error_class"] else "") \
            if r["error_class"] else ""
        retry = f"  retry_after {_ts(r['retry_after'])}" if r["retry_after"] else ""
        echo(f"  {r['source_id']}  {r['content_role']:<12} lane {r['lane'] or '-':<8} {r['status']:<17} "
             f"attempts {r['attempts']}{why}{retry}  snapshot {r['snapshot_id'] or '-'}")
    echo(f"snapshots ({len(d['snapshots'])})")
    for s in d["snapshots"]:
        text = f"text {s['text_status'] or '-'}" + (f"[{s['text_error_class']}]" if s["text_error_class"] else "")
        echo(f"  {s['snapshot_id']}  {s['kind']:<6} {s['content_role']:<12} {s['via']:<8} {s['raw']['media_type']:<22} "
             f"{_size(s['raw']['bytes']):>10}  {_dg(s['raw']['sha256'])}  {text}  quality {s['quality'] or '-'}")
    c = d["current"]
    if c is None:
        echo("current text: none (not acquired)")
    else:
        echo(f"current text: {c['text_status'] or '-'}  media {c['text_media'] or '-'}  snapshot "
             f"{c['snapshot_id'] or '-'}  source {c['source_id'] or '-'}  role {c['role'] or '-'}  digest "
             f"{_dg(c['text_sha256'])}  size {_size(c['size']) if c['text_sha256'] else '-'}  "
             f"quality {c['text_quality'] or '-'}")
    if d["same_raw_doc_ids"]:
        echo(f"  same raw as {', '.join(d['same_raw_doc_ids'])}")
    echo(f"annotations ({len(d['annotations'])})")
    for a in d["annotations"]:
        st = a["status"] + (f"[{a['error_class']}]" if a["error_class"] else "")
        basis = ""
        if "basis" in a:
            got = {q: b or "?" for q, b in a["basis"].items()}
            basis = "  basis " + (next(iter(got.values())) if len(set(got.values())) == 1
                                  else ",".join(f"{q}={b}" for q, b in got.items()) or "-")
        where = f"  ({a['from']}/)" if a["from"] != DOCUMENTS else ""
        echo(f"  {a['workflow']:<16} {st:<22} {a['summary']}{basis}{where}")


def print_source(s: Mapping[str, Any]) -> None:
    echo = typer.echo
    echo(f"source {s['source_id']} (in no document)  state {s['state']}  status {s['status']}  "
         f"observed {_ts(s['observed_at'])}  in {s['sources_dir']}/")
    echo(f"  {s['channel']}  external_id {s['external_id'] or '-'}  {s['canonical_url'] or '-'}")
    echo(f"  title: {s['title'] or '-'}")
    echo(f"  matched_by {', '.join(s['matched_by']) or '-'}")
    if s["inband"]:
        echo("  in-band:")
        _print_inband(echo, s["inband"], "    ")


# ---------------------------------------------------------------- the verb
def show(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(
        help="A doc_id or a source_id (a member of a document, or a source no document holds); or `runs`.")],
    build: Annotated[str | None, typer.Argument(
        help="`show runs` only: a build id or builds/ directory name (default: the newest of --series, "
             "else of the store).")] = None,
    series: Annotated[str | None, typer.Option(
        "--series", help="The series (data/documents/<series>/; for `show runs`, the build record's series); "
                         "default: the store's only documents series (`show runs`: any).")] = None,
    record: Annotated[bool, typer.Option(
        "--record", help=f"Print only the queried source's provider record (its newest {PROVIDER_RECORD_ROLE} "
                         "in-band entry), as stored.")] = False,
    text: Annotated[bool, typer.Option("--text", help="Print only the document's current text, as stored.")] = False,
    counts: Annotated[str | None, typer.Option(
        "--counts", help="`show runs` only: count key prefixes to show, comma-separated, or `all` "
                         f"(default: {','.join(COUNT_PREFIXES)}).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
    store: StoreOpt = None,
) -> None:
    """Show one document from the series' heads (identity, metadata, members and their in-band entries,
    fetch roles and snapshots, the current text pointer, annotations), or with `show runs [BUILD]` one
    build's run records (state, reasons, wall time, rows per minute, spend per model, totals). Read-only."""
    g = resolve_globals(ctx, store, None)
    if not g.store.is_dir():
        fail(f"no store at {g.store}")
    if target == RUNS:
        if record or text:
            fail("--record and --text are for a document, not `show runs`")
        if counts == "all" or (as_json and counts is None):  # --json carries every count unless asked
            prefixes: tuple[str, ...] | None = None
        else:
            prefixes = tuple(p for p in (counts or "").split(",") if p) or COUNT_PREFIXES
        runs = runs_report(g.store, build, series, prefixes)
        if as_json:
            echo_json(runs)
        else:
            print_runs(runs)
        return
    if build is not None:
        fail(f"unexpected argument {build!r}: `stack show <doc_id|source_id>` takes one id")
    if counts is not None:
        fail("--counts is for `show runs`")
    if record and text:
        fail("--record and --text each print only their content: give one")
    if as_json and (record or text):
        fail("--record and --text print raw content, not JSON (--json alone prints the report)")
    rep = show_report(g.store, target, series)
    docs = rep["documents"]
    if record:
        typer.echo(read_blob(open_blobs(g.store), pick_record(rep)["sha256"]), nl=False)
        return
    if text:
        if len(docs) != 1:
            held = f"{len(docs)} documents hold it ({', '.join(d['doc_id'] for d in docs)}): pass one doc_id" \
                if docs else "no document holds it"
            fail(f"{target}: {held}")
        c = docs[0]["current"] or {}
        if not c.get("text_sha256"):
            fail(f"{docs[0]['doc_id']}: no current text (text status {c.get('text_status') or 'none'})")
        typer.echo(read_blob(open_blobs(g.store), c["text_sha256"]), nl=False)
        return
    if as_json:
        echo_json(rep)
        return
    typer.echo(shown(f"{rep['dir']}/ head {rep['head']}  query {rep['query']} (matched by {rep['matched']})"))
    for s in rep["sources"]:
        print_source(s)
    for n, d in enumerate(docs):
        if n:
            typer.echo("")
        print_doc(d)


__all__ = ["COUNT_PREFIXES", "PROVIDER_RECORD_ROLE", "annotation_reads", "doc_report", "docs_with_member", "pick_build", "pick_record",
           "pick_series", "provider_records", "run_row", "runs_report", "show", "show_report", "summary", "triage_basis"]
