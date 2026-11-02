"""``collect.manual`` and the manual-source flags (round 4, track L): ``manual_sources`` override
validation (``overrides.add``'s, D338) and the effective rule on it (D337); one scenario on a temp store under the real ``links-scratch`` plan: the newsletter
feeds (``collect.feeds`` over the frozen fixtures, fake sender) and four manual entries (a paper,
a web page flagged ``collect_links`` + ``accept``, a LessWrong post flagged ``accept``, a tweet)
are collected, typed to ``manual_<group>``, gathered (the ``flag`` keys reach
``documents.identity.keys`` and merge nothing), and the plan's gate clauses evaluated on the gate
view (``newsletter_posts`` selects the feed documents, ``manual_accept`` the two accepted ones);
then a flag is removed (a superseding entry) and two entries revoked: the flag leaves the
document, the revoked sources are retracted and their flags gone; an unchanged entry set writes
nothing.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest
import yaml

from stack import gate, stage_runner
from stack.checks.verify import run_checks
from stack.overrides import (
    MANUAL_SOURCES,
    OverrideError,
    check_add,
    effective,
    read_file,
)
from stack.plan import load_plan
from stack.scholarly_records import FLAGS
from stack.stage_runner import StageError
from stack.store.chain import head_of, read_view, resolve
from stack.store.layout import parse_data_key
from tests.fakes.overrides import OVERRIDES_DIR, add_overrides
from tests.synth.records import run_line, sdir
from tests.test_collect_feeds import fixture_bodies, sender

REPO = pathlib.Path(__file__).resolve().parent.parent
PLAN = load_plan(REPO / "configs" / "plans" / "links-scratch.yaml")

PAPER = "https://arxiv.org/abs/2409.15747v2"
PAGE = "https://example.org/reading-list/?utm_source=x"
LW = "https://www.lesswrong.com/posts/abcDEF123ghiJKL45/some-post"
TWEET = "https://x.com/someone/status/1234567890123"
OTHER = "https://example.com/other-page"


def entry(oid: str, url: str | None = None, flags: list[str] | None = None, **more: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"override_id": oid}
    if url is not None:
        e.update(url=url, note=f"note {oid}", added_by="tomas", added_at="2026-10-12")
    if flags is not None:
        e["flags"] = flags
    e.update(more)
    return e


#: An entry's metadata fields as written, and as the source's (and its document's) `metadata` holds them.
OTHER_FIELDS: dict[str, Any] = {"title": "Other Page", "authors": ["Ann Author", "Bo Builder"],
                                "published_at": "2014-03-02"}
OTHER_METADATA: dict[str, Any] = {
    "title": "Other Page", "published_at": dt.datetime(2014, 3, 2, tzinfo=dt.UTC),
    "authors": [{"name": "Ann Author", "orcid": None, "affiliation_raw": None},
                {"name": "Bo Builder", "orcid": None, "affiliation_raw": None}]}


def write(store: pathlib.Path, entries: list[dict[str, Any]]) -> None:
    """Add the entries the store's overrides chain lacks (one ``overrides.add`` run)."""
    store.mkdir(parents=True, exist_ok=True)
    add_overrides(store, MANUAL_SOURCES, entries, only_new=True)


def _effective(path: pathlib.Path, entries: list[dict[str, Any]]) -> list[str]:
    path.write_text(yaml.safe_dump(entries, sort_keys=False))
    rows = check_add(read_file(path, now=dt.datetime(2026, 10, 13, tzinfo=dt.UTC)), [])
    return [o.override_id for o in effective(rows, MANUAL_SOURCES).rows]


def test_manual_sources_file_validation(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "manual_sources.yaml"
    assert _effective(path, [entry("a", PAPER, ["accept"]), entry("b", PAGE), entry("c", revokes="b")]) == ["a"]
    bad = {
        "unknown flag": [entry("a", PAPER, ["fetch_now"])],
        "flag twice": [entry("a", PAPER, ["accept", "accept"])],
        "not http": [entry("a", "ftp://example.org/x")],
        "revoke with url": [entry("a", PAPER), entry("b", PAGE, revokes="a")],
        "no url": [entry("a")],
        "author spelling": [entry("a", PAPER, author="tomas")],
        "id twice": [entry("a", PAPER), entry("a", PAGE)],
    }
    for why, entries in bad.items():
        with pytest.raises(OverrideError):
            _effective(path, entries)
            pytest.fail(why)
    # two entries naming one target (equal first typed key) are one group: the later one wins (D337)
    later = entry("b", "https://arxiv.org/pdf/2409.15747", added_at="2026-10-13")
    assert _effective(path, [entry("a", PAPER), later]) == ["b"]
    assert _effective(path, [entry("a", PAPER, added_at="2026-10-14"), later]) == ["a"]
    # a superseded entry: a revoke row plus the replacement
    assert _effective(path, [entry("a", PAPER), entry("b", "https://arxiv.org/pdf/2409.15747", ["accept"],
                                                     supersedes="a")]) == ["b"]
    assert set(FLAGS) == {"collect_links", "accept"}


#: Manual-source metadata values refused at add time, and the reason; the same values a `fields` override
#: carries are refused by gather (one validator, :func:`stack.overrides.field_value`).
BAD_FIELDS: dict[str, dict[str, Any]] = {
    "a year as a number (read as a Unix time otherwise)": {"published_at": 2015},
    "a year alone as text": {"published_at": "2015"},
    "not a date": {"published_at": "spring 2015"},
    "a title that is not text": {"title": 42},
    "an empty title": {"title": "  "},
    "authors as one string": {"authors": "Fallenstein, Soares"},
    "an author mapping with an unknown key": {"authors": [{"name": "A. Author", "email": "a@x"}]},
    "an empty author name": {"authors": [""]},
    "a field manual sources do not carry": {"abstract": "text"},
}


def test_manual_source_metadata_fields_are_validated_and_kept_in_the_payload(tmp_path: pathlib.Path) -> None:
    """`title`, `authors`, `published_at` on an entry: the `sources` metadata fields, in the forms a `fields`
    override takes, validated by the same function; kept in the row's payload only when given, so an entry
    without them keeps the payload (and the overrides digest) it had before the fields existed."""
    from stack.overrides import field_value

    path = tmp_path / "manual_sources.yaml"
    good = entry("a", PAGE, ["accept"], title="Vingean Reflection", published_at="2015-01-01",
                 authors=["Benja Fallenstein", {"name": "Nate Soares", "orcid": None}])
    path.write_text(yaml.safe_dump([good, entry("b", OTHER)], sort_keys=False))
    a, b = check_add(read_file(path, now=dt.datetime(2026, 10, 13, tzinfo=dt.UTC)), [])
    assert dict(a.payload) == {"url": PAGE, "flags": ["accept"], "note": "note a", "title": "Vingean Reflection",
                               "published_at": "2015-01-01",
                               "authors": ["Benja Fallenstein", {"name": "Nate Soares", "orcid": None}]}
    assert dict(b.payload) == {"url": OTHER, "flags": [], "note": "note b"}
    # a YAML date reads as the same ISO string
    path.write_text(f"- {{override_id: c, url: '{PAGE}', published_at: 2015-01-01}}\n")
    (c,) = read_file(path, now=dt.datetime(2026, 10, 13, tzinfo=dt.UTC))
    assert c.payload["published_at"] == "2015-01-01"
    assert field_value("published_at", "2015-01-01") == dt.datetime(2015, 1, 1, tzinfo=dt.UTC)
    assert field_value("authors", ["A. Author"]) == [{"name": "A. Author", "orcid": None, "affiliation_raw": None}]
    for why, more in BAD_FIELDS.items():
        with pytest.raises(OverrideError):
            _effective(path, [entry("a", PAPER, **more)])
            pytest.fail(why)
    with pytest.raises(OverrideError):
        _effective(path, [entry("a", PAPER), entry("b", revokes="a", title="T")])
        pytest.fail("a revoke with a title")


def test_fields_overrides_refuse_what_manual_sources_refuse() -> None:
    """gather's `fields` overrides and the manual sources read values through one function."""
    from stack.overrides import Override
    from workflows.gather.merge import field_ops
    from workflows.gather.normalise import GatherError

    for why, more in BAD_FIELDS.items():
        ((f, v),) = more.items()
        if f == "title" and isinstance(v, str) or f == "abstract":
            continue  # gather takes an empty string (absent) and every OVERRIDABLE field
        o = Override(override_id="f1", target="fields", key="s1", item_key="source_id", kind="field", field=f,
                     payload={"value": v}, tier="human", created_at=dt.datetime(2026, 10, 13, tzinfo=dt.UTC))
        with pytest.raises(GatherError, match="fields override f1: "):
            field_ops([o])
            pytest.fail(why)


def gather(store: pathlib.Path) -> None:
    rec = run_line(store, PLAN, "gather")  # every sources.*/main/ with a file, documents/, overrides/
    assert rec.status == "ok", rec.error


def docs_by_url(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Manual entry URL → the document its source is a member of."""
    sid = {r["source_id"]: r["identity"]["external_id"]
           for r in read_view(resolve(store, sdir("sources.manual"))).to_pylist()}
    return {sid[m["source_id"]]: d for d in read_view(resolve(store, sdir("documents"))).to_pylist()
            for m in d["identity"]["members"] if m["source_id"] in sid}


def flag_keys(doc: dict[str, Any]) -> list[str]:
    return sorted(k["value"] for k in doc["identity"]["keys"] if k["kind"] == "flag")


def gate_hits(store: pathlib.Path) -> dict[str, set[str]]:
    """Rule name → doc_ids its links-scratch gate clause selects on the gate view."""
    table = read_view(resolve(store, sdir("documents")))
    view = gate.gate_view(table, gate.channel_groups())
    cls_ = gate.clauses(PLAN.gate)
    hits = gate.sql_hits(view, cls_)
    out: dict[str, set[str]] = {c.rule: set() for c in cls_ if c.rule}
    for doc_id, idx in hits.items():
        for c in cls_:
            if c.index in idx and c.rule:
                out[c.rule].add(doc_id)
    return out


@pytest.mark.slow  # real store + runner: two collectors, two gathers, checks
def test_manual_sources_flags_through_gather_and_triage_rules(tmp_path: pathlib.Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    monkeypatch.setattr("stack.collect.base.urllib_send", sender(fixture_bodies()))
    write(store, [entry("ms-1", PAPER), entry("ms-2", PAGE, ["collect_links", "accept"]),
                  entry("ms-3", LW, ["accept"]), entry("ms-4", TWEET),
                  entry("ms-6", OTHER, ["accept"], **OTHER_FIELDS)])
    for st in ("collect.feeds", "collect.manual"):
        rec = run_line(store, PLAN, st)
        assert rec.status == "ok", (st, rec.error)
    head = head_of(store, OVERRIDES_DIR.rstrip("/"))  # provenance (design v4.1 §8): the read file's run
    assert rec.inputs["overrides"] == f"{parse_data_key(OVERRIDES_DIR + head).run_id}.overrides", rec.inputs
    manual = {r["identity"]["external_id"]: r for r in read_view(resolve(store, sdir("sources.manual"))).to_pylist()}
    assert {u: r["identity"]["channel"] for u, r in manual.items()} == {
        PAPER: "manual_paper", PAGE: "manual_url", LW: "manual_forum", TWEET: "manual_social",
        OTHER: "manual_url"}
    keys = {u: [(k["kind"], k["value"]) for k in r["identity"]["keys"]] for u, r in manual.items()}
    assert keys == {
        PAPER: [("doi", "10.48550/arxiv.2409.15747"), ("arxiv", "2409.15747")],
        PAGE: [("url", "https://example.org/reading-list"), ("flag", "accept"), ("flag", "collect_links")],
        LW: [("fm_post", "abcDEF123ghiJKL45"), ("flag", "accept")],
        TWEET: [("x_post", "1234567890123")],
        OTHER: [("url", "https://example.com/other-page"), ("flag", "accept")],
    }
    page = manual[PAGE]
    assert (page["identity"]["external_id"], page["identity"]["canonical_url"]) == (
        PAGE, "https://example.org/reading-list/")
    assert (page["metadata"]["origin"], page["metadata"]["license_class"], page["metadata"]["doc_type"]) == (
        "human", "public_web", None)
    assert manual[LW]["metadata"]["doc_type"] == "forum_post"
    # an entry's title, authors and date are its source's metadata, typed as every collector's are
    assert {f: manual[OTHER]["metadata"][f] for f in OTHER_FIELDS} == OTHER_METADATA
    assert {f: page["metadata"][f] for f in OTHER_FIELDS} == {f: None for f in OTHER_FIELDS}
    from stack.collect.inband import json_record
    from stack.store.blobs import open_blobs

    record = json_record(open_blobs(store), page)  # D522: the curated entry is the source's record
    assert (record["url"], record["note"], record["flags"]) == (PAGE, "note ms-2", ["accept", "collect_links"])
    assert not set(OTHER_FIELDS) & set(record)  # an entry without the fields: the record it had before them
    record = json_record(open_blobs(store), manual[OTHER])
    assert {f: record[f] for f in OTHER_FIELDS} == OTHER_FIELDS
    assert {(m["query_id"], m["label"]) for r in manual.values()
            for m in r["metadata"]["matched_by"]} == {("manual:manual_sources", None)}  # D228

    gather(store)
    docs = docs_by_url(store)
    assert {u: flag_keys(d) for u, d in docs.items()} == {
        PAPER: [], PAGE: ["accept", "collect_links"], LW: ["accept"], TWEET: [], OTHER: ["accept"]}
    # flag keys merge nothing: the two url-group pages flagged `accept` stay two documents
    assert len({d["doc_id"] for d in docs.values()}) == 5
    # gather merges them as any member's metadata (origin `human`, the source named in field_sources)
    md = docs[OTHER]["metadata"]
    assert {f: md[f] for f in OTHER_FIELDS} == OTHER_METADATA
    assert {(s["field"], s["origin"]) for s in md["field_sources"]} >= {
        ("title", "human"), ("authors", "human"), ("published_at", "human")}
    assert docs[PAPER]["metadata"]["title"] is None
    hits = gate_hits(store)
    feed_docs = {d["doc_id"] for d in read_view(resolve(store, sdir("documents"))).to_pylist()
                 if d["identity"]["channels"][0] in ("zvi_substack", "import_ai", "paradigm3_news",
                                                     "aisafetychina_substack")}
    assert len(feed_docs) == 8 and hits["newsletter_posts"] == feed_docs  # 2 of Concordia AI's feed (the Chinese-coverage work)
    assert hits["manual_accept"] == {docs[PAGE]["doc_id"], docs[LW]["doc_id"], docs[OTHER]["doc_id"]}
    assert hits["zvi_posts"] == set() and hits["frontier_lab_posts"] == set()
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}

    # An unchanged entry set: no rows (not due within relist_after_h).
    rec = run_line(store, PLAN, "collect.manual")
    assert rec.status == "ok" and "sources" not in rec.outputs and rec.counts["not_due"] == 4
    # D495: a run without `--in data/overrides/<series>/` would read none and retract every manual source:
    # refused before any record (`--no-overrides` runs it on purpose)
    with pytest.raises(StageError, match=r"collect.manual: data/overrides/main/ has overrides"):
        run_line(store, PLAN, "collect.manual", ins=[sdir("sources.manual"), sdir("coverage.manual")])

    # Drop `accept` from the page (a superseding entry), revoke the LessWrong and the other page's entries,
    # and give the paper a title (a superseding entry: how a stored entry picks up fields).
    write(store, [entry("ms-2b", PAGE, ["collect_links"], supersedes="ms-2"),
                  entry("ms-5", revokes="ms-3"), entry("ms-7", revokes="ms-6"),
                  entry("ms-1b", PAPER, title="A Paper Title", supersedes="ms-1")])
    rec = run_line(store, PLAN, "collect.manual")
    assert rec.status == "ok", rec.error
    assert (rec.counts["sources_written"], rec.counts["entries_gone"], rec.counts["sources_unchanged"]) == (4, 2, 1)
    manual = {r["identity"]["external_id"]: r for r in read_view(resolve(store, sdir("sources.manual"))).to_pylist()}
    assert (manual[LW]["state"], manual[OTHER]["state"], manual[PAGE]["state"]) == (
        "retracted", "retracted", "observed")
    assert [k["kind"] for k in manual[LW]["identity"]["keys"]] == ["fm_post"]
    gather(store)
    docs = docs_by_url(store)
    assert {u: flag_keys(d) for u, d in docs.items()} == {
        PAPER: [], PAGE: ["collect_links"], LW: [], TWEET: [], OTHER: []}
    assert docs[LW]["identity"]["members"][0]["withdrawn_at"] is not None
    assert docs[PAPER]["metadata"]["title"] == "A Paper Title"
    assert gate_hits(store)["manual_accept"] == set()
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


def test_a_pinned_wayback_capture_is_the_sources_fetch_url(tmp_path: pathlib.Path) -> None:
    """An entry URL pinning one Wayback capture's original bytes (``…/web/<ts>id_/<url>``) is the source's
    ``canonical_url`` as written, so acquire fetches the archived copy and not the live host (TC260 answers scripted
    clients with its homepage); a replay-form Wayback URL still unwraps to the live page. The keys are the live
    page's either way."""
    live = "https://www.tc260.org.cn/upload/2024-09-09/1725849192841090989.pdf"
    pin = f"https://web.archive.org/web/20250708033232id_/{live}"
    replay = "https://web.archive.org/web/20250516072703/https://www.tc260.org.cn/upload/2025-01-24/1.pdf"
    store = tmp_path / "store"
    write(store, [entry("ms-pin", pin, ["accept"]), entry("ms-replay", replay, ["accept"])])
    rec = run_line(store, PLAN, "collect.manual")
    assert rec.status == "ok", rec.error
    manual = {r["identity"]["external_id"]: r for r in read_view(resolve(store, sdir("sources.manual"))).to_pylist()}
    assert manual[pin]["identity"]["canonical_url"] == pin
    assert manual[replay]["identity"]["canonical_url"] == "https://www.tc260.org.cn/upload/2025-01-24/1.pdf"
    assert ("url", "https://tc260.org.cn/upload/2024-09-09/1725849192841090989.pdf") in [
        (k["kind"], k["value"]) for k in manual[pin]["identity"]["keys"]]


def test_flag_kind_is_non_merging_on_every_manual_channel() -> None:
    from stack.config import load
    from stack.scholarly_records import MERGING_KEY_KINDS, NON_MERGING_KEY_KINDS

    assert "flag" in NON_MERGING_KEY_KINDS and all("flag" not in v for v in MERGING_KEY_KINDS.values())
    manual = {c.id: c for c in load().channels.channels if c.id.startswith("manual_")}
    assert sorted(manual) == ["manual_forum", "manual_paper", "manual_social", "manual_url"]
    assert all(c.non_merging_keys == ["flag"] and c.miss_means == "withdrawn" and c.kind == "manual"
               and c.channel_group == ch.removeprefix("manual_") for ch, c in manual.items())
    assert stage_runner.stage_module_path("collect.manual").exists()


# ---------------------------------------------------------------- the prepared configs/overrides/manual_sources.yaml
PREPARED = REPO / "configs" / "overrides" / "manual_sources.yaml"
#: Id prefix -> the flags every entry under it carries (the file's header says what each block is).
PREPARED_FLAGS = {"ms-r2-lm-": ["accept"], "ms-r2-d3-": ["accept"], "ms-r2-miri-pdf-": ["accept"],
                  "ms-r2-miri-post-": ["collect_links"], "ms-r2-miri-similar-": ["collect_links"]}


def _prepared() -> list[Any]:
    return check_add(read_file(PREPARED, now=dt.datetime(2026, 10, 21, tzinfo=dt.UTC)), [])


def test_prepared_manual_sources_load_and_every_entry_is_effective() -> None:
    """The prepared file loads through `stack overrides add`'s own reader and checks (ids unique, URLs
    http(s), known flags), and no entry shadows another: two entries on one target (equal first typed
    key) would leave only the later one effective, silently."""
    rows = _prepared()
    assert rows and all(o.target == MANUAL_SOURCES and o.kind == "row" for o in rows)
    eff = effective(rows, MANUAL_SOURCES)
    assert len(eff.rows) == len(rows), sorted(o.key for o in rows if [p.key for p in rows].count(o.key) > 1)
    by_prefix: dict[str, list[Any]] = {p: [] for p in PREPARED_FLAGS}
    for o in rows:
        prefix = next((p for p in PREPARED_FLAGS if o.override_id.startswith(p)), None)
        assert prefix is not None, f"{o.override_id}: no known id prefix"
        assert sorted(o.payload["flags"]) == PREPARED_FLAGS[prefix], o.override_id
        assert o.note, f"{o.override_id}: the note carries title, year and ids"
        by_prefix[prefix].append(o)
    # D658: 14 venue-gap landmarks + Chollet (#119); 3 D654 fallbacks; the 37 MIRI
    # "New paper/report" posts (+ 8 similar announcement posts)
    assert {p: len(v) for p, v in by_prefix.items()} == {
        "ms-r2-lm-": 15, "ms-r2-d3-": 3, "ms-r2-miri-pdf-": 42, "ms-r2-miri-post-": 37, "ms-r2-miri-similar-": 8}


def test_prepared_manual_sources_carry_their_metadata_as_fields() -> None:
    """D666: every `accept` entry gives its title and date as fields (the source's metadata, not only the note);
    the announcement posts give none (their `miri_blog` member has the post's title, which outranks a manual
    one anyway). A sample checked by hand against the PDFs' first pages is pinned."""
    from workflows.collect.manual.main import entry_metadata
    from stack.urls import link_keys

    rows = {o.override_id: o for o in _prepared()}
    for oid, o in rows.items():
        md = entry_metadata(o, link_keys(o.payload["url"]))
        if "accept" in o.payload["flags"]:
            assert md.get("title") and md.get("published_at") is not None, oid
            assert md["title"].strip() == md["title"] and "  " not in md["title"], oid
            assert dt.datetime(2000, 1, 1, tzinfo=dt.UTC) < md["published_at"] < dt.datetime(2020, 1, 1, tzinfo=dt.UTC), oid
        else:
            assert not set(o.payload) & {"title", "authors", "published_at"}, oid
    sample = {  # read off the first pages (and the arXiv abstract page) on 2026-10-21
        "ms-r2-lm-96-vingean-reflection": ("Vingean Reflection: Reliable Reasoning for Self-Improving Agents",
                                           ["Benja Fallenstein", "Nate Soares"], "2015-01-15"),
        "ms-r2-lm-95-tiling-agents": ("Tiling Agents for Self-Modifying AI, and the Löbian Obstacle",
                                      ["Eliezer Yudkowsky", "Marcello Herreshoff"], "2013-10-07"),
        "ms-r2-lm-119-measure-of-intelligence": ("On the Measure of Intelligence", ["François Chollet"], "2019-11-05"),
        "ms-r2-miri-pdf-distributionsallowingtiling": (  # the note says Fallenstein; the PDF names Yudkowsky
            "Distributions Allowing Tiling of Staged Subjective EU Maximizers", ["Eliezer Yudkowsky"], "2014-05-11"),
        "ms-r2-miri-pdf-scientificinduction": (  # the note says "Metamathematics"
            "Scientific Induction in Probabilistic Mathematics", ["Jeremy Hahn"], "2013-12-31"),
        "ms-r2-miri-pdf-tdt": ("Timeless Decision Theory", ["Eliezer Yudkowsky"], "2010-01-01"),
    }
    for oid, (title, authors, date) in sample.items():
        p = rows[oid].payload
        assert (p["title"], p["authors"], p["published_at"]) == (title, authors, date), oid


def test_prepared_manual_sources_land_on_the_intended_channels_and_keys() -> None:
    """arXiv/DOI URLs are `manual_paper` keyed by the id (so gather merges them with any member of the
    same paper); the MIRI post URLs carry the `url` key the `miri_blog` documents have (checked on the
    mirror for all 45 on 2026-10-21), so their `collect_links` flag lands on those documents."""
    from stack.config import load
    from stack.urls import link_keys
    from workflows.collect.manual.main import channel_of

    channels = {c.id for c in load().channels.channels}
    for o in _prepared():
        target = link_keys(o.payload["url"])
        assert target is not None and channel_of(target) in channels, o.override_id
        if "arxiv.org/" in o.payload["url"] or "doi.org/" in o.payload["url"]:
            assert target.group == "paper" and target.keys[0][0] in ("arxiv", "doi"), o.override_id
        if o.override_id.startswith(("ms-r2-miri-post-", "ms-r2-miri-similar-")):
            assert target.group == "url" and o.key.startswith("url=https://intelligence.org/20"), o.override_id
            assert not o.key.endswith("/"), o.override_id
    quant = next(o for o in _prepared() if o.payload["url"].endswith("/2015/11/29/new-paper-quantilizers/"))
    assert quant.key == "url=https://intelligence.org/2015/11/29/new-paper-quantilizers"  # the mirror doc's key


def test_collect_links_keeps_a_flagged_post_s_own_host_pdfs() -> None:
    """The MIRI PDFs live on the announcement posts' host. An unflagged linking page drops its own host as a
    self-link; a page flagged `collect_links` keeps a same-host link typed `pdf` (D665), so `collect_links` on
    the posts reaches the PDFs too. The prepared file still lists them as `accept` entries (D658) until a build
    shows the selector picks them; both lanes key the URL alike, so they join one document."""
    from stack.collect.links import extract
    from stack.overrides import manual_key

    pdf = "https://intelligence.org/files/QuantilizersSaferAlternative.pdf"
    body = (f"<html><body><article><p>New paper: <a href='{pdf}'>Quantilizers</a>, also "
            "<a href='https://arxiv.org/abs/1609.03543'>Logical induction</a>.</p>"
            "<p><a href='https://intelligence.org/category/news/'>News</a></p></article></body></html>").encode()
    post = "https://intelligence.org/2015/11/29/new-paper-quantilizers/"
    got = extract(body, base_url=post, fragment=False)
    assert [d["reason"] for d in got.dropped] == ["self", "self"]
    assert list(got.candidates) == ["doi:10.48550/arxiv.1609.03543"]  # an off-host link stays a candidate
    got = extract(body, base_url=post, fragment=False, flagged=True)
    assert [d["url"] for d in got.dropped] == ["https://intelligence.org/category/news/"]
    assert list(got.candidates) == [f"url:{pdf}", "doi:10.48550/arxiv.1609.03543"]
    assert manual_key(pdf) == f"url={pdf}"  # the manual entry's key for the same URL


@pytest.mark.parametrize("other", ["anthropic_alignment_blog", "anthropic_cards", None])
def test_gather_ranks_a_manual_title_after_every_collected_one(tmp_path: pathlib.Path, other: str | None) -> None:
    """D666: the manual channels rank after every collected channel (`PROVIDER_LAST`, before the link channels),
    so a manual source's metadata only fills gaps: a blog's or card's title on the same document wins even when
    the manual row is newer; alone, the manual title stands; a link anchor still ranks after it. A `fields`
    override is the way to force a value."""
    from tests.test_gather_identity import Store, src

    st = Store(tmp_path)
    url = "https://example.org/post"
    manual = src(2, [("url", url), ("flag", "accept")], channel="manual_url", t=90)  # the newer row
    manual["metadata"].update(title="The Manual Title", origin="human")
    link = src(3, [("url", url)], channel="links_url", t=120)
    link["metadata"]["title"] = "an anchor text"
    st.add("sources.manual", [manual])
    st.add("sources.links", [link])
    if other is not None:
        page = src(1, [("url", url)], channel=other)
        page["metadata"]["title"] = "The Collected Title"
        st.add("sources.blogs" if "blog" in other else "sources.cards", [page])
    st.gather()
    (doc,) = st.docs().values()
    assert doc["metadata"]["title"] == ("The Collected Title" if other else "The Manual Title")
    origin = [e["origin"] for e in doc["metadata"]["field_sources"] if e["field"] == "title"]
    assert origin == (["provider"] if other else ["human"])
