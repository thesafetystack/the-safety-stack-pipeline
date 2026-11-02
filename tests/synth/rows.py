"""Minimal valid rows per family (D46), their tables and headers, and one failing fixture per
write-time check (S1-S10). Deterministic: ids and digests derive from small integers and labels.

Later tests build every row through these factories (``tests/synth/world.py`` builds the
scenario world on top), so a schema change touches this file only.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
from collections.abc import Callable
from typing import Any

import pyarrow as pa

from stack.checks.digests import row_fields_digest, row_members_digest
from stack.schemas.families import family
from stack.store.layout import DataFile, make_data_file
from stack.store.parquet import build_header
from workflows.acquire.convert import CONFIGS, pin_of

Row = dict[str, Any]
AT = dt.datetime(2026, 10, 12, 1, 30, tzinfo=dt.UTC)
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def mint(letter: str, n: int) -> str:
    """A deterministic id in ``stack.core.ids`` shape: ``letter`` + 12 Crockford chars of ``n``."""
    body = "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(12)))
    return letter + body


def sid(n: int) -> str:
    """Source id (= doc id of a document anchored on it)."""
    return mint("s", n)


def rid(n: int = 1) -> str:
    """Run id."""
    return mint("r", n)


def eid(n: int) -> str:
    """Evidence id (kind letter ``a``, annotation)."""
    return mint("a", n)


def blob(sha256: str, size: int, media_type: str) -> Row:
    """A ``BLOB`` reference (D690)."""
    return {"sha256": sha256, "bytes": size, "media_type": media_type}


def dg(label: object) -> str:
    """A 64-hex digest of ``label``."""
    return hashlib.sha256(str(label).encode()).hexdigest()


def _with(row: Row, over: dict[str, Any]) -> Row:
    out = copy.deepcopy(row)
    out.update(over)
    return out


# ---------------------------------------------------------------- sources, coverage
def source_row(i: int = 0, run: int = 1, **over: Any) -> Row:
    s = sid(i)
    return _with({
        "source_id": s, "state": "observed", "record_digest": dg(("record", i)),
        "record_format": "json", "observed_at": AT,
        "status": "ok", "run_id": rid(run), "error_class": None, "error": None,
        "identity": {"source_id": s, "channel": "arxiv", "external_id": f"2610.{i:05d}",
                     "canonical_url": f"https://arxiv.org/abs/2610.{i:05d}",
                     "keys": [{"kind": "arxiv", "value": f"2610.{i:05d}"}]},
        "metadata": {"title": f"Paper {i}", "abstract": f"Abstract {i}", "authors": [
            {"name": "A. Author", "orcid": None, "affiliation_raw": None}],
            "published_at": AT, "provider_updated_at": None, "doc_type": "preprint",
            "is_withdrawn": False, "sensitivity": [], "license_class": "open", "origin": "provider",
            "provider_version": 1, "lookup_of_source_ids": None,
            "miss_count": 0, "query_digest": dg("q")},
        "inband": {"entries": [], "run_id": rid(run), "updated_at": AT},
    }, over)


def coverage_row(i: int = 0, run: int = 1, **over: Any) -> Row:
    return _with({
        "channel": "arxiv", "query_digest": dg(("query", i)),
        "window_start_at": AT - dt.timedelta(days=i + 1), "window_end_at": AT - dt.timedelta(days=i),
        "query": "cat:cs.AI", "seen": 10, "kept": 3, "is_exhausted": True, "next_cursor": None,
        "gate_digest": None, "status": "ok", "run_id": rid(run), "error_class": None, "error": None,
        "attempts": 1, "first_attempt_at": AT, "last_attempt_at": AT, "retry_after": None,
    }, over)


# ---------------------------------------------------------------- documents, corpus
def _ann(workflow: str, payload: Any, *, basis_kind: str, run: int = 1) -> Row:
    return {"workflow": workflow, "version": 1, "status": "ok", "error_class": None,
            "basis_kind": basis_kind, "basis_digest": dg(("basis", workflow)),
            "config_digest": dg(("config", workflow)), "decided_at": AT, "evidence_id": eid(run),
            "origin": "rule", "is_basis_current": True, "payload": payload}


#: The convert pin the synthetic ``content`` blocks were converted at: the dev plans' config
#: (``pymupdf4llm-v1``, D39) in this environment (T3.5's pin includes the tool versions).
CONVERT_PIN = pin_of(CONFIGS["pymupdf4llm-v1"])


def _content(i: int, run: int) -> Row:
    s, r = sid(i), rid(run)
    raw = dg(("raw", i))
    return {
        "convert_pin": CONVERT_PIN,
        "snapshots": [{
            "snapshot_id": f"{r}.0", "source_id": s, "kind": "raw", "content_role": "full_text",
            "via": "live", "url": f"https://arxiv.org/pdf/{i}",
            "final_url": None, "http_status": 200, "etag": None, "last_modified": None,
            "captured_at": AT, "as_of": AT, "revision": "v1",
            "raw": blob(raw, 1234, "application/pdf"),
            "text": blob(dg(("text", i)), 4321, "text/markdown"), "text_status": "ok", "text_error_class": None,
            "convert_attempts": 1, "convert_pin": CONVERT_PIN,
        }],
        "roles": [{
            "content_role": "full_text", "source_id": s, "status": "ok", "error_class": None,
            "error": None, "attempts": 1, "first_attempt_at": AT, "last_attempt_at": AT,
            "last_outcome": "ok", "retry_after": None, "tried_urls": [f"https://arxiv.org/pdf/{i}"],
            "last_source_id": s, "etag": None, "last_modified": None, "validator_url": None,
            "snapshot_id": f"{r}.0", "lane": "arxiv", "rule": None,
        }],
        "current_snapshot_id": f"{r}.0", "current_source_id": s, "current_role": "full_text",
        "current_text_sha256": dg(("text", i)), "current_text_status": "ok",
        "current_text_media": "md", "current_text_quality": None, "current_revision": "v1",
        "revisions": [{"revision": "v1", "published_at": AT, "title": None}],
        "members_digest": dg(("members", i)), "same_raw_doc_ids": [], "run_id": r, "updated_at": AT,
    }


def html_only(content: Row) -> Row:
    """An acquired ``content`` block (:func:`_content`'s shape) as ``arxiv_pdf: on_miss | never``
    leaves an arXiv paper whose HTML is the text (D480, D485): the raw snapshot is the LaTeXML
    ``html`` (current, ``current_role = 'html'``), its role ``ok`` on lane ``arxiv``, and the PDF role
    ``skipped[not_requested]`` with nothing fetched. A new dict; ``content`` is not modified."""
    c = copy.deepcopy(content)
    (snap,), (pdf,) = c["snapshots"], c["roles"]
    snap.update(content_role="html", raw={**snap["raw"], "media_type": "text/html"},
                url=str(snap["url"]).replace("/pdf/", "/html/") + "v1")
    html = {**pdf, "content_role": "html", "tried_urls": [snap["url"]], "validator_url": snap["url"]}
    pdf.update(status="skipped", error_class="not_requested", error="arxiv_pdf: on_miss, the arXiv HTML is the text",
               rule="arxiv_pdf",
               attempts=0, first_attempt_at=None, last_attempt_at=None, last_outcome=None, tried_urls=[],
               snapshot_id=None)
    c["roles"] = [pdf, html]
    c["current_role"] = "html"
    return c


def document_row(i: int = 0, run: int = 1, *, state: str = "identified", **over: Any) -> Row:
    """A ``documents`` row as ``gather`` creates it (D7: ``inband.entries = []``, typed
    annotations NULL with ``other = []``, ``content`` NULL); ``state='triaged'`` adds the triage
    annotation, ``state='acquired'`` also a fetched-and-converted ``content`` block."""
    s, r = sid(i), rid(run)
    row: Row = {
        "doc_id": s, "state": state, "run_id": r, "created_at": AT, "updated_at": AT,
        "identity": {
            "anchor_source_id": s,
            "members": [{
                "source_id": s, "channel": "arxiv", "key_kind": "arxiv", "key_value": f"2610.{i:05d}",
                "canonical_url": None, "title": f"Paper {i}", "published_at": AT,
                "doc_type": "preprint", "license_class": "open", "is_live": True,
                "edge_kind": "anchor", "edge_to_source_id": None, "override_id": None,
                "first_observed_at": AT, "last_observed_at": AT, "observation_count": 1,
                "withdrawn_at": None, "withdrawn_reason": None,
            }],
            "member_ids": [s], "keys": [{"kind": "arxiv", "value": f"2610.{i:05d}", "source_id": s}],
            "channels": ["arxiv"], "former_doc_ids": [], "merged_into": None, "is_masked": False,
            "mask_reason": None, "override_ids": [], "members_digest": dg(("members", i)),
            "run_id": r, "updated_at": AT,
        },
        "metadata": {
            "title": f"Paper {i}", "abstract": f"Abstract {i}", "authors": [
                {"name": "A. Author", "orcid": None, "affiliation_raw": None}],
            "published_at": AT, "provider_updated_at": None, "doc_type": "preprint",
            "is_withdrawn": False, "sensitivity": [],
            "field_sources": [{"field": "title", "source_id": s, "origin": "provider"}],
            "fields_digest": dg(("fields", i)), "run_id": r, "updated_at": AT,
        },
        "inband": {"entries": [], "run_id": r, "updated_at": AT},
        "content": None,
        "annotations": {"triage": None, "screen": None, "affiliation": None, "doc_class": None,
                        "other": []},
    }
    if state in ("triaged", "acquired"):
        row["annotations"]["triage"] = _ann("triage", {
            "decision": "fetch", "wanted_by": ["screen"], "questions": [{
                "question": "safety_relevant", "status": "ok", "error_class": None,
                "verdict": "yes", "confidence": 0.9, "evidence_id": eid(i),
                "config_digest": dg("q-config")}],
        }, basis_kind="fields", run=run)
        # S9 (§2.5): a triaged row's triage basis is the row's current fields_digest.
        row["annotations"]["triage"]["basis_digest"] = row["metadata"]["fields_digest"]
    if state == "acquired":
        row["inband"]["entries"] = [{
            "source_id": s, "content_role": "abstract", "raw": None,
            "text": blob(dg(("abstract", i)), 120, "text/plain"), "captured_at": AT}]
        row["content"] = _content(i, run)
    return _with(row, over)


def with_digests(row: Row) -> Row:
    """``row`` (a ``documents`` row) with its digests recomputed as V4 does (§2.7, D19): the
    ``metadata.fields_digest``, ``identity.members_digest``, and the bases that must agree with
    them (S9): ``annotations.triage.basis_digest`` and ``content.members_digest`` when present.
    In place; returns ``row``. (``document_row``'s labelled digests satisfy S9 but not V4.)"""
    row["metadata"]["fields_digest"] = row_fields_digest(row)
    row["identity"]["members_digest"] = row_members_digest(row)
    if row["annotations"].get("triage") is not None:
        row["annotations"]["triage"]["basis_digest"] = row["metadata"]["fields_digest"]
    if row.get("content") is not None:
        row["content"]["members_digest"] = row["identity"]["members_digest"]
    return row


# ---------------------------------------------------------------- <wf>, <wf>_evidence
def screen_row(i: int = 0, run: int = 1, **over: Any) -> Row:
    return _with({
        "doc_id": sid(i), "item_key": "", "evidence_id": eid(i), "source_id": sid(i),
        "override_ids": [], "override_stale_ids": [], "result_digest": dg(("result", i)),
        "workflow": "screen", "version": 1, "status": "ok", "error_class": None,
        "basis_kind": "text", "basis_digest": dg(("text", i)), "config_digest": dg("screen-config"),
        "decided_at": AT, "origin": "computed", "is_basis_current": True,
        "verdict": "relevant", "basis_role": "full_text", "buckets": ["evals"],
        "quotes": [{"text": "we evaluate", "offset": 10}], "off_topic_reason": None,
        "confidence": 0.8, "reasoning": "about evals", "adjudication_tier": 1,
        "tier1_verdict": None, "tier1_model": None,
        "run_id": rid(run), "error": None, "attempts": 1, "first_attempt_at": AT,
        "last_attempt_at": AT, "retry_after": None,
    }, over)


def affiliation_row(i: int = 0, item_key: str = "a. author", run: int = 1, **over: Any) -> Row:
    """An ``affiliation`` ``<wf>`` row at item grain (one byline author, ``item_key``); an
    ``item_key = ''`` row carries the document's status only (its item fields NULL; T5.2)."""
    item = item_key != ""
    return _with({
        "doc_id": sid(i), "item_key": item_key, "evidence_id": eid(i), "source_id": sid(i),
        "override_ids": [], "override_stale_ids": [], "result_digest": dg(("affiliation", i, item_key)),
        "workflow": "affiliation", "version": 1, "status": "ok" if item else "empty",
        "error_class": None, "basis_kind": "text", "basis_digest": dg(("text", i)),
        "config_digest": dg("affiliation-config"), "decided_at": AT, "origin": "computed",
        "is_basis_current": True,
        "author_name": item_key.title() if item else None, "author_surface": item_key if item else None,
        "byline_index": 0 if item else None,
        "affiliations": [{"surface": "Example Lab", "org_slugs": ["example-lab"]}] if item else None,
        "orcid": None, "resolution": "resolved" if item else None,
        "run_id": rid(run), "error": None, "attempts": 1, "first_attempt_at": AT,
        "last_attempt_at": AT, "retry_after": None,
    }, over)


def _evi(i: int, workflow: str, basis_kind: str) -> Row:
    return {
        "evidence_id": eid(i), "work_digest": dg(("work", workflow, i)), "source_id": sid(i),
        "basis_kind": basis_kind, "basis_digest": dg(("basis", i)),
        "config_digest": dg((workflow, "config")), "item_key": "", "doc_id": sid(i),
        "origin": "computed", "decided_at": AT, "supersedes": None, "is_cache_hit": False,
        "template_digest": dg("template"), "model_digest": dg("model"), "prompt_name": "p-v1",
        "rendered_digest": dg(("rendered", i)), "model": "fake-model",
        "response_digest": dg(("response", i)), "cost_usd": 0.001, "tokens_in": 100,
        "tokens_out": 20, "latency_ms": 300, "result_digest": dg(("result", i)),
    }


def _t2(run: int) -> Row:
    return {"status": "ok", "run_id": rid(run), "error_class": None, "error": None, "attempts": 1,
            "first_attempt_at": AT, "last_attempt_at": AT, "retry_after": None}


def screen_evidence_row(i: int = 0, run: int = 1, **over: Any) -> Row:
    return _with({**_evi(i, "screen", "text"),
                  "verdict": "relevant", "basis_role": "full_text", "buckets": ["evals"],
                  "quotes": [{"text": "we evaluate", "offset": 10}], "off_topic_reason": None,
                  "confidence": 0.8, "reasoning": "about evals", "adjudication_tier": 1,
                  "tier1_verdict": None, "tier1_model": None, **_t2(run)}, over)


def triage_evidence_row(i: int = 0, run: int = 1, **over: Any) -> Row:
    return _with({**_evi(i, "triage", "fields"), "question": "safety_relevant", "verdict": "yes",
                  "confidence": 0.9, "reasoning": "mentions evaluations", **_t2(run)}, over)


# ---------------------------------------------------------------- tables, headers, names
ROW_FACTORIES: dict[str, Callable[..., Row]] = {
    "sources": source_row,
    "coverage": coverage_row,
    "documents": document_row,
    "corpus": document_row,
    "screen": screen_row,
    "screen_evidence": screen_evidence_row,
    "triage_evidence": triage_evidence_row,
    "affiliation": affiliation_row,
}

#: Writer/stage and header defaults per family for the synthetic files.
_HEADER_DEFAULTS: dict[str, dict[str, Any]] = {
    "sources": {"stage": "collect.arxiv", "chain_id": "sources.arxiv", "kind": "part",
                "base_file_id": f"{rid(0)}.sources"},
    "coverage": {"stage": "collect.arxiv", "chain_id": "coverage.arxiv"},
    "documents": {"stage": "gather", "chain_id": "documents",
                  "blocks_written": ["identity", "metadata", "inband"]},
    # Fold files: writer `fold`, stage `fold.<config>` (D65, D81).
    "corpus": {"stage": "fold.corpus", "writer": "fold", "fold_config": "corpus", "filter": "has_text",
               "annotations_folded": ["triage"]},
    "screen": {"stage": "annotate.screen", "basis_kind": "text"},
    # chain_id (the family directory, D372) defaults to the family.
    "screen_evidence": {"stage": "annotate.screen"},
    "triage_evidence": {"stage": "triage", "blocks_written": ["annotations"]},
    "affiliation": {"stage": "annotate.affiliation", "basis_kind": "text"},
}


def table(family_name: str, rows: list[Row]) -> pa.Table:
    """The family table of ``rows`` under the registry schema."""
    return pa.Table.from_pylist(rows, schema=family(family_name).schema)


def header(family_name: str, tbl: pa.Table, run: int = 1, **over: Any) -> dict[str, str]:
    """A valid header for ``tbl`` (A.6) via ``build_header``, family defaults, then ``over``."""
    kw: dict[str, Any] = {"stage": "collect.arxiv", **_HEADER_DEFAULTS[family_name], **over}
    kw.setdefault("writer", kw["stage"])
    kw.setdefault("inputs", {"documents": f"{rid(0)}.documents"})  # T5.1b: X2/X9 fixtures pin inputs
    kw.setdefault("series", "main")
    # test shorthand: base_file_id (a file id) -> base by name, after = base (D372)
    base_fid = kw.pop("base_file_id", "none")
    if base_fid != "none" and "base" not in kw:
        kw["base"] = kw["after"] = f"20261012T012900Z_{base_fid.split('.')[0]}.full.parquet"
    return build_header(tbl, family_name, run_id=rid(run), config_digest=dg("config"),
                        finished_at=AT, code_digest=dg("code"), plan="dev-v2", **kw)


def data_file(h: dict[str, str]) -> DataFile:
    """The data file a header belongs under (D372; `docs/OPERATE.md`, "Data files: series and chains"): ``data/<chain_id>/<series>/<name>``."""
    return make_data_file(h["chain_id"], h.get("series") or None, AT, h["run_id"], h["kind"],
                          comment=h.get("comment") or None)


def file_name(h: dict[str, str]) -> str:
    """The store key (``data/<chain_id>/<series>/<name>``) a header belongs under."""
    return data_file(h).key


def valid_file(family_name: str, n: int = 3) -> tuple[pa.Table, dict[str, str]]:
    """``n`` valid rows of ``family_name`` as a table with its header."""
    make = ROW_FACTORIES[family_name]
    if family_name in ("documents", "corpus"):
        states = ["identified", "triaged", "acquired"]
        rows = [make(i, state=states[i % 3]) for i in range(n)]
    else:
        rows = [make(i) for i in range(n)]
    tbl = table(family_name, rows)
    return tbl, header(family_name, tbl)


# ---------------------------------------------------------------- S1-S10 failing fixtures
def _s1() -> tuple[pa.Table, dict[str, str]]:
    # `attempts` int64 instead of int32 (Appendix B's example).
    spec = family("coverage")
    bad = pa.schema([pa.field(f.name, pa.int64(), f.nullable) if f.name == "attempts" else f
                     for f in spec.schema])
    tbl = pa.Table.from_pylist([coverage_row(0)], schema=bad)
    return tbl, header("coverage", table("coverage", [coverage_row(0)]))


def _s2() -> tuple[pa.Table, dict[str, str]]:
    # NULL current_text_status in a content block.
    row = document_row(0, state="acquired")
    row["content"]["current_text_status"] = None
    tbl = table("documents", [row])
    return tbl, header("documents", tbl)


def _s3() -> tuple[pa.Table, dict[str, str]]:
    # doc_id twice in a part file.
    tbl = table("documents", [document_row(0), document_row(0)])
    return tbl, header("documents", tbl, kind="part", base_file_id=f"{rid(0)}.documents")


def _s4() -> tuple[pa.Table, dict[str, str]]:
    # `ok` with error_class `dead`.
    tbl = table("screen", [screen_row(0, error_class="dead")])
    return tbl, header("screen", tbl)


def _s5() -> tuple[pa.Table, dict[str, str]]:
    # a role twice.
    row = document_row(0, state="acquired")
    row["content"]["roles"] = row["content"]["roles"] * 2
    tbl = table("documents", [row])
    return tbl, header("documents", tbl)


def _s6() -> tuple[pa.Table, dict[str, str]]:
    # raw.sha256 = 'abc'.
    row = document_row(0, state="acquired")
    row["inband"]["entries"][0]["raw"] = blob("abc", 3, "text/html")
    tbl = table("documents", [row])
    return tbl, header("documents", tbl)


def _s7() -> tuple[pa.Table, dict[str, str]]:
    # a file without a base: the base key is missing.
    tbl = table("documents", [document_row(0)])
    h = header("documents", tbl)
    del h["base"]
    return tbl, h


def _s8() -> tuple[pa.Table, dict[str, str]]:
    # `triaged` without annotations.triage.
    row = document_row(0, state="triaged")
    row["annotations"]["triage"] = None
    tbl = table("documents", [row])
    return tbl, header("documents", tbl)


def _s9() -> tuple[pa.Table, dict[str, str]]:
    # `acquired` after a member joined: content.members_digest is the old membership's.
    row = document_row(0, state="acquired")
    row["content"]["members_digest"] = dg(("members", "before the join"))
    tbl = table("documents", [row])
    return tbl, header("documents", tbl)


def _s10() -> tuple[pa.Table, dict[str, str]]:
    # `screen` both typed and in other[] (a corpus row; typed screen is allowed there).
    row = document_row(0, state="acquired")
    payload = {"verdict": "relevant", "basis_role": "full_text", "buckets": [], "quotes": [],
               "off_topic_reason": None, "confidence": None, "reasoning": None,
               "adjudication_tier": None, "tier1_verdict": None, "tier1_model": None}
    row["annotations"]["screen"] = _ann("screen", payload, basis_kind="text")
    row["annotations"]["other"] = [{**_ann("screen", None, basis_kind="text"),
                                    "payload": {"item_key": "", "payload_json": "{}"}}]
    tbl = table("corpus", [row])
    return tbl, header("corpus", tbl)


#: Check name → builder of ``(table, header)`` that fails exactly that check.
S_FIXTURES: dict[str, Callable[[], tuple[pa.Table, dict[str, str]]]] = {
    "schema_equal": _s1,
    "nonnull": _s2,
    "key_unique": _s3,
    "status_pairing": _s4,
    "nested_unique": _s5,
    "id_formats": _s6,
    "kv_complete": _s7,
    "state_blocks": _s8,
    "digest_agreements": _s9,
    "payload_typed": _s10,
}


def content_entries(row: Row) -> list[Row]:
    """A ``sources``/``documents`` row's in-band entries without its ``provider_record`` (D522)."""
    return [e for e in (row.get("inband") or {}).get("entries") or [] if e["content_role"] != "provider_record"]
