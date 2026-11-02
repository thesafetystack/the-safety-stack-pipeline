"""``collect.arxiv_sweep``'s keyword and scoped-category families (SR scope note,
docs/scope/shallow-review.md (e)).

Pure cases pin the exact arXiv ``search_query`` text of both families (the AI clause only on
``needs_ai_context`` groups and on every category query), the option validation (unknown group,
malformed category, unknown family) and the author allowlist's five added categories. The
end-to-end case drives the stage through ``run_stage`` on a temporary store over
``tests/fakes/http.py`` (no network) with all three families: one source per arXiv id across
families, one coverage row per ``kw:group:<id>`` / ``cat:<c>`` query, every query that returned a
source in ``metadata.matched_by`` (joined to the coverage rows by digest), the arXiv categories in
the ``provider_record`` (the Atom entry), a ``Last, First`` roster twin merged into its ``First Last`` entry,
and run 2 resuming each query from its own watermark.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import time
from typing import Any

import pytest

from stack.build import incomplete_reasons
from stack.checks.verify import run_checks
from stack.collect.base import query_digest
from stack.config import load
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.collect.inband import record_entry
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from tests.fakes.http import FakeHttp

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_collect_{name}_kw_under_test", REPO / "workflows" / "collect" / name / "main.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


arxiv = _load_wf("arxiv_sweep")
AI = ('abs:"language model" OR abs:"artificial agents" OR abs:"AI agents" OR abs:"AI alignment" '
      'OR abs:"superintelligence" OR abs:"AGI"')


def _cfg(ai_context: list[str] | None = None) -> Any:
    cfg = load(CONFIG_DIR)
    cfg.channels.ai_context = ["language model"] if ai_context is None else ai_context
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# (A) query construction
# ─────────────────────────────────────────────────────────────────────────────
def test_ai_clause_is_ai_context_plus_agent_phrases_deduplicated() -> None:
    assert arxiv.ai_clause(["language model", "ai alignment"]) == (
        'abs:"language model" OR abs:"ai alignment" OR abs:"artificial agents" OR '
        'abs:"AI agents" OR abs:"superintelligence" OR abs:"AGI"')


def test_keyword_query_titles_and_abstracts_each_phrase() -> None:
    """Each phrase in title or abstract, OR'd; a phrase with a double quote is dropped (it would
    break out of the quoting)."""
    assert arxiv.keyword_query(["AI safety", 'say "hi"', "AI  alignment"], None) == (
        '(ti:"AI safety" OR abs:"AI safety" OR ti:"AI alignment" OR abs:"AI alignment")')
    assert arxiv.keyword_query(['"x"'], None) == ""


def test_keyword_plan_ands_the_ai_clause_only_on_needs_ai_context_groups() -> None:
    """`steerability` and `decision-theory` are another field's words without AI; `umbrella` and
    `decision-theory-af` are not, and go out as bare phrases (no category allowlist either)."""
    plan = {p["query"]: p for p in arxiv.plan_keyword_queries(
        _cfg(), ["umbrella", "steerability", "decision-theory", "decision-theory-af", "umbrella"])}
    assert list(plan) == ["kw:group:umbrella", "kw:group:steerability",
                          "kw:group:decision-theory", "kw:group:decision-theory-af"]
    assert {p["family"] for p in plan.values()} == {"kw"}
    assert plan["kw:group:steerability"]["search"] == (
        f'(ti:"steerability" OR abs:"steerability") AND ({AI})')
    assert plan["kw:group:decision-theory"]["search"] == (
        "(ti:\"decision theory\" OR abs:\"decision theory\" OR ti:\"Newcomb's problem\" OR "
        f"abs:\"Newcomb's problem\") AND ({AI})")
    for q in ("kw:group:umbrella", "kw:group:decision-theory-af"):
        assert " AND " not in plan[q]["search"] and "cat:" not in plan[q]["search"], q
    assert plan["kw:group:umbrella"]["search"].startswith(
        '(ti:"AI safety" OR abs:"AI safety" OR ti:"AI alignment" OR abs:"AI alignment"')


def test_keyword_plan_refuses_an_unknown_group() -> None:
    with pytest.raises(ValueError, match="keyword_groups"):
        arxiv.plan_keyword_queries(_cfg(), ["umbrella", "steerabilty"])


def test_category_plan_is_category_and_ai_clause() -> None:
    plan = arxiv.plan_category_queries(_cfg(), ["cs.GT", "q-bio.NC"])
    assert plan == [
        {"family": "cat", "query": "cat:cs.GT", "search": f"cat:cs.GT AND ({AI})", "label": "cs.GT"},
        {"family": "cat", "query": "cat:q-bio.NC", "search": f"cat:q-bio.NC AND ({AI})",
         "label": "q-bio.NC"}]
    with pytest.raises(ValueError, match="scoped_categories"):
        arxiv.plan_category_queries(_cfg(), ["cs.GT OR cat:physics"])


def test_scoped_categories_default_and_author_allowlist() -> None:
    """The author sweep's allowlist gained the five scoped categories (rostered authors' game
    theory, logic and decision theory); physics stays out (namesakes)."""
    assert arxiv.DEFAULT_SCOPED_CATEGORIES == ("cs.GT", "cs.LO", "math.LO", "econ.TH", "q-bio.NC")
    assert arxiv.ARXIV_CATEGORIES == ("cs.AI", "cs.LG", "cs.CL", "cs.CR", "cs.CY", "cs.MA",
                                      "stat.ML", "cs.GT", "cs.LO", "math.LO", "econ.TH", "q-bio.NC")
    assert arxiv.DEFAULT_KEYWORD_GROUPS == ("umbrella", "steerability")


def test_category_keep_regex_covers_umbrella_and_the_sr_groups() -> None:
    """C6 (collection-yield.md §9.3): the keep phrases are the `terms` of umbrella and the 20
    SR-scope groups; an unknown group id fails loudly."""
    cfg = load(CONFIG_DIR)
    assert len(arxiv.CATEGORY_KEEP_GROUPS) == 21 and arxiv.CATEGORY_KEEP_GROUPS[0] == "umbrella"
    r = arxiv.category_keep_regex(cfg)
    for text in ("a survey of AI  alignment", "Newcomb's problem for LLM agents",
                 "updateless decision theory", "Scientist AI"):
        assert r.search(text), text
    assert not r.search("game-theoretic pricing") and not r.search("ai safetyx")
    with pytest.raises(ValueError, match="CATEGORY_KEEP_GROUPS"):
        arxiv.category_keep_regex(cfg, ("umbrella", "no-such-group"))


@pytest.mark.parametrize(("title", "abstract", "kept"), [
    ("Auctions with LLM bidders", "We study revenue with language model bidders.", False),
    ("Auctions with LLM bidders", "Robust to adversarial bidders.", True),           # stem
    # a needs_ai_context group's phrase keeps a hit too: the cat query already ANDed the AI clause,
    # so such a hit is exactly a kw-family hit of that group (game-theory-ai)
    ("Mechanism design for LLM auctions", "Revenue with language model bidders.", True),
    ("Bayesian persuasion", "Implications for AI alignment of language model agents.", True),
    ("Logic of provability", "A Löbian obstacle for tiling agents.", True),          # SR phrase
    ("fMRI decoding with transformers", "A language model reads brain signals.", False),
    ("Oversight games", "", True),                                                   # stem, title
])
def test_keep_category_hit_needs_a_safety_stem_or_a_keep_phrase(
        title: str, abstract: str, kept: bool) -> None:
    r = arxiv.category_keep_regex(load(CONFIG_DIR))
    assert arxiv.keep_category_hit({"title": title, "abstract": abstract}, r) is kept


# ─────────────────────────────────────────────────────────────────────────────
# (B) end to end: three families, one store
# ─────────────────────────────────────────────────────────────────────────────
ARXIV_API = "https://export.arxiv.org/api/query"
P1 = {"id": "2609.00011", "title": "Debate as oversight", "published": "2026-09-02",
      "authors": ["Ethan Perez"], "summary": "Debate between language models as AI safety."}
K1 = {"id": "2609.00012", "title": "An AI alignment survey", "published": "2026-09-03",
      "authors": ["Ann Other"], "summary": "We survey AI alignment."}
K2 = {"id": "2609.00013", "title": "Steerability of agents", "published": "2026-09-04",
      "authors": ["Bea Other"], "summary": "Steerability of AI agents in games.",
      "categories": ["cs.GT", "cs.AI"]}


def _atom(entries: list[dict[str, Any]]) -> str:
    parts = ['<?xml version="1.0"?>',
             ('<feed xmlns="http://www.w3.org/2005/Atom" '
              'xmlns:arxiv="http://arxiv.org/schemas/atom">')]
    for e in entries:
        authors = "".join(f"<author><name>{a}</name></author>" for a in e["authors"])
        cats = e.get("categories") or []
        tags = (f'<arxiv:primary_category term="{cats[0]}"/>' if cats else "") + "".join(
            f'<category term="{c}" scheme="http://arxiv.org/schemas/atom"/>' for c in cats[::-1])
        parts.append(f"<entry><id>http://arxiv.org/abs/{e['id']}v1</id><title>{e['title']}</title>"
                     f"<summary>{e['summary']}</summary>"
                     f"<published>{e['published']}T00:00:00Z</published>{authors}{tags}</entry>")
    return "".join([*parts, "</feed>"])


def _q(search: str, frm: str, n: int = 2) -> dict[str, Any]:
    """The params ``fetch_query`` sends (same order, so FakeHttp's URL key matches)."""
    return {"search_query": f"{search} AND submittedDate:[{frm}0000 TO {arxiv.ARXIV_DATE_UPPER}]",
            "start": 0, "max_results": n, "sortBy": "submittedDate", "sortOrder": "ascending"}


def _plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.arxiv_sweep", "prior": ["sources", "coverage"], "options": options}]})


def _view(store: pathlib.Path, chain_id: str) -> list[dict[str, Any]]:
    return read_view(resolve(store, sdir(chain_id))).to_pylist()


@pytest.mark.slow  # real store + runner
def test_three_families_end_to_end(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM"):
        monkeypatch.delenv(var, raising=False)
    roster = tmp_path / "roster.yaml"
    # the `Last, First` twin is the same researcher: one query (D229 roster normalisation)
    roster.write_text('names:\n  - "Ethan Perez"\n  - "Perez, Ethan"\n', encoding="utf-8")
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date()
    cfg = load(CONFIG_DIR)
    kw = {p["query"]: p["search"] for p in arxiv.plan_keyword_queries(
        cfg, list(arxiv.DEFAULT_KEYWORD_GROUPS))}
    cat = f"cat:cs.GT AND ({arxiv.ai_clause(list(cfg.channels.ai_context))})"
    author = f"au:Perez_Ethan AND {arxiv._CAT_CLAUSE}"
    fake = FakeHttp()
    fake.add(ARXIV_API, _q(author, "20260901"), body=_atom([P1]))
    fake.add(ARXIV_API, _q(kw["kw:group:umbrella"], "20260901"), body=_atom([P1, K1]))  # cap 2
    fake.add(ARXIV_API, _q(kw["kw:group:umbrella"], "20260903"), body=_atom([K1]))
    fake.add(ARXIV_API, _q(kw["kw:group:steerability"], "20260901"), body=_atom([K2]))
    fake.add(ARXIV_API, _q(cat, "20260901"), body=_atom([K2]))  # the same paper, another family
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    opts = {"roster": str(roster), "from": "2026-09-01", "max_records": 2,
            "scoped_categories": ["cs.GT"]}  # families and keyword_groups: the defaults

    rec1 = run_line(store, _plan(**opts), "collect.arxiv_sweep")
    assert rec1.status == "ok", rec1.error
    assert len(fake.calls) == 4
    # D471 (S25-58): the capped umbrella query is not caught up, so the run is not scope complete
    # and a build names the reason `limit`
    assert (rec1.scope_complete, rec1.counts.get("queries_capped"), rec1.counts["caught_up"]) == (False, 1, 3)
    assert incomplete_reasons(rec1.counts) == ["limit"]
    assert rec1.counts["minted_ids"] == 3 and rec1.counts["sources_repeat"] == 2
    assert {k: rec1.counts[f"queries_{k}"] for k in ("author", "kw", "cat")} == {
        "author": 1, "kw": 2, "cat": 1}
    src1 = {r["identity"]["external_id"]: r for r in _view(store, "sources.arxiv_sweep")}
    assert sorted(src1) == ["2609.00011", "2609.00012", "2609.00013"]
    assert {r["identity"]["channel"] for r in src1.values()} == {"arxiv"}
    assert src1["2609.00013"]["identity"]["keys"] == [
        {"kind": "doi", "value": "10.48550/arxiv.2609.00013"},
        {"kind": "arxiv", "value": "2609.00013"}]
    cov1 = {r["query"]: r for r in _view(store, "coverage.arxiv_sweep")}
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    assert set(cov1) == {"Perez_Ethan", "kw:group:umbrella", "kw:group:steerability", "cat:cs.GT"}
    um = cov1["kw:group:umbrella"]
    # the cap-truncated ascending kw pass banks [from, newest fetched] (D-SWEEP1), as the author
    # family does; P1 was observed by the author query first, so it is not new to the kw query
    assert (um["status"], um["window_start_at"], um["window_end_at"], um["seen"], um["kept"]) == (
        "ok", d("2026-09-01"), d("2026-09-03"), 2, 1)
    for q in ("kw:group:steerability", "cat:cs.GT", "Perez_Ethan"):
        assert (cov1[q]["status"], cov1[q]["window_end_at"]) == ("ok", d(today.isoformat())), q
    assert (cov1["cat:cs.GT"]["seen"], cov1["cat:cs.GT"]["kept"]) == (1, 0)
    # attribution (D228): every query that returned the source, sorted by query_id, each joined to
    # its coverage row by digest; metadata.query_digest is the first query's (run order)
    m = lambda qid, q, lab: {"query_id": qid, "query_digest": query_digest(q), "label": lab}
    assert src1["2609.00011"]["metadata"]["matched_by"] == [
        m("au:Perez_Ethan", "Perez_Ethan", "Ethan Perez"),
        m("kw:group:umbrella", "kw:group:umbrella", "umbrella")]
    assert src1["2609.00011"]["metadata"]["query_digest"] == query_digest("Perez_Ethan")
    assert src1["2609.00012"]["metadata"]["matched_by"] == [
        m("kw:group:umbrella", "kw:group:umbrella", "umbrella")]
    assert src1["2609.00013"]["metadata"]["matched_by"] == [
        m("cat:cs.GT", "cat:cs.GT", "cs.GT"),
        m("kw:group:steerability", "kw:group:steerability", "steerability")]
    assert src1["2609.00013"]["metadata"]["query_digest"] == query_digest("kw:group:steerability")
    cov_digests = {r["query_digest"] for r in cov1.values()}
    assert {e["query_digest"] for r in src1.values() for e in r["metadata"]["matched_by"]} \
        <= cov_digests
    # the provider_record (D522): the Atom <entry> as received, with its categories
    rec_e = record_entry(src1["2609.00013"])
    assert rec_e is not None and (rec_e["raw"]["media_type"], rec_e["text"]) == ("application/atom+xml", None)
    atom = open_blobs(store).get(rec_e["raw"]["sha256"]).decode()
    assert rec_e["raw"]["bytes"] == len(atom.encode())
    assert atom.startswith("<entry") and atom.endswith("</entry>") and "2609.00013" in atom
    assert 'term="cs.GT"' in atom and 'term="cs.AI"' in atom

    # run 2: complete queries are covered through today (no request); umbrella resumes: its tail
    # asks from the watermark − 4 days (D405: the floor, 09-01), the cap fills inside that
    # look-back, so it is asked again from its 09-03 watermark
    n = len(fake.calls)
    rec2 = run_line(store, _plan(**opts), "collect.arxiv_sweep")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["minted_ids"] == 0 and rec2.counts["already_covered"] == 3
    assert [c.url for c in fake.calls[n:]] == [fake.urls()[1], fake.urls()[-1]]
    assert "submittedDate%3A%5B202609010000" in fake.calls[n].url
    assert "submittedDate%3A%5B202609030000" in fake.calls[-1].url
    assert rec2.counts["lookback_capped"] == 1
    # the cap filled inside the look-back only: the re-ask from the watermark caught up (D471)
    assert (rec2.scope_complete, rec2.counts["queries_capped"]) == (True, 0)
    results = {r.check: r for r in run_checks(store)}
    assert {k: r.items for k, r in results.items() if r.status in ("failed", "warn")} == {}


def test_unknown_family_fails_the_run(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("stack.collect.base.urllib_send", FakeHttp())
    rec = run_line(tmp_path / "store", _plan(families=["kw", "authors"]), "collect.arxiv_sweep")
    assert rec.status != "ok" and "families" in str(rec.error)


def test_roster_names_sharing_a_token_are_one_query(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """``Adam Jermyn`` and ``Adam S. Jermyn`` both give ``au:Jermyn_Adam``: one request, one
    coverage window (two windows on one key in a run is a harness error), the hit matched by
    either name and attributed to both (D229)."""
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM"):
        monkeypatch.delenv(var, raising=False)
    roster = tmp_path / "roster.yaml"
    roster.write_text('names:\n  - "Adam Jermyn"\n  - "Adam S. Jermyn"\n', encoding="utf-8")
    paper = {"id": "2609.00021", "title": "Toy models", "published": "2026-09-02",
             "authors": ["Adam S. Jermyn"], "summary": "Superposition in toy models."}
    fake = FakeHttp()
    fake.add(ARXIV_API, _q(f"au:Jermyn_Adam AND {arxiv._CAT_CLAUSE}", "20260901"), body=_atom([paper]))
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    store = tmp_path / "store"
    rec = run_line(store, _plan(roster=str(roster), **{"from": "2026-09-01"}, max_records=2,
                                 families=["author"]), "collect.arxiv_sweep")
    assert rec.status == "ok", rec.error
    assert len(fake.calls) == 1 and rec.counts["queries_author"] == 1
    (row,) = _view(store, "sources.arxiv_sweep")
    assert row["metadata"]["matched_by"] == [{"query_id": "au:Jermyn_Adam",
                                              "query_digest": query_digest("Jermyn_Adam"),
                                              "label": "Adam Jermyn | Adam S. Jermyn"}]


G1 = {"id": "2609.00014", "title": "Revenue in auctions with LLM bidders", "published": "2026-09-05",
      "authors": ["Cy Other"], "summary": "Pricing when the bidders are AI agents.",
      "categories": ["cs.GT"]}


def test_category_family_drops_hits_without_a_stem_or_keep_phrase(
        tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """C6 end to end: of two cat hits only the one with a keep phrase is a source; the dropped one
    is counted `cat_dropped` and is not in the window's `seen`; the window still covers through
    today (the filter does not touch paging)."""
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM"):
        monkeypatch.delenv(var, raising=False)
    store = tmp_path / "store"
    cfg = load(CONFIG_DIR)
    cat = f"cat:cs.GT AND ({arxiv.ai_clause(list(cfg.channels.ai_context))})"
    fake = FakeHttp()
    fake.add(ARXIV_API, _q(cat, "20260901", 10), body=_atom([K2, G1]))
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    rec = run_line(store, _plan(families=["cat"], scoped_categories=["cs.GT"], max_records=10,
                                 **{"from": "2026-09-01"}), "collect.arxiv_sweep")
    assert rec.status == "ok", rec.error
    assert (rec.counts["cat_kept"], rec.counts["cat_dropped"], rec.counts["hits_fetched"]) == (1, 1, 2)
    assert [r["identity"]["external_id"] for r in _view(store, "sources.arxiv_sweep")] == [K2["id"]]
    (cov,) = _view(store, "coverage.arxiv_sweep")
    today = dt.datetime.now(dt.UTC).date()
    assert (cov["status"], cov["seen"], cov["window_end_at"].date()) == ("ok", 1, today)


@pytest.mark.slow  # real store + runner
def test_floor_moved_back_sweeps_only_the_gap(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """B7a: a first window from 2025-10-01, then `from: 2024-11-30`: the second run asks only the
    hole [2024-11-30, 2025-10-01] (bounded above), not the covered range again; a third run asks
    nothing."""
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM"):
        monkeypatch.delenv(var, raising=False)
    store = tmp_path / "store"
    kw = {p["query"]: p["search"] for p in arxiv.plan_keyword_queries(load(CONFIG_DIR), ["umbrella"])}
    search = kw["kw:group:umbrella"]
    old = {"id": "2412.00001", "title": "An AI safety case", "published": "2024-12-02",
           "authors": ["Old Author"], "summary": "AI safety."}
    gap = {"search_query": f"{search} AND submittedDate:[202411300000 TO 202510010000]",
           "start": 0, "max_results": 10, "sortBy": "submittedDate", "sortOrder": "ascending"}
    fake = FakeHttp()
    fake.add(ARXIV_API, _q(search, "20251001", 10), body=_atom([K1]))
    fake.add(ARXIV_API, gap, body=_atom([old]))
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    opts = {"families": ["kw"], "keyword_groups": ["umbrella"], "max_records": 10}
    rec1 = run_line(store, _plan(**opts, **{"from": "2025-10-01"}), "collect.arxiv_sweep")
    assert rec1.status == "ok", rec1.error
    assert len(fake.calls) == 1
    n = len(fake.calls)
    rec2 = run_line(store, _plan(**opts, **{"from": "2024-11-30"}), "collect.arxiv_sweep")
    assert rec2.status == "ok", rec2.error
    assert [c.url for c in fake.calls[n:]] == [fake.urls()[1]]  # the gap only
    assert rec2.counts["holes"] == 1 and rec2.counts["minted_ids"] == 1
    today = dt.datetime.now(dt.UTC).date()
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    cov = sorted((r["window_start_at"], r["window_end_at"], r["status"])
                 for r in _view(store, "coverage.arxiv_sweep"))
    assert cov == [(d("2024-11-30"), d("2025-10-01"), "ok"),
                   (d("2025-10-01"), d(today.isoformat()), "ok")]
    n = len(fake.calls)
    rec3 = run_line(store, _plan(**opts, **{"from": "2024-11-30"}), "collect.arxiv_sweep")
    assert rec3.status == "ok" and len(fake.calls) == n and rec3.counts["already_covered"] == 1
