"""The synthetic world of the scenario tests (D46): the v4e mock's world
(the v4 design mock ``v4e``'s ``stack_mock.py``) ported onto ``tests/synth/rows.py``'s factories and
the ``documents`` blocks of Appendix A. Deterministic, offline, small (16 sources, 15 documents).

What the world holds (labels are the mock's: ``A`` arXiv, ``O`` OpenAlex, ``LW``/``AF`` the
forum crosspost, ``X`` a thread, ``W`` web pages on the ``manual`` channel, chain ``sources.web``):

* ``acquired``: A1 (full text, plus an ``xml`` snapshot the pointer never takes and an in-band
  abstract), A2 (full text; its ``xml`` role failed), A4 (the survivor of the A4+O4 merge), A7
  (withdrawn at the provider: no raw, ``no_raw``), O2 (full text whose conversion failed:
  ``convert_failed``, never the in-band abstract), O5 (paywalled: the in-band abstract is
  current), LW (one document with the AlignmentForum crosspost AF as a second live member: two
  in-band ``post`` snapshots, the newer wins), W1 (a live page and a newer wayback capture: the
  live one wins), W2/W3 (one blog post at two URLs: equal raw digest, ``same_raw_doc_ids``);
* ``triaged`` with ``decision = 'skip'``: A3 (protein folding), O3 (crop yields, no abstract);
* ``identified``: A6 (new, not yet triaged), X (a thread, masked by an operator);
* ``retired``: O4 (the journal version, merged into A4: ``merged_into`` / ``former_doc_ids``).

:func:`build_world` writes one ``sources.<channel>`` chain per channel (``sources.arxiv`` is a
full file plus a part file re-observing A1 at provider version 2), ``coverage.arxiv`` and
``coverage.openalex``, and the ``documents`` chain as **one full ``gather`` file** that carries
the ``content`` and ``annotations`` blocks of earlier acquire/triage runs byte for byte (§2.4;
those runs are not in this store: :data:`RUN_ACQUIRE` has no record). Every file goes through
the family writer (``tests.fixtures.checks.write_world``) with an ``ok`` run record, and one
``ok`` build record pins the heads and records the gate the stand-in triage rows were decided
under (:data:`GATE`, stored by ``write_world``), so the clean world verifies with every implemented check
``ok`` (``47 checks: 22 ok, 25 skipped[not_implemented]`` in M1).

Later scenario tests (M2 gather/triage, M3 acquire, M5 build) start from these rows. The pointer
choice recorded in each ``content`` block is the referent ``acquire``'s pointer must reproduce
(§5.5; ``tests/test_scenario_m1.py``, step 3). :func:`write_on_head` publishes further files
on a chain head with an ``ok`` record, as a stage run would.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stack.gate import gate_digest
from stack.schemas.structs import text_blob
from stack.runs import RunRecord, write_final_record
from stack.store.chain import HeaderFields, Published, head_of, write_file
from stack.store.layout import NONE, make_data_file, series_dir, tie_guard
from tests.fixtures.checks import FileSpec, T, fid, id_gate, write_world
from tests.fixtures.checks import World as FileWorld
from tests.synth.records import SERIES
from tests.synth.rows import (
    AT,
    CONVERT_PIN,
    Row,
    _ann,
    blob,
    coverage_row,
    dg,
    document_row,
    eid,
    mint,
    rid,
    sid,
    source_row,
    table,
    with_digests,
)

#: Runs of the world (one hour apart from ``AT``, ``tests.fixtures.checks.T``). Collectors 1-7,
#: gather 8; the acquire run that wrote the carried ``content`` blocks is not in the store.
RUN_COLLECT: dict[str, int] = {"arxiv": 1, "openalex": 2, "lesswrong": 3, "alignmentforum": 4,
                               "x": 5, "web": 6}
RUN_ARXIV_PART, RUN_GATHER, RUN_ACQUIRE = 7, 8, 20
PLAN = "dev-v2"
#: The documents the stand-in triage annotations say ``fetch`` (``wanted_by = [screen]``); the
#: rest of the triaged ones say ``skip``.
FETCHED = ("A1", "A2", "A4", "A7", "O2", "O5", "LW", "W1", "W2", "W3")
GATHER_BLOCKS = ["identity", "metadata", "inband"]
CONVERT = CONVERT_PIN
C0, C1 = AT - dt.timedelta(days=2), AT - dt.timedelta(days=1)  # capture times (older, newer)


# ---------------------------------------------------------------- sources of the world
@dataclass(frozen=True)
class Src:
    """One source of the synthetic web (the mock's ``WORLD`` row)."""

    label: str
    i: int
    channel: str
    key_kind: str
    key_value: str
    url: str
    title: str
    abstract: str | None
    doc_type: str = "preprint"
    license_class: str = "open"
    #: The collector stage suffix (``sources.<collector>`` chain) when it is not the channel id
    #: (D-A: chain = stage suffix, ``identity.channel`` = the ``channels.yaml`` id).
    collector: str | None = None

    @property
    def source_id(self) -> str:
        return sid(self.i)

    @property
    def chain(self) -> str:
        return self.collector or self.channel


def _arxiv(label: str, i: int, title: str, abstract: str | None) -> Src:
    aid = f"2410.{i:05d}"
    return Src(label, i, "arxiv", "arxiv", aid, f"https://arxiv.org/abs/{aid}", title, abstract)


SOURCES: dict[str, Src] = {s.label: s for s in [
    _arxiv("A1", 1, "Evaluating deception in language model agents", "We evaluate deceptive behaviour."),
    _arxiv("A2", 2, "A benchmark for dangerous capabilities", "A benchmark suite."),
    _arxiv("A3", 3, "Protein folding with diffusion", "Biology only."),
    _arxiv("A4", 4, "Red-teaming frontier models: a survey", "A survey of red-team practice."),
    _arxiv("A6", 6, "Benchmarking evaluation awareness", "Evaluation awareness benchmark."),
    _arxiv("A7", 7, "Withdrawn: an eval of jailbreak robustness", None),
    Src("O2", 12, "openalex", "doi", "10.1/bbb", "https://doi.org/10.1/bbb",
        "Measuring sycophancy in evaluations", "Sycophancy evals.", "article"),
    Src("O3", 13, "openalex", "doi", "10.1/ccc", "https://doi.org/10.1/ccc", "Crop yield forecasting",
        None, "article"),
    Src("O4", 14, "openalex", "doi", "10.1/ddd", "https://doi.org/10.1/ddd",
        "Red-teaming frontier models (journal version)", "A survey of red-team practice.", "article"),
    Src("O5", 15, "openalex", "doi", "10.1/eee", "https://paywalled.example/eee",
        "Paywalled study of model evaluation", "We study model evaluation.", "article", "restricted"),
    Src("LW", 21, "lesswrong", "forum_post", "abc123", "https://www.lesswrong.com/posts/abc123",
        "Sandbagging evals: what we found", None, "post", "public_web"),
    Src("AF", 22, "alignmentforum", "forum_post", "abc123", "https://www.alignmentforum.org/posts/abc123",
        "Sandbagging evals: what we found", None, "post", "public_web"),
    Src("X", 31, "x", "x_post_id", "1850000000000000001",
        "https://x.com/someone/status/1850000000000000001", "Thread: new results on agent safety", None,
        "post", "api_tos"),
    Src("W1", 41, "manual", "url", "example.org/watched-policy", "https://example.org/watched-policy",
        "Lab policy page", None, "page", "public_web", collector="web"),
    Src("W2", 42, "manual", "url", "blog-a.example/eval-harness", "https://blog-a.example/eval-harness",
        "Notes on our eval harness", None, "post", "public_web", collector="web"),
    Src("W3", 43, "manual", "url", "mirror.example/copy-of-eval-harness",
        "https://mirror.example/copy-of-eval-harness", "Mirror: notes on an eval harness", None, "post",
        "public_web", collector="web"),
]}

#: In-band bodies the collectors minted (§5.4): abstracts as text only (``raw`` NULL),
#: forum posts as raw HTML with their text. ``label -> [(content_role, raw, text, media, at)]``.
INBAND: dict[str, list[tuple[str, str | None, str, str, dt.datetime]]] = {
    **{s.label: [("abstract", None, dg(("abstract", s.label)), "text/plain", C0)]
       for s in SOURCES.values() if s.abstract is not None},
    "LW": [("post", dg(("post-raw", "LW")), dg(("post-text", "LW")), "text/html", C0)],
    "AF": [("post", dg(("post-raw", "AF")), dg(("post-text", "AF")), "text/html", C1)],
}


#: The recorded sizes of the synthetic blobs (the world's blobs are digests of labels, not bytes): a fetched
#: raw body, and a text (in-band or converted; above the quality gate's paper-body floor).
RAW_SIZE, SIZE = 1000, 9600


def inband_entries(label: str) -> list[Row]:
    return [{"source_id": SOURCES[label].source_id, "content_role": role,
             "raw": blob(raw, SIZE, media) if raw else None,
             "text": blob(text, SIZE, "text/markdown" if raw else media), "captured_at": at}
            for role, raw, text, media, at in INBAND.get(label, [])]


# ---------------------------------------------------------------- sources/coverage rows
def sources_row(label: str, run: int, **over: Any) -> Row:
    s = SOURCES[label]
    row = source_row(s.i, run)
    row["record_digest"] = dg(("record", label, run))
    row["identity"] = {"source_id": s.source_id, "channel": s.channel, "external_id": s.key_value,
                       "canonical_url": s.url, "keys": [{"kind": s.key_kind, "value": s.key_value}]}
    row["metadata"].update(title=s.title, abstract=s.abstract, doc_type=s.doc_type,
                           license_class=s.license_class, is_withdrawn=label == "A7")
    row["inband"] = {"entries": inband_entries(label), "run_id": rid(run), "updated_at": T(run)}
    row.update(over)
    return row


# ---------------------------------------------------------------- documents rows
def member(label: str, *, edge_kind: str = "anchor", edge_to: str | None = None,
           is_live: bool = True) -> Row:
    s = SOURCES[label]
    withdrawn = label == "A7"
    return {"source_id": s.source_id, "channel": s.channel, "key_kind": s.key_kind,
            "key_value": s.key_value, "canonical_url": s.url, "title": s.title, "published_at": C0,
            "doc_type": s.doc_type, "license_class": s.license_class, "is_live": is_live,
            "edge_kind": edge_kind, "edge_to_source_id": SOURCES[edge_to].source_id if edge_to else None,
            "override_id": None, "first_observed_at": C0, "last_observed_at": AT,
            "observation_count": 1, "withdrawn_at": C1 if withdrawn else None,
            "withdrawn_reason": "withdrawn" if withdrawn else None}


def _triage(decision: str, verdict: str | None, i: int) -> Row:
    qs = [] if verdict is None else [{
        "question": "safety_relevance", "status": "ok", "error_class": None, "verdict": verdict,
        "confidence": 0.8, "evidence_id": eid(i), "config_digest": dg("q-config")}]
    return _ann("triage", {"decision": decision, "wanted_by": ["screen"] if decision == "fetch" else [],
                           "questions": qs}, basis_kind="fields")


#: ``"<label>/<role>[/<via>]"`` → ``snapshot_id`` of the carried acquire run (filled below).
SNAPS: dict[str, str] = {}


def _snap(key: str, label: str, kind: str, role: str, via: str, media: str, *, digest: str,
          at: dt.datetime, text: str | None = None, text_status: str | None = None,
          text_error: str | None = None, attempts: int = 0, url: str | None = None,
          text_media: str | None = None) -> Row:
    sid_ = SNAPS.setdefault(key, f"{rid(RUN_ACQUIRE)}.{len(SNAPS)}")
    raw = blob(digest, RAW_SIZE if kind == "raw" else SIZE, media)
    return {"snapshot_id": sid_, "source_id": SOURCES[label].source_id, "kind": kind,
            "content_role": role, "via": via, "url": url, "final_url": None,
            "http_status": 200 if kind == "raw" else None, "etag": None, "last_modified": None,
            "captured_at": at, "as_of": at, "revision": None, "raw": raw,
            "text": (blob(text, SIZE, text_media) if text_media else text_blob(text, SIZE, raw)) if text else None,
            "text_status": text_status,
            "text_error_class": text_error, "convert_attempts": attempts,
            "convert_pin": CONVERT if kind == "raw" and text_status else None}


def _inband_snaps(label: str) -> list[Row]:
    return [_snap(f"{label}/{e['content_role']}", label, "inband", e["content_role"], "inband",
                  (e["raw"] or e["text"])["media_type"], digest=(e["raw"] or e["text"])["sha256"],
                  at=e["captured_at"], text=e["text"]["sha256"], text_media=e["text"]["media_type"], text_status="ok")
            for e in inband_entries(label)]


def _role(label: str, role: str, status: str, lane: str, *, snap: str | None = None,
          error_class: str | None = None, attempts: int = 1) -> Row:
    s = SOURCES[label].source_id
    return {"content_role": role, "source_id": s, "status": status, "error_class": error_class,
            "error": None if error_class is None else error_class, "attempts": attempts,
            "first_attempt_at": C1 if attempts else None, "last_attempt_at": C1 if attempts else None,
            "last_outcome": status if attempts else None, "retry_after": None,
            "tried_urls": [SOURCES[label].url] if attempts else [], "last_source_id": s if attempts else None,
            "etag": None, "last_modified": None, "validator_url": None,
            "snapshot_id": SNAPS[snap] if snap else None, "lane": lane, "rule": None}


def _content(snaps: list[Row], roles: list[Row], current: str | None, *, status: str,
             media: str | None = None, same_raw: Sequence[str] = ()) -> Row:
    cur = next((s for s in snaps if s["snapshot_id"] == SNAPS.get(current or "")), None)
    return {"convert_pin": CONVERT, "snapshots": snaps, "roles": roles,
            "current_snapshot_id": cur["snapshot_id"] if cur else None,
            "current_source_id": cur["source_id"] if cur else None,
            "current_role": cur["content_role"] if cur else None,
            "current_text_sha256": cur["text"]["sha256"] if cur and status == "ok" else None,
            "current_text_status": status, "current_text_media": media if status == "ok" else None,
            "current_text_quality": None, "current_revision": None, "revisions": [],
            "members_digest": "", "same_raw_doc_ids": [SOURCES[x].source_id for x in same_raw],
            "run_id": rid(RUN_ACQUIRE), "updated_at": C1}


def _raw(label: str, role: str, via: str, media: str, *, at: dt.datetime = C1, ok: bool = True,
         raw_label: str | None = None, convert: bool = True) -> Row:
    raw = dg(("raw", raw_label or label, role))
    if not convert:  # a never-text role (xml): fetched, not converted
        return _snap(f"{label}/{role}/{via}", label, "raw", role, via, media, digest=raw, at=at,
                     url=SOURCES[label].url)
    return _snap(f"{label}/{role}/{via}", label, "raw", role, via, media, digest=raw, at=at,
                 text=dg(("text", raw_label or label, role)) if ok else None,
                 text_status="ok" if ok else "failed_permanent", text_error=None if ok else "parse_error",
                 attempts=1 if ok else 2, url=SOURCES[label].url)


def content_blocks() -> dict[str, Row]:
    """The carried ``content`` block of every acquired document, by anchor label."""
    pdf, html = "application/pdf", "text/html"
    return {
        "A1": _content(
            [_raw("A1", "full_text", "live", pdf), _raw("A1", "xml", "live", "application/xml", convert=False),
             *_inband_snaps("A1")],
            [_role("A1", "full_text", "ok", "arxiv", snap="A1/full_text/live"),
             _role("A1", "xml", "ok", "arxiv", snap="A1/xml/live"),
             _role("A1", "abstract", "ok", "inband", snap="A1/abstract")],
            "A1/full_text/live", status="ok", media="md"),
        "A2": _content(
            [_raw("A2", "full_text", "live", pdf), *_inband_snaps("A2")],
            [_role("A2", "full_text", "ok", "arxiv", snap="A2/full_text/live"),
             _role("A2", "xml", "failed_permanent", "arxiv", error_class="dead"),
             _role("A2", "abstract", "ok", "inband", snap="A2/abstract")],
            "A2/full_text/live", status="ok", media="md"),
        "A4": _content(
            [_raw("A4", "full_text", "live", pdf), *_inband_snaps("A4"), *_inband_snaps("O4")],
            [_role("A4", "full_text", "ok", "arxiv", snap="A4/full_text/live"),
             _role("A4", "abstract", "ok", "inband", snap="A4/abstract"),
             _role("O4", "abstract", "ok", "inband", snap="O4/abstract")],
            "A4/full_text/live", status="ok", media="md"),
        "A7": _content(
            [], [_role("A7", "full_text", "skipped", "arxiv", error_class="withdrawn", attempts=0)],
            None, status="no_raw"),
        "O2": _content(
            [_raw("O2", "full_text", "live", pdf, ok=False), *_inband_snaps("O2")],
            [_role("O2", "full_text", "ok", "oa", snap="O2/full_text/live"),
             _role("O2", "abstract", "ok", "inband", snap="O2/abstract")],
            "O2/full_text/live", status="convert_failed"),
        "O5": _content(
            [*_inband_snaps("O5")],
            [_role("O5", "full_text", "failed_permanent", "http", error_class="paywalled"),
             _role("O5", "abstract", "ok", "inband", snap="O5/abstract")],
            "O5/abstract", status="ok", media="txt"),
        "LW": _content(
            [*_inband_snaps("LW"), *_inband_snaps("AF")],
            [_role("LW", "post", "ok", "inband", snap="LW/post"),
             _role("AF", "post", "ok", "inband", snap="AF/post")],
            "AF/post", status="ok", media="md"),
        "W1": _content(
            [_raw("W1", "page", "live", html, at=C0), _raw("W1", "page", "wayback", html, at=C1)],
            [_role("W1", "page", "ok", "http", snap="W1/page/live")],
            "W1/page/live", status="ok", media="md"),
        "W2": _content(
            [_raw("W2", "post", "live", html, raw_label="harness")],
            [_role("W2", "post", "ok", "http", snap="W2/post/live")],
            "W2/post/live", status="ok", media="md", same_raw=["W3"]),
        "W3": _content(
            [_raw("W3", "post", "live", html, raw_label="harness")],
            [_role("W3", "post", "ok", "http", snap="W3/post/live")],
            "W3/post/live", status="ok", media="md", same_raw=["W2"]),
    }


def document(anchor: str, state: str, *, members: Sequence[Row] | None = None,
             content: Row | None = None, triage: Row | None = None, **identity: Any) -> Row:
    """A ``documents`` row as the gather run of the world writes it (digests recomputed)."""
    s = SOURCES[anchor]
    row = document_row(s.i, RUN_GATHER)
    row["state"], row["created_at"] = state, T(RUN_GATHER)
    ms = list(members) if members is not None else [member(anchor)]
    labels = [next(lb for lb, x in SOURCES.items() if x.source_id == m["source_id"]) for m in ms]
    row["identity"].update(
        anchor_source_id=s.source_id, members=ms, member_ids=[m["source_id"] for m in ms],
        keys=[{"kind": SOURCES[lb].key_kind, "value": SOURCES[lb].key_value,
               "source_id": SOURCES[lb].source_id} for lb in labels],
        channels=sorted({SOURCES[lb].channel for lb in labels}), **identity)
    row["metadata"].update(
        title=s.title, abstract=s.abstract, doc_type=s.doc_type, is_withdrawn=anchor == "A7",
        published_at=C0, field_sources=[{"field": f, "source_id": s.source_id, "origin": "provider"}
                                        for f in ("title", "abstract") if f == "title" or s.abstract])
    row["inband"]["entries"] = [e for lb in labels for e in inband_entries(lb)]
    row["content"] = content
    row["annotations"]["triage"] = triage
    with_digests(row)
    for blk in ("identity", "metadata", "inband"):
        row[blk]["run_id"], row[blk]["updated_at"] = rid(RUN_GATHER), T(RUN_GATHER)
    row["run_id"], row["updated_at"] = rid(RUN_GATHER), T(RUN_GATHER)
    return row


def documents_rows() -> dict[str, Row]:
    """Every document of the world, by anchor label, in a fixed order."""
    SNAPS.clear()
    c = content_blocks()
    fetch = {lb: _triage("fetch", "relevant", SOURCES[lb].i) for lb in ("A1", "A2", "A4", "O2", "O5", "LW")}
    fetch |= {"A7": _triage("fetch", "relevant", 7)}
    fetch |= {lb: _triage("fetch", None, SOURCES[lb].i) for lb in ("W1", "W2", "W3")}  # sql: url_no_abstract
    assert sorted(fetch) == sorted(FETCHED), "the stored gate (FETCHED) must select the fetch rows"
    docs = {
        **{lb: document(lb, "acquired", content=c[lb], triage=fetch[lb])
           for lb in ("A1", "A2", "A7", "O2", "O5", "W1", "W2", "W3")},
        "A4": document("A4", "acquired", content=c["A4"], triage=fetch["A4"],
                       members=[member("A4"), member("O4", edge_kind="key:arxiv", edge_to="A4")],
                       former_doc_ids=[SOURCES["O4"].source_id]),
        "LW": document("LW", "acquired", content=c["LW"], triage=fetch["LW"],
                       members=[member("LW"), member("AF", edge_kind="key:forum_post", edge_to="LW")]),
        "A3": document("A3", "triaged", triage=_triage("skip", "irrelevant", 3)),
        "O3": document("O3", "triaged", triage=_triage("skip", "irrelevant", 13)),
        "A6": document("A6", "identified"),
        "X": document("X", "identified", is_masked=True, mask_reason="operator",
                      override_ids=["ov-mask-x"]),
        "O4": document("O4", "retired", members=[member("O4", is_live=False)],
                       merged_into=SOURCES["A4"].source_id),
    }
    return docs


# ---------------------------------------------------------------- the store
@dataclass
class World:
    """What :func:`build_world` wrote: the store root, the ``documents`` rows by label, the
    file ids by key (``documents``, ``sources.<channel>``, ``sources.arxiv@part``,
    ``coverage.<channel>``), the build record, and the snapshot ids by ``label/role[/via]``."""

    root: Path
    docs: dict[str, Row]
    files: dict[str, str]
    build: dict[str, Any]
    snaps: dict[str, str] = field(default_factory=dict)

    def doc_id(self, label: str) -> str:
        return SOURCES[label].source_id

    def label(self, doc_id: str) -> str:
        return next(lb for lb, s in SOURCES.items() if s.source_id == doc_id)


def _file_specs(docs: dict[str, Row]) -> list[FileSpec]:
    by_channel: dict[str, list[str]] = {}
    for lb, s in SOURCES.items():
        by_channel.setdefault(s.chain, []).append(lb)
    specs = []
    for ch, run in RUN_COLLECT.items():
        stage = f"collect.{ch}"
        specs.append(FileSpec(f"sources.{ch}", "sources", run,
                              {"stage": stage, "chain_id": f"sources.{ch}", "kind": "full",
                               "base_file_id": NONE}, [sources_row(lb, run) for lb in by_channel[ch]]))
    again = sources_row("A1", RUN_ARXIV_PART, observed_at=T(RUN_ARXIV_PART))
    again["metadata"]["provider_version"] = 2  # A1 re-observed at a new provider version
    specs.append(FileSpec(
        "sources.arxiv@part", "sources", RUN_ARXIV_PART,
        {"stage": "collect.arxiv", "chain_id": "sources.arxiv", "kind": "part",
         "base_file_id": fid(RUN_COLLECT["arxiv"], "sources")}, [again]))
    for i, ch in enumerate(("arxiv", "openalex")):
        specs.append(FileSpec(f"coverage.{ch}", "coverage", RUN_COLLECT[ch],
                              {"stage": f"collect.{ch}", "chain_id": f"coverage.{ch}"},
                              [coverage_row(i, RUN_COLLECT[ch], channel=ch)]))
    specs.append(FileSpec("documents", "documents", RUN_GATHER,
                          {"stage": "gather", "chain_id": "documents", "kind": "full",
                           "base_file_id": NONE, "blocks_written": GATHER_BLOCKS},
                          list(docs.values())))
    return specs


def build_world(store_root: str | Path) -> World:
    """Write the world into a fresh store at ``store_root`` (see the module docstring)."""
    root = Path(store_root)
    docs = documents_rows()
    specs = _file_specs(docs)
    files = {s.key: fid(s.run, s.family) for s in specs}
    gate = id_gate({"screen": [SOURCES[lb].source_id for lb in FETCHED]})
    chained = sorted(k for k in files if k != "documents")  # "sources.arxiv" sorts before its "@part"
    heads = {f"{k.split('.')[0]}/{k}": {"file_id": files[k]} for k in chained if "@" not in k}
    heads["sources/sources.arxiv"] = {"file_id": files["sources.arxiv@part"]}  # the chain's head
    build: dict[str, Any] = {
        "build_id": mint("b", 1), "plan": PLAN, "plan_digest": dg("plan"), "gate_digest": gate_digest(gate),
        "status": "ok", "started_at": T(9).isoformat(),
        "finished_at": (T(9) + dt.timedelta(minutes=5)).isoformat(), "inputs_pinned_at": T(9).isoformat(),
        "version_chain": [files["documents"], *(files[k] for k in chained)],
        "files": {"documents": {"file_id": files["documents"], "run_id": rid(RUN_GATHER)}, **heads},
        "runs": [rid(r) for r in sorted({s.run for s in specs})], "verify": {"status": "ok", "checks": []},
        "require_complete": ["documents"], "counts": {}}
    write_world(FileWorld(specs, copy.deepcopy(build), gate=gate), root)
    return World(root, docs, files, build, dict(SNAPS))


def write_on_head(store_root: str | Path, chain_id: str, family: str, rows: list[Row], *, stage: str,
                  run: int, blocks_written: Sequence[str] | str = "*", kind: str = "part",
                  at: dt.datetime | None = None, series: str = SERIES, basis_kind: str | None = None) -> Published:
    """Publish ``rows`` as a ``kind`` file on the last file of ``data/<chain_id>/<series>/`` (its
    base and ``after``) with an ``ok`` final record (run ``rid(run)``), as a stage run would leave
    it. ``basis_kind``: the header's, for a ``<wf>`` family. The family writer's refusals
    (``SchemaRefused``) propagate, and nothing is published then."""
    at = at or T(run)
    fields = HeaderFields(stage=stage, run_id=rid(run), config_digest=dg(("config", stage)),
                          code_digest=dg("code"), plan=PLAN, blocks_written=blocks_written,
                          **({"basis_kind": basis_kind} if basis_kind else {}))
    head = head_of(store_root, series_dir(chain_id, series))
    dest = make_data_file(chain_id, series, tie_guard(at, head[:16] or None), rid(run), kind)

    def hook(pubs: list[Published]) -> None:
        write_final_record(store_root, RunRecord(
            run_id=rid(run), stage=stage, writer=stage, plan=PLAN, status="ok", started_at=at,
            heartbeat_at=at, finished_at=at, code_digest=dg("code"), config_digest=dg(("config", stage)),
            scope_complete=True, version={"rows_touched": len(rows)},
            outputs={family: pubs[0].as_output()}))

    return write_file(store_root, dest, table(family, rows), fields, base=head, after=head, finished_at=at,
                      on_published=hook)


__all__ = ["FETCHED", "RUN_ACQUIRE", "RUN_GATHER", "SNAPS", "SOURCES", "Src", "World", "build_world",
           "content_blocks", "document", "documents_rows", "member", "sources_row", "write_on_head"]
