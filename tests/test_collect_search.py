"""Tests for the two collect search sweeps — ``collect.arxiv_sweep`` and ``collect.forums``
(ports of v1 ``search_arxiv.py`` / ``search_forums.py``).

Pure-unit tests cover query construction (arXiv ``Lastname_Firstname`` + category filter; LW
term/tag query shapes + coverage topic_keys), the ``names_match`` tightening, roster loading, the
arXiv page loop's receipts and its one retry, and the D-SWEEP1 window accounting.

The arXiv end-to-end case drives ``collect.arxiv_sweep`` through ``stack.stage_runner.run_stage``
on a temporary store over ``tests/fakes/http.py`` (installed at the harness's default transport
seam, ``stack.collect.base.urllib_send``; no network): run 1 writes ``sources.arxiv_sweep`` and
``coverage.arxiv_sweep`` (rows, typed keys, in-band abstract, a cap-truncated ascending pass
banked as the prefix it covered, a failed query not advancing), run 2 over the same fake mints no
new ``source_id`` and resumes each query from its watermark, and ``run_checks`` passes (V1/V2).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import time
from typing import Any

import pyarrow.parquet as pq
import pytest
import yaml

from stack.checks.verify import run_checks
from stack.config import load
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from stack.store.parquet import read_header
from tests.fakes.http import FakeHttp
from stack.collect.inband import record_entry
from tests.synth.rows import content_entries

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"
WORKFLOWS_DIR = REPO / "workflows"


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_collect_{name}_under_test", WORKFLOWS_DIR / "collect" / name / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


arxiv = _load_wf("arxiv_sweep")
forums = _load_wf("forums")


# ─────────────────────────────────────────────────────────────────────────────
# canned-response builders
# ─────────────────────────────────────────────────────────────────────────────
def _atom(entries: list[dict[str, Any]]) -> str:
    """Build a minimal arXiv Atom feed from ``[{id, title, published, authors, summary}]``."""
    parts = ['<?xml version="1.0"?>', '<feed xmlns="http://www.w3.org/2005/Atom">']
    for e in entries:
        authors = "".join(f"<author><name>{a}</name></author>" for a in e["authors"])
        parts.append(
            "<entry>"
            f"<id>http://arxiv.org/abs/{e['id']}v1</id>"
            f"<title>{e['title']}</title>"
            f"<summary>{e.get('summary', 'abstract')}</summary>"
            f"<published>{e['published']}T00:00:00Z</published>"
            f"{authors}"
            "</entry>"
        )
    parts.append("</feed>")
    return "".join(parts)


def _fake_arxiv_get(feed_entries: list[dict[str, Any]]) -> Any:
    """A ``http_get`` stand-in that paginates ``feed_entries`` by the ``start``/``max_results``
    query params (mirrors the real Atom API windowing)."""

    def fake(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, str]:
        start = int(params["start"])
        n = int(params["max_results"])
        return 200, _atom(feed_entries[start : start + n])

    return fake


# ─────────────────────────────────────────────────────────────────────────────
# (A) pure-unit: arXiv query construction + name matching
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name, expect",
    [
        ("Ethan Perez", "Perez_Ethan"),
        ("Jan Leike", "Leike_Jan"),
        ("A. Feder Cooper", "Cooper_A."),          # multi-token: last_first
        ("Aakanksha", "Aakanksha"),                # single token passes through
        ("Hilton, Benjamin", "Hilton_Benjamin"),   # `Last, First` read in order (D229)
        ("van der Weij, Teun", "Weij_Teun"),       # as its `Teun van der Weij` twin
    ],
)
def test_arxiv_author_query(name: str, expect: str) -> None:
    assert arxiv.arxiv_author_query(name) == expect


@pytest.mark.parametrize(
    "target, author, expect",
    [
        ("Ethan Perez", "Ethan Perez", True),
        ("Ethan Perez", "E. Perez", True),          # initial-listed keeps recall
        ("Ethan Perez", "Estefania Perez", False),  # surname+initial collision dropped
        ("Ethan Perez", "Ethan Caballero", False),  # different surname
        ("Jan Leike", "J. Leike", True),
        ("Nanda", "Neel Nanda", False),             # bare-surname target: needs the full first name
        ("Hilton, Benjamin", "Benjamin Hilton", True),  # inverted roster entry (D229)
        ("Hilton, Benjamin", "B. Hilton", True),
        ("Benjamin Hilton", "Hilton, Benjamin", True),  # inverted author form
        ("Hilton, Benjamin", "Jacob Hilton", False),
        ("John Smith, Jr.", "John Smith, Jr.", True),   # a suffix is not an inversion
    ],
)
def test_names_match(target: str, author: str, expect: bool) -> None:
    assert arxiv.names_match(target, author) is expect


def test_arxiv_query_carries_category_filter(monkeypatch: Any) -> None:
    """The submitted ``search_query`` ANDs the ``au:`` token with the full CS/ML category clause
    and the submittedDate window."""
    captured: dict[str, Any] = {}

    def spy(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, str]:
        captured.update(params)
        return 200, _atom([])

    monkeypatch.setattr(arxiv, "http_get", spy)
    monkeypatch.setattr(arxiv, "_sleep", lambda s: None)
    arxiv.fetch_author("Perez_Ethan", "202001010000", 100)
    q = captured["search_query"]
    assert "au:Perez_Ethan" in q
    for cat in arxiv.ARXIV_CATEGORIES:
        assert f"cat:{cat}" in q
    assert "submittedDate:[202001010000 TO 99991231235959]" in q


def test_arxiv_fetch_exhausted_states(monkeypatch: Any) -> None:
    monkeypatch.setattr(arxiv, "_sleep", lambda s: None)
    entries = [
        {"id": f"2401.0000{i}", "title": f"P{i}", "published": "2024-01-01", "authors": ["X Y"]}
        for i in range(5)
    ]
    # complete: cap far above the 5 available
    monkeypatch.setattr(arxiv, "http_get", _fake_arxiv_get(entries))
    got, ex = arxiv.fetch_author("X_Y", "202001010000", 100)
    assert ex == "complete" and len(got) == 5
    # truncated: cap below availability
    got, ex = arxiv.fetch_author("X_Y", "202001010000", 2)
    assert ex == "truncated" and len(got) == 2
    # complete zero-yield: empty feed
    monkeypatch.setattr(arxiv, "http_get", _fake_arxiv_get([]))
    got, ex = arxiv.fetch_author("Nobody_X", "202001010000", 100)
    assert ex == "complete" and got == []
    # error: transport failure
    monkeypatch.setattr(arxiv, "http_get", lambda u, p, *, timeout=60.0: (0, ""))
    got, ex = arxiv.fetch_author("X_Y", "202001010000", 100)
    assert ex == "error"


def test_arxiv_fetch_retries_a_throttled_page_once(monkeypatch: Any) -> None:
    """A failed page (throttled/garbled) gets ONE retry after RETRY_DELAY_S: fail-then-succeed
    completes the query and tallies the retry; fail-twice banks 'error'. The retry sleep replaces
    the normal inter-page delay for that attempt (D-SWEEP arXiv throttling, ~32% page-error rate)."""
    delays: list[float] = []
    monkeypatch.setattr(arxiv, "_sleep", lambda s: delays.append(s))
    entries = [
        {"id": f"2401.0000{i}", "title": f"P{i}", "published": "2024-01-01", "authors": ["X Y"]}
        for i in range(3)
    ]

    # fail-once-then-succeed → the single retry recovers the page and the query completes
    state = {"n": 0}

    def flaky(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, str]:
        state["n"] += 1
        if state["n"] == 1:
            return 503, ""            # first attempt fails
        return 200, _atom(entries)    # retry (and the empty trailing page) succeed

    monkeypatch.setattr(arxiv, "http_get", flaky)
    stats: dict[str, int] = {"retries": 0}
    got, ex = arxiv.fetch_author("X_Y", "202001010000", 100, stats=stats)
    assert ex == "complete" and len(got) == 3
    assert stats["retries"] == 1
    assert arxiv.RETRY_DELAY_S in delays  # the longer cool-off was used for the retry

    # fail-twice on the same page → error, still exactly one retry counted
    monkeypatch.setattr(arxiv, "http_get", lambda u, p, *, timeout=60.0: (503, ""))
    stats2: dict[str, int] = {"retries": 0}
    got2, ex2 = arxiv.fetch_author("X_Y", "202001010000", 100, stats=stats2)
    assert ex2 == "error" and got2 == []
    assert stats2["retries"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (B) pure-unit: forums plan + roster loading
# ─────────────────────────────────────────────────────────────────────────────
def test_forums_plan_queries_shape() -> None:
    cfg = load(CONFIG_DIR)
    chan = cfg.channels.channel("lesswrong")
    plan = forums.plan_queries(cfg, chan, ("ai-control", "rlhf"))
    terms = [p for p in plan if p["kind"] == "term"]
    tags = [p for p in plan if p["kind"] == "tag"]
    # concept-group TERM queries first, each carrying group:/technique: topic keys. The two
    # curated forum-native keywords (EXTRA_KEYWORDS) join no group, so they carry none — and
    # must not be given a made-up one, since coverage topic_keys feed the taxonomy join.
    group_terms = [p for p in terms if p["term"] not in forums.EXTRA_KEYWORDS]
    extra_terms = [p for p in terms if p["term"] in forums.EXTRA_KEYWORDS]
    assert group_terms and all(p["topic_keys"][0].startswith("group:") for p in group_terms)
    assert len(extra_terms) == len(forums.EXTRA_KEYWORDS)
    assert all(p["topic_keys"] == [] for p in extra_terms)
    rlhf = next(p for p in terms if p["term"] == "RLHF")
    # the group's FULL taxonomy join, post-taxonomy-port: one group key + every mapped technique
    assert rlhf["topic_keys"] == ["group:rlhf", "technique:rlhf", "technique:rlaif"]
    # TAG queries after terms, no concept topic; label form is tag:<slug>.
    assert [p["tag"] for p in tags] == ["ai-control", "rlhf"]
    assert all(p["topic_keys"] == [] for p in tags)
    assert forums._query_label(rlhf) == "kw:RLHF"
    assert forums._query_label(tags[0]) == "tag:ai-control"
    # deterministic ordering: all terms precede all tags.
    kinds = [p["kind"] for p in plan]
    assert kinds == ["term"] * len(terms) + ["tag"] * len(tags)


def test_forums_groups_option_narrows_terms_and_drops_extra_keywords() -> None:
    """The `groups` option: only those groups' terms (as they are, also `needs_ai_context`
    ones: LW/AF are AI venues), no extra keywords, tags still appended; unknown id refused."""
    cfg = load(CONFIG_DIR)
    chan = cfg.channels.channel("lesswrong")
    plan = forums.plan_queries(cfg, chan, ("ai-control",), ["steerability", "decision-theory"])
    assert [(p["kind"], p["term"] or p["tag"]) for p in plan] == [
        ("term", "decision theory"), ("term", "Newcomb's problem"), ("term", "steerability"),
        ("tag", "ai-control")]
    assert plan[0]["topic_keys"] == ["group:decision-theory"]
    with pytest.raises(ValueError, match="groups"):
        forums.plan_queries(cfg, chan, (), ["steerability", "nope"])


def test_roster_loads_from_repo() -> None:
    cfg = load(CONFIG_DIR)
    names = arxiv.load_roster(cfg)
    assert names == sorted(set(names))          # deterministic, de-duplicated, sorted
    assert len(names) > 100                       # the v1-derived roster
    assert all(isinstance(n, str) and n for n in names)


def _person(entry: str) -> tuple[str, str]:
    """A roster entry's researcher as an arXiv author list writes them (``First Last``) and
    inverted (``Last, First``); a ``Last, First`` entry is turned round first."""
    last, sep, first = entry.partition(",")
    nat = f"{first.strip()} {last.strip()}" if sep else entry
    parts = nat.split()
    return nat, f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else nat


def test_every_roster_entry_collects_its_researcher() -> None:
    """Over the whole roster file (D229, docs/design/collection-yield.md §3.2): every raw entry
    yields an ``au:Last_First`` query, ``names_match`` accepts a synthetic author list holding the
    researcher in either order and rejects an unrelated one; no entry is a username or a fragment
    (the one single-name entry is a known mononym); the plan has one query per token."""
    raw = yaml.safe_load((CONFIG_DIR / "rosters" / "researchers.yaml").read_text())["names"]
    mononyms = {"Aakanksha"}
    bad = []
    for item in raw:
        entry = item["name"] if isinstance(item, dict) else item  # {name, openalex_ids} (C4)
        nat, inverted = _person(entry)
        parts = nat.split()
        if len(parts) < 2:
            ok = entry in mononyms and arxiv.author_in_list(["Ann Other", nat], entry)
        else:
            ok = (arxiv.arxiv_author_query(entry) == f"{parts[-1]}_{parts[0]}"
                  and arxiv.author_in_list(["Ann Other", nat, "Bo Third"], entry)
                  and arxiv.author_in_list(["Ann Other", inverted], entry)
                  and not arxiv.author_in_list(["Ann Other", "Bo Third"], entry))
        if not ok or "_" in entry:
            bad.append(entry)
    assert bad == []
    names = arxiv.load_roster(load(CONFIG_DIR))
    plan = arxiv.plan_author_queries(names)
    assert len({q["query"] for q in plan}) == len(plan)  # one window key per query and run
    jermyn = next(q for q in plan if q["query"] == "Jermyn_Adam")
    assert jermyn["names"] == ["Adam Jermyn", "Adam S. Jermyn"] and jermyn["query_id"] == "au:Jermyn_Adam"
    for added in ("Vanessa Kosoy", "Caspar Oesterheld", "Vincent Conitzer", "Daniel Murfet",
                  "Adam Shai", "Stuart Armstrong", "Benjamin Hilton"):
        assert added in names, added


def test_roster_loads_custom_file(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(
        'version: "1.0.0"\nnames:\n  - "Zeta Author"\n  - "Alpha Author"\n  - "Alpha Author"\n'
        '  - "Author, Zeta"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("STACK_ARXIV_ROSTER", str(p))
    cfg = load(CONFIG_DIR)
    assert arxiv.load_roster(cfg) == ["Alpha Author", "Zeta Author"]  # `Last, First` twin merged


# ─────────────────────────────────────────────────────────────────────────────
# (E) D-SWEEP1 — window accounting units + the breadth-preset depth reader
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exhausted,dates,expect",
    [
        # complete: covered through today, whatever was fetched
        ("complete", ["2024-01-15"], ("2026-07-21", "complete")),
        ("complete", [], ("2026-07-21", "complete")),
        # capped ascending pull: bank the contiguous prefix, mark it exhausted for THAT window
        ("truncated", ["2021-03-01", "2022-06-30"], ("2022-06-30", "complete")),
        # unsplittable / no-progress cases: stall honestly on the floor
        ("truncated", ["2020-01-01"], ("2020-01-01", "truncated")),
        ("truncated", ["2019-05-05"], ("2020-01-01", "truncated")),
        ("truncated", [], ("2020-01-01", "truncated")),
        # a future date from the feed is clamped to today, never trusted forward
        ("truncated", ["2099-01-01"], ("2026-07-21", "complete")),
        # an errored pull claims nothing
        ("error", ["2024-01-15"], ("2020-01-01", "error")),
    ],
)
def test_covered_window_accounting(
    monkeypatch: Any, exhausted: str, dates: list[str], expect: tuple[str, str]
) -> None:
    monkeypatch.setattr(arxiv, "_as_of_date", lambda: "2026-07-21")
    entries = [{"pub_date": d} for d in dates]
    assert arxiv.covered_window(entries, exhausted, "2020-01-01") == expect


def test_covered_window_ignores_undated_entries(monkeypatch: Any) -> None:
    """Entries without a parsed date cannot bound a window; they must not drag the frontier."""
    monkeypatch.setattr(arxiv, "_as_of_date", lambda: "2026-07-21")
    entries = [{"pub_date": None}, {"pub_date": "2023-04-01"}, {}]
    assert arxiv.covered_window(entries, "truncated", "2020-01-01") == ("2023-04-01", "complete")


def test_breadth_preset_is_the_configured_sweep_depth() -> None:
    """channels.yaml's breadth preset is what the sweeps actually run at — it was committed but
    unread for the whole of v3.0, so every sweep silently ran at the hardcoded floor."""
    cfg = load(CONFIG_DIR)
    assert cfg.channels.channel("arxiv").breadth == "standard"
    assert cfg.channels.max_results_per_query("arxiv", 100) == 5000
    assert cfg.channels.max_results_per_query("lesswrong", 100) == 5000


def test_breadth_preset_falls_back_on_unknown_or_bad_preset() -> None:
    """A breadth typo or malformed preset degrades to the caller's floor — it never wedges a
    sweep (config shape is check_configs' job, not the sweep's)."""
    cfg = load(CONFIG_DIR)
    channels = cfg.channels.model_copy(deep=True)
    channels.channel("arxiv").breadth = "nonexistent"
    assert channels.max_results_per_query("arxiv", 77) == 77
    channels.breadth_presets["nonexistent"] = {"max_results_per_query": "lots"}
    assert channels.max_results_per_query("arxiv", 77) == 77
    channels.breadth_presets["nonexistent"] = {"max_results_per_query": 0}
    assert channels.max_results_per_query("arxiv", 77) == 77


# ─────────────────────────────────────────────────────────────────────────────
# (F) arXiv end to end: run_stage on a temp store over tests/fakes/http.py
# ─────────────────────────────────────────────────────────────────────────────
ARXIV_API = "https://export.arxiv.org/api/query"  # channels.yaml location, upgraded to https
P1 = {"id": "2609.00001", "title": "Debate as oversight", "published": "2026-09-02",
      "authors": ["Ethan Perez", "Sam Bowman"],
      "summary": "We study debate between language models as a scalable oversight method."}
P2 = {"id": "2609.00002", "title": "Weak to strong", "published": "2026-09-05",
      "authors": ["E. Perez", "Jan Leike"],
      "summary": "Weak supervisors elicit strong capabilities from much larger student models."}
P3 = {"id": "2609.00003", "title": "Galaxy clusters", "published": "2026-09-07",
      "authors": ["Estefania Perez"], "summary": "A namesake in the allowlisted categories."}


def _q(au: str, frm: str, start: int, n: int) -> dict[str, Any]:
    """The params ``fetch_author`` sends (same order, so FakeHttp's URL key matches)."""
    return {"search_query": f"au:{au} AND {arxiv._CAT_CLAUSE} AND "
                            f"submittedDate:[{frm}0000 TO {arxiv.ARXIV_DATE_UPPER}]",
            "start": start, "max_results": n, "sortBy": "submittedDate", "sortOrder": "ascending"}


def _arxiv_plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.arxiv_sweep", "prior": ["sources", "coverage"], "options": options}]})


def _view(store: pathlib.Path, chain_id: str) -> list[dict[str, Any]]:
    return read_view(resolve(store, sdir(chain_id))).to_pylist()


@pytest.mark.slow  # real store + runner
def test_arxiv_sweep_end_to_end(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM"):
        monkeypatch.delenv(var, raising=False)
    roster = tmp_path / "roster.yaml"
    roster.write_text('names:\n  - "Ethan Perez"\n  - "Jan Leike"\n', encoding="utf-8")
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date()
    fake = FakeHttp()
    fake.add(ARXIV_API, _q("Perez_Ethan", "20260901", 0, 2), body=_atom([P1, P2]))  # cap: 2
    fake.add(ARXIV_API, _q("Perez_Ethan", "20260905", 0, 2), body=_atom([P2, P3]))
    fake.add(ARXIV_API, _q("Leike_Jan", "20260901", 0, 2), body=_atom([P2]))
    fake.inject(ARXIV_API, _q("Leike_Jan", "20260901", 0, 2), status=503, times=2)  # + its retry
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    # the author family alone (the kw/cat families: tests/test_collect_arxiv_kw.py)
    opts = {"roster": str(roster), "from": "2026-09-01", "max_records": 2, "families": ["author"]}

    # run 1: starts both chains
    rec1 = run_line(store, _arxiv_plan(**opts), "collect.arxiv_sweep")
    assert rec1.status == "ok", rec1.error
    h1 = {k: read_header(store / o["path"]) for k, o in rec1.outputs.items()}
    assert {k: (h["chain_id"], h["kind"]) for k, h in h1.items()} == {
        "sources": ("sources.arxiv_sweep", "full"), "coverage": ("coverage.arxiv_sweep", "full")}
    assert rec1.counts["minted_ids"] == 2 and rec1.counts["inband_minted"] == 2
    assert arxiv.RETRY_DELAY_S in slept  # the one retry of the failed page
    assert any(2.5 < s <= arxiv.ARXIV_DELAY_S for s in slept)  # the transport paced at 3 s
    src1 = {r["identity"]["external_id"]: r for r in _view(store, "sources.arxiv_sweep")}
    assert sorted(src1) == ["2609.00001", "2609.00002"]
    r2 = src1["2609.00002"]
    assert r2["identity"]["channel"] == "arxiv"
    assert r2["identity"]["canonical_url"] == "https://arxiv.org/abs/2609.00002"
    assert r2["identity"]["keys"] == [{"kind": "doi", "value": "10.48550/arxiv.2609.00002"},
                                      {"kind": "arxiv", "value": "2609.00002"}]
    md = r2["metadata"]
    assert (md["title"], md["doc_type"], md["published_at"]) == (
        "Weak to strong", "preprint", dt.datetime(2026, 9, 5, tzinfo=dt.UTC))
    assert [a["name"] for a in md["authors"]] == ["E. Perez", "Jan Leike"]
    assert md["abstract"] == P2["summary"]
    (entry,) = content_entries(r2)
    assert (entry["content_role"], entry["raw"], entry["text"]["media_type"]) == (
        "abstract", None, "text/plain")
    blobs = open_blobs(store)
    atom = record_entry(r2)  # D522: the Atom <entry> as received
    assert atom is not None and (atom["raw"]["media_type"], atom["text"]) == ("application/atom+xml", None)
    xml = blobs.get(atom["raw"]["sha256"]).decode()
    assert xml.startswith("<entry") and "2609.00002" in xml and P2["summary"][:40] in " ".join(xml.split())
    assert blobs.get(entry["text"]["sha256"]).decode() == P2["summary"]
    cov1 = {r["query"]: r for r in _view(store, "coverage.arxiv_sweep")}
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    ethan, jan = cov1["Perez_Ethan"], cov1["Leike_Jan"]
    # the cap-truncated ascending pass banks [from, newest fetched] as ok (D-SWEEP1)
    assert (ethan["status"], ethan["window_start_at"], ethan["window_end_at"]) == (
        "ok", d("2026-09-01"), d("2026-09-05"))
    assert (ethan["seen"], ethan["kept"]) == (2, 2)
    assert (jan["status"], jan["error_class"], jan["window_end_at"]) == (
        "failed_transient", "http_5xx", d("2026-09-01"))

    # run 2: no flags (the stage line pins the own chains); same fake; resumes per query
    n_calls = len(fake.calls)
    rec2 = run_line(store, _arxiv_plan(**opts), "collect.arxiv_sweep")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["minted_ids"] == 0 and rec2.counts["inband_minted"] == 0
    froms = sorted(u.split("submittedDate%3A%5B")[1][:8] for u in fake.urls()[n_calls:])
    # Jan restarts at the floor; Ethan's tail asks from his watermark − 4 days (D405: the floor,
    # 09-01), the cap fills inside that look-back, so he is asked again from his 09-05 watermark
    assert froms == ["20260901", "20260901", "20260905"]
    assert rec2.counts["lookback_capped"] == 1
    src2 = {r["identity"]["external_id"]: r for r in _view(store, "sources.arxiv_sweep")}
    assert sorted(src2) == sorted(src1)  # P3 (namesake) never observed
    assert {k: r["source_id"] for k, r in src2.items()} == {
        k: r["source_id"] for k, r in src1.items()}
    assert src2["2609.00002"]["inband"]["entries"] == r2["inband"]["entries"]
    cov2 = {r["query"]: r for r in pq.read_table(store / rec2.outputs["coverage"]["path"]).to_pylist()}
    assert (cov2["Perez_Ethan"]["window_start_at"], cov2["Perez_Ethan"]["window_end_at"]) == (
        d("2026-09-05"), d("2026-09-07"))
    assert (cov2["Leike_Jan"]["status"], cov2["Leike_Jan"]["window_end_at"], cov2["Leike_Jan"]["kept"]) \
        == ("ok", d(today.isoformat()), 0)

    results = {r.check: r for r in run_checks(store)}
    red = {n: r.items for n, r in results.items() if r.status in ("failed", "warn")}
    assert red == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# (G) arXiv announcement lag (L3-13, D405): the tail re-asks announce_lag_days below the mark
# ─────────────────────────────────────────────────────────────────────────────
_UTC = dt.UTC
LAG_PAPERS = [  # (id, submitted, announced): the API lists a paper only once it is announced
    ("2610.00001", dt.datetime(2026, 10, 12, 15, 0, tzinfo=_UTC), dt.datetime(2026, 10, 14, 0, 0, tzinfo=_UTC)),
    ("2610.00002", dt.datetime(2026, 10, 13, 19, 0, tzinfo=_UTC), dt.datetime(2026, 10, 15, 0, 0, tzinfo=_UTC)),
    ("2610.00003", dt.datetime(2026, 10, 14, 5, 0, tzinfo=_UTC), dt.datetime(2026, 10, 15, 0, 0, tzinfo=_UTC)),
]


class AnnouncedArxiv:
    """A fake Atom API over :data:`LAG_PAPERS`: a paper is listed when announced by ``now`` and
    submitted inside the query's ``submittedDate`` range; every ``search_query`` is logged."""

    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 14, 7, 0, tzinfo=_UTC)
        self.queries: list[str] = []

    def __call__(self, method: str, url: str, *, headers: Any, body: bytes | None,
                 timeout: float, max_bytes: int) -> Any:
        import re
        import urllib.parse

        from stack.http import HttpResponse
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.queries.append(q["search_query"])
        m = re.search(r"submittedDate:\[(\d{12}) TO (\d+)\]", q["search_query"])
        assert m is not None
        lo = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=_UTC)
        vis = [p for p in LAG_PAPERS if p[2] <= self.now and p[1] >= lo]
        start, n = int(q.get("start", 0)), int(q.get("max_results", 100))
        ents = "".join(
            f"<entry><id>http://arxiv.org/abs/{a}v1</id><title>Paper {a}</title>"
            "<summary>alignment of language models</summary>"
            f"<published>{sub.strftime('%Y-%m-%dT%H:%M:%SZ')}</published>"
            "<author><name>X Y</name></author></entry>" for a, sub, _ in vis[start:start + n])
        feed = f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{ents}</feed>'
        return HttpResponse(200, {"content-type": "application/xml"}, feed.encode(), url)


@pytest.fixture
def announced(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    for var in ("STACK_ARXIV_FROM", "STACK_COLLECT_LIMIT", "STACK_ARXIV_ROSTER",
                "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake = AnnouncedArxiv()
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: fake.now)
    return tmp_path / "store", fake


def _kw_run(store: pathlib.Path, **options: Any) -> Any:
    opts = {"from": "2026-10-01", "families": ["kw"], "keyword_groups": ["umbrella"], **options}
    rec = run_line(store, _arxiv_plan(**opts), "collect.arxiv_sweep")
    assert rec.status == "ok", rec.error
    return rec


def test_arxiv_tail_rescans_the_announce_lag(announced: Any) -> None:
    """A paper submitted the evening before a run and announced after it (2610.00002) is
    collected by the next run: the tail asks from watermark − ``announce_lag_days`` (default 4),
    while the window written still starts at the watermark (coverage stays contiguous)."""
    store, fake = announced
    _kw_run(store)
    assert sorted(r["identity"]["external_id"] for r in _view(store, "sources.arxiv_sweep")) == [
        "2610.00001"]
    fake.now = dt.datetime(2026, 10, 21, 7, 0, tzinfo=_UTC)
    n = len(fake.queries)
    rec2 = _kw_run(store)
    assert "submittedDate:[202610100000 TO " in fake.queries[n]  # 10-14 − 4 days
    assert sorted(r["identity"]["external_id"] for r in _view(store, "sources.arxiv_sweep")) == [
        "2610.00001", "2610.00002", "2610.00003"]
    cov2 = [r for r in pq.read_table(store / rec2.outputs["coverage"]["path"]).to_pylist()
            if r["run_id"] == rec2.run_id]
    assert [(r["status"], r["window_start_at"].date().isoformat(),
             r["window_end_at"].date().isoformat()) for r in cov2] == [
        ("ok", "2026-10-14", "2026-10-21")]


@pytest.mark.slow
def test_arxiv_lag_overlap_writes_no_duplicate_rows(announced: Any) -> None:
    """Runs on three consecutive days: each re-asks the look-back, and a paper re-seen there
    unchanged writes no row (``sources_unchanged``): one row per paper over all runs."""
    store, fake = announced
    rec1 = _kw_run(store)
    assert rec1.counts["sources_written"] == 1
    fake.now = dt.datetime(2026, 10, 15, 7, 0, tzinfo=_UTC)
    rec2 = _kw_run(store)
    # 2610.00001 (submitted 10-12, before the mark) re-seen unchanged: no row; 00002/00003 new
    assert (rec2.counts["sources_written"], rec2.counts["sources_unchanged"]) == (2, 1)
    fake.now = dt.datetime(2026, 10, 16, 7, 0, tzinfo=_UTC)
    rec3 = _kw_run(store)
    assert rec3.counts.get("sources_written", 0) == 0  # all three before the mark (10-15)
    assert rec3.counts["sources_unchanged"] == 3
    observed = {r["identity"]["external_id"]: r["observed_at"]
                for r in _view(store, "sources.arxiv_sweep")}
    assert observed == {"2610.00001": dt.datetime(2026, 10, 14, 7, 0, tzinfo=_UTC),
                        "2610.00002": dt.datetime(2026, 10, 15, 7, 0, tzinfo=_UTC),
                        "2610.00003": dt.datetime(2026, 10, 15, 7, 0, tzinfo=_UTC)}
    fake.now = dt.datetime(2026, 10, 17, 7, 0, tzinfo=_UTC)
    n = len(fake.queries)
    _kw_run(store, announce_lag_days=0)  # lag 0: the tail starts at the watermark, as before D405
    assert "submittedDate:[202610160000 TO " in fake.queries[n]
