"""Track E2's SR eval data (``stack/evals/sr.py``) with fake HTTP (no network): papers.csv parsing and
link classification; external records equal ``workflows.triage.ask.document_text`` of the row the gate
view would hold, with the excerpt minted as the collectors mint it (arXiv Atom summary, LessWrong search
body); a title-only record is dropped and replaced from its stratum; a document the store already holds
gets the store's own frozen record; the SR-heavy amendment appends without touching existing bytes,
amends the manifest keeping every key, and refuses colliding ids and documents."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from stack.collect.inband import normalize
from stack.evals import freeze as F
from stack.evals import sr
from workflows.triage.ask import document_text

pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

CSV = (
    "agenda_name,agenda_section,agenda_sub_section,agenda_full_name,link_url,link_text,title,authors,"
    "author_organizations,published_year,date\n"
    "Agent foundations,Theory,,Theory / Agent foundations,https://arxiv.org/abs/2501.00001v2,T1,Paper One,A,,2025,\n"
    "Tiling,Theory,,Theory / Tiling,https://arxiv.org/pdf/2501.00001,T1,Paper One,A,,2025,\n"
    "Tiling,Theory,,Theory / Tiling,https://www.lesswrong.com/posts/AbCdEf123/some-slug,T2,Post,B,,2025,\n"
    "Tiling,Theory,,Theory / Tiling,https://www.lesswrong.com/s/SeQ1/p/XyZ987,T3,Seq post,B,,2025,\n"
    "Debate,Black-box,,Black-box / Debate,https://www.alignmentforum.org/posts/AfPost1/x,T4,AF post,C,,2025,\n"
    "ODYSSEY,Theory,,Theory / ODYSSEY,https://openreview.net/forum?id=or1D,T5,OR paper,D,,2025,\n"
    "Psych,Black-box,,Black-box / Psych,https://www.nature.com/articles/s41586-025-0001-x,T6,Nat,E,,2025,\n"
    "Psych,Black-box,,Black-box / Psych,https://www.pnas.org/doi/10.1073/pnas.2500001122,T7,Pnas,E,,2025,\n"
    "Labs,Labs,,Labs / Anthropic,https://alignment.anthropic.com/2025/foo/,T8,Lab post,F,,2025,\n"
    "Tools,Other,,Other / Tools,https://github.com/org/repo,T9,Repo,G,,2025,\n"
    "Orgs,Other,,Other / Orgs,https://metr.org/blog/2025-x/,T10,Org post,H,,2025,\n"
    "Labs,Labs,,Labs / OpenAI,,No link here,,,,,\n"
    "Html,Theory,,Theory / Html,https://arxiv.org/html/2502.00002v1,T11,Paper Two,I,,2025,\n"
)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>http://arxiv.org/abs/2501.00001v2</id><title>Paper
   One: tiling   agents</title><summary>  We study   corrigibility
   of tiling agents under self-modification, with proofs.  </summary>
   <author><name>Ann Author</name></author><author><name>Bob Writer</name></author>
   <published>2025-01-02T00:00:00Z</published></entry>
 <entry><id>http://arxiv.org/abs/2502.00002v1</id><title>Paper Two</title><summary>short</summary>
   <author><name>Cy</name></author><published>2025-02-02T00:00:00Z</published></entry>
</feed>"""

BODY = "First paragraph about embedded agency.  \r\n\r\n\r\n\r\nSecond paragraph, trailing space.   \n"


class FakeHttp:
    """Answers by URL/payload; records every call."""

    def __init__(self, posts: dict[str, dict[str, Any]] | None = None, hits: list[dict[str, Any]] | None = None,
                 works: dict[str, dict[str, Any]] | None = None) -> None:
        self.posts, self.hits, self.works = posts or {}, hits or [], works or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, kw: Any) -> tuple[int, bytes]:
        self.calls.append((method, url, dict(kw)))
        if "export.arxiv.org" in url:
            return 200, ATOM.encode()
        if url.endswith("/graphql"):
            pid = kw["json_body"]["variables"]["id"]
            return 200, json.dumps({"data": {"post": {"result": self.posts.get(pid)}}}).encode()
        if url.endswith("/api/search"):
            return 200, json.dumps([{"hits": self.hits}]).encode()
        if "api.openalex.org" in url:
            for k, w in self.works.items():
                if k in url or k in json.dumps(kw):
                    return 200, json.dumps(w).encode()
            return 404, b""
        return 500, b""


class Budget429(FakeHttp):
    """OpenAlex has spent its daily budget; arXiv answers a title search."""

    def __call__(self, method: str, url: str, kw: Any) -> tuple[int, bytes]:
        if "api.openalex.org" in url:
            self.calls.append((method, url, dict(kw)))
            return 429, b'{"error": "Rate limit exceeded"}'
        return super().__call__(method, url, kw)


def _net(http: FakeHttp) -> sr.Net:
    return sr.Net(http, sleep=lambda s: None)


def _resolver(http: FakeHttp, index: dict[tuple[str, str], str] | None = None,
              store: dict[str, F.Cand] | None = None) -> sr.Resolver:
    return sr.Resolver(_net(http), index or {}, store or {})


# ---------------------------------------------------------------- csv and links
def test_classify_and_unique_links() -> None:
    rows, cols = sr.parse_papers_csv(CSV.encode())
    assert len(rows) == 13 and cols[4] == "link_url"
    got = [sr.classify_link(r["link_url"]) for r in rows]
    assert [k for k, _ in got] == ["arxiv", "arxiv", "lesswrong", "lesswrong", "alignmentforum", "openreview",
                                   "journal", "journal", "lab", "other", "org_site", "none", "arxiv"]
    assert [v for _, v in got[:6]] == ["2501.00001", "2501.00001", "AbCdEf123", "XyZ987", "AfPost1", "or1D"]
    assert got[7][1] == "10.1073/pnas.2500001122" and got[12][1] == "2502.00002"
    assert got[6][1].endswith("nature.com/articles/s41586-025-0001-x") and got[11] == ("none", "")
    links = sr.unique_links(rows)
    first = links[0]
    assert (first.kind, first.key) == ("arxiv", "2501.00001")
    assert [r["agenda_name"] for r in first.rows] == ["Agent foundations", "Tiling"]  # one link, two agendas
    assert len(links) == 12
    assert sr.link_counts(rows)["arxiv"] == 3


# ---------------------------------------------------------------- records
def test_arxiv_record_is_document_text() -> None:
    res, why = _resolver(FakeHttp()).resolve(sr.Link("arxiv", "2501.00001", "u", "Paper One", [{}]))
    assert res is not None and why == ""
    inp = res.input
    assert inp["title"] == "Paper One: tiling agents"
    assert inp["abstract"] == "We study corrigibility of tiling agents under self-modification, with proofs."
    assert inp["excerpt"] == normalize(inp["abstract"])  # minted as the collector mints it
    row = {"metadata": {"title": inp["title"], "abstract": inp["abstract"],
                        "authors": [{"name": "Ann Author"}, {"name": "Bob Writer"}]}}
    assert inp["record"] == document_text(row, inp["excerpt"])
    assert (res.origin, res.channel, res.channel_group, res.license_class) == ("external", "arxiv", "paper", "open")


def test_forum_record_uses_search_body_minted() -> None:
    post = {"_id": "AbCdEf123", "title": "On tiling", "af": True, "user": {"displayName": "U"},
            "coauthors": [{"displayName": "V"}], "contents": {"markdown": "graphql markdown body " * 3}}
    hit = {"_id": "AbCdEf123", "title": " On tiling ", "authorDisplayName": "U", "coauthors": [{"displayName": "V"}],
           "body": BODY}
    http = FakeHttp(posts={"AbCdEf123": post}, hits=[{"_id": "other", "title": "x", "body": "y" * 50}, hit])
    res, _ = _resolver(http).resolve(sr.Link("alignmentforum", "AbCdEf123", "u", "On tiling", [{}]))
    assert res is not None
    want = "First paragraph about embedded agency.\n\nSecond paragraph, trailing space."
    assert res.input["excerpt"] == want and res.input["abstract"] is None
    assert res.input["record"] == document_text({"metadata": {"title": "On tiling", "abstract": None,
                                                              "authors": [{"name": "U"}, {"name": "V"}]}}, want)
    assert res.info["excerpt_origin"] == "lw_search_body" and res.channel == "lesswrong"
    # no search hit: GraphQL markdown, marked as the approximation it is
    res2, _ = _resolver(FakeHttp(posts={"AbCdEf123": post}, hits=[])).resolve(
        sr.Link("lesswrong", "AbCdEf123", "u", "On tiling", [{}]))
    assert res2 is not None and res2.info["excerpt_origin"] == "graphql_markdown"


def test_title_only_dropped_and_replaced_in_stratum() -> None:
    post = {"_id": "P1", "title": "Empty", "user": {"displayName": "U"}, "contents": {"markdown": ""}}
    post2 = {"_id": "P2", "title": "Full", "user": {"displayName": "W"}, "contents": {"markdown": "body " * 20}}
    http = FakeHttp(posts={"P1": post, "P2": post2}, hits=[{"_id": "P1", "title": "Empty", "body": "tiny"}])
    links = [sr.Link("lesswrong", "P1", "u1", "Empty", [{}]), sr.Link("lesswrong", "P2", "u2", "Full", [{}])]
    chosen, rep = sr.sample_recall(links, _resolver(http), "s", {"forum": 1}, {})
    assert [c.link.key for c in chosen] == ["P2"]
    assert rep["lesswrong"]["dropped:title_only"] == 1 and rep["lesswrong"]["resolved"] == 1


# ---------------------------------------------------------------- store overlap
def _cand(doc_id: str, record: str, asks: bool = True) -> F.Cand:
    inp: dict[str, Any] = {"title": "Store title", "authors": [], "abstract": "store abstract", "excerpt": None, "record": record}
    return F.Cand(doc_id=doc_id, order="0", group="paper", channel="openalex", proxy="kw_low", hard=None,
                  eligible=True, item={"license_class": "restricted", "production_asks": asks, "input": inp,
                                       "input_digest": F.sha256_hex(record)})


def test_store_overlap_prefers_store_record() -> None:
    store = {"sDOC": _cand("sDOC", "Title: Store title\n\nAuthors: Unknown"), "sNOB": _cand("sNOB", "x", asks=False)}
    r = _resolver(FakeHttp(), {("arxiv", "2501.00001"): "sDOC", ("arxiv", "2502.00002"): "sNOB"}, store)
    res, _ = r.resolve(sr.Link("arxiv", "2501.00001", "u", "Paper One", [{}]))
    assert res is not None and res.origin == "store" and res.doc_id == "sDOC"
    assert res.input["record"] == "Title: Store title\n\nAuthors: Unknown" and res.license_class == "restricted"
    assert r.checks == [{"link": "arxiv:2501.00001", "doc_id": "sDOC", "record_equal": False}]
    res2, why = r.resolve(sr.Link("arxiv", "2502.00002", "u", "Paper Two", [{}]))
    assert res2 is None and why == "store_title_only"
    # a link found by OpenAlex title search whose work the store holds by DOI
    w = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1234/abc", "title": "Nature: paper!",
         "abstract_inverted_index": {"hello": [0], "world": [1]}, "authorships": []}
    http = FakeHttp(works={"Nature paper": {"results": [w]}})
    r3 = _resolver(http, {("doi", "10.1234/abc"): "sDOC"}, store)
    res3, _ = r3.resolve(sr.Link("org_site", "https://example.org/x", "https://example.org/x", "Nature paper", [{}]))
    assert res3 is not None and res3.origin == "store" and res3.doc_id == "sDOC"
    assert any("title.search:Nature paper" in json.dumps(kw) for _, _, kw in http.calls)
    # the same work absent from the store: an external openalex record
    r4 = _resolver(FakeHttp(works={"Nature paper": {"results": [w]}}), {}, store)
    res4, _ = r4.resolve(sr.Link("org_site", "https://example.org/x", "https://example.org/x", "Nature paper", [{}]))
    assert res4 is not None and res4.origin == "external" and res4.channel == "openalex"
    assert res4.input["abstract"] == "hello world" and res4.license_class == "open"


def test_recall_items_and_gold() -> None:
    rows = [{"agenda_section": "Theory", "agenda_name": "Tiling", "agenda_full_name": "Theory / Tiling"}]
    res, _ = _resolver(FakeHttp()).resolve(sr.Link("arxiv", "2501.00001", "u", "Paper One", rows))
    assert res is not None
    items, gold = sr.recall_items([res])
    it, g = items[0], gold[0]
    assert it["item_id"] == "sr-0001" and it["doc_id"] == "ext:arxiv:2501.00001"
    assert it["stratum"] == {"channel_group": "paper", "channel": "arxiv", "kind": "arxiv",
                             "sr_section": "Theory", "sr_agenda": "Tiling"}
    assert it["input_digest"] == hashlib.sha256(it["input"]["record"].encode()).hexdigest()
    assert g["label"] == {"verdict": "relevant", "fetch": True, "scope": None}
    assert g["labelers"][0]["who"] == "sr2025-inclusion" and g["contested"] is False and g["agreement"] is None


# ---------------------------------------------------------------- amendment
def _heavy(doc_id: str, intent: str) -> sr.HeavyItem:
    inp = sr.record_input("T " + doc_id, ["A"], "abstract text long enough to count", None)
    return sr.HeavyItem({"source": "arxiv", "id": doc_id, "intent": intent, "query": "q", "topic": "t"},
                        "external", doc_id, "paper", "arxiv",
                        {"hard": None, "license_class": "open", "production_asks": True, "input": inp,
                         "input_digest": F.sha256_hex(inp["record"])}, "kw_low")


def _set(tmp: Path) -> tuple[Path, bytes, dict[str, Any]]:
    d = tmp / "set"
    d.mkdir()
    old = [{"item_id": "tri-0001", "doc_id": "d1", "z": 1, "a": "é"}, {"item_id": "tri-0002", "doc_id": "d2"}]
    raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in old).encode()
    (d / "items.jsonl").write_bytes(raw)
    manifest = {"set_id": "s", "items_sha256": hashlib.sha256(raw).hexdigest(), "counts": {"total": 2}}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return d, raw, manifest


def test_append_only_amendment(tmp_path: Path) -> None:
    d, raw, manifest = _set(tmp_path)
    existing = [json.loads(x) for x in raw.decode().splitlines()]
    rows = sr.heavy_items([_heavy("ext:arxiv:1", "likely_in"), _heavy("ext:arxiv:2", "likely_in"),
                           _heavy("ext:arxiv:3", "negative"), _heavy("ext:arxiv:4", "negative")],
                          existing, "tri", "seed")
    assert sorted(r["item_id"] for r in rows) == ["tri-0003", "tri-0004", "tri-0005", "tri-0006"]
    assert sorted(r["split"] for r in rows) == ["dev", "dev", "test", "test"]
    assert all(r["sr_heavy"] is True and r["origin"] == "external" for r in rows)
    amend = sr.append_items(d, rows, {"by": "track E2"})
    new = (d / "items.jsonl").read_bytes()
    assert new[: len(raw)] == raw and new.decode().splitlines()[:2] == raw.decode().splitlines()
    assert len(new.decode().splitlines()) == 6
    m = json.loads((d / "manifest.json").read_text())
    assert {k: m[k] for k in manifest} == manifest  # every existing key kept, unchanged
    assert m["amendments"] == [amend]
    assert amend["items_sha256_before"] == hashlib.sha256(raw).hexdigest()
    assert amend["items_sha256_after"] == hashlib.sha256(new).hexdigest()
    assert amend["added"] == [r["item_id"] for r in rows]


def test_amendment_refuses_collisions(tmp_path: Path) -> None:
    _, raw, _ = _set(tmp_path)
    existing = [json.loads(x) for x in raw.decode().splitlines()]
    with pytest.raises(sr.AmendError, match="already in the set"):
        sr.heavy_items([_heavy("ext:arxiv:9", "negative")], existing, "tri", "seed", first=2)
    with pytest.raises(sr.AmendError, match="already in the set"):
        sr.heavy_items([_heavy("d1", "negative")], existing, "tri", "seed")


def test_openalex_budget_falls_back_to_arxiv_title() -> None:
    http = Budget429()
    r = _resolver(http)
    link = sr.Link("openreview", "or1D", "https://openreview.net/forum?id=or1D", "Paper one: Tiling agents", [{}])
    res, _ = r.resolve(link)
    assert r.openalex_down and res is not None
    assert (res.channel, res.license_class, res.info["resolver"]) == ("arxiv", "open", "arxiv_title")
    assert res.input["title"] == "Paper One: tiling agents" and res.doc_id == "ext:openreview:or1D"
    assert any(kw.get("params", {}).get("search_query") == 'ti:"Paper one Tiling agents"'
               for _, u, kw in http.calls if "arxiv" in u)
    oa = [c for c in http.calls if "openalex" in c[1]]
    assert len(oa) == sr.RETRIES  # retried, then switched; never a silent empty result
    res2, why = r.resolve(sr.Link("journal", "10.1073/pnas.1", "u", "No such title", [{}]))
    assert res2 is None and why == "arxiv_title_not_found"
    assert len([c for c in http.calls if "openalex" in c[1]]) == sr.RETRIES  # not asked again


def test_jsonl_survives_splitlines(tmp_path: Path) -> None:
    rows = [{"item_id": "a", "text": "line sep para\x85nel"}, {"item_id": "b", "text": "é"}]
    raw = sr.jsonl(rows)
    assert [json.loads(x) for x in raw.decode().splitlines()] == rows  # the harness reads with splitlines
    (tmp_path / "x.jsonl").write_bytes(raw)
    assert sr.read_jsonl(tmp_path / "x.jsonl") == rows


def test_sr_title_mismatch_relinks_or_drops() -> None:
    """SR's URL points at another paper (link_text names the intended one): re-resolved by link_text
    through the arXiv title search, else dropped; a scraping-artefact link_text is not a title."""
    rows = [{"link_text": "Paper Two", "title": "429: Too Many Requests"}]
    assert sr.sr_titles(sr.Link("arxiv", "x", "u", "", rows)) == []  # both too short / an artefact
    rows = [{"link_text": "Unable to access: 429", "title": "A real scraped title here"},
            {"link_text": "The editor's title for it", "title": "The wrong scraped title"}]
    assert sr.sr_titles(sr.Link("arxiv", "x", "u", "", rows)) == ["A real scraped title here",
                                                                   "The editor's title for it"]
    assert sr.titles_match("Paper One: tiling agents", "paper one tiling agents")
    assert sr.titles_match("Paper One", "Paper One: tiling agents, a longer subtitle")
    assert not sr.titles_match("Beyond the Binary: Diverse Preferences", "Optimal Quantum Gates")
    assert sr.titles_match("Opus 4.5's Soul Document", "Claude 4.5 Opus' Soul Document")  # a paraphrase
    assert sr.titles_match("Neuron circuits", "Language Model Circuits Are Sparse in the Neuron Basis")
    assert not sr.titles_match("Self-Correction in Thought-Attractors: A Nudge Towards Alignment",
                               "Large Language Models Report Subjective Experience Under Self-Referential Processing")
    link = sr.Link("arxiv", "2502.00002", "https://arxiv.org/abs/2502.00002", "",
                   [{"link_text": "Paper One: tiling agents", "title": "Paper Two"}])  # title scraped from the URL
    r = _resolver(FakeHttp())
    res, _ = r.resolve(link)  # 2502.00002 is "Paper Two"; the title search finds Paper One
    assert res is not None and res.info["resolver"] == "arxiv_link_text"
    assert res.doc_id == "ext:arxiv:2501.00001" and res.input["title"] == "Paper One: tiling agents"
    assert r.mismatches[0]["outcome"] == "relinked" and r.mismatches[0]["resolved_title"] == "Paper Two"
    items, _ = sr.recall_items([res])
    assert items[0]["sr_link"] == "https://arxiv.org/abs/2502.00002" and items[0]["resolver"] == "arxiv_link_text"
    bad = sr.Link("arxiv", "2502.00002", "u", "", [{"link_text": "A title arXiv does not know at all", "title": ""}])
    r2 = _resolver(FakeHttp())
    assert r2.resolve(bad) == (None, "title_mismatch")
    assert r2.mismatches[0]["outcome"] == "dropped:title_mismatch"
