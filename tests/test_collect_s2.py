"""Tests for ``collect.semantic_scholar`` — the phrase-match academic axis (the S2 half of v1's
``search-other-apis`` stage).

Pure-unit tests cover what decides IDENTITY and RECALL, because those are the expensive mistakes
here: the arXiv bridge (now carried by the keys, so an S2 hit on a preprint the author sweep already
found merges in gather instead of duplicating), the pmid/paperId fallback chain the channels.yaml
mapping table documents, and the misuse × ``ai_context`` intersection that is the entire reason
this channel exists. The end-to-end cases drive ``run(ctx)`` through
``stack.stage_runner.run_stage`` on a temp store over ``tests/fakes/http.py`` and pin the honesty
properties on the emitted ``sources``/``coverage`` rows: a failed or capped pass claims nothing, a
windowed pass whose date filter the server ignored claims the whole floor range instead of
over-claiming, a re-run mints no new ``source_id`` and resumes from the watermark.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import time
import urllib.parse
from typing import Any

import pyarrow.parquet as pq
import pytest

from stack.build import incomplete_reasons
from stack.checks.verify import run_checks
from stack.config import ChannelDef, load
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from stack.store.parquet import read_header
from tests.fakes.http import FakeHttp
from stack.collect.inband import json_record
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


s2 = _load_wf("semantic_scholar")

HEX_A = "a" * 40
HEX_B = "b" * 40


def _fake_s2(pages: list[list[dict[str, Any]]]) -> Any:
    """A ``_get_page`` stand-in serving canned token pages (last page carries no token)."""
    state = {"i": 0}

    def fake(
        params: dict[str, Any], headers: dict[str, str], stats: dict[str, Any] | None = None
    ) -> Any:
        i = state["i"]
        if i >= len(pages):
            return {"data": []}
        state["i"] = i + 1
        return {"data": pages[i], "token": "tok" if i + 1 < len(pages) else None}

    return fake


# ─────────────────────────────────────────────────────────────────────────────
# (A) Identity — the external id, the keys (the bridge) and the fallback chain
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "paper, expect",
    [
        # The arXiv id comes first (v3's precedence), on this channel: the bridge is in the keys.
        ({"externalIds": {"ArXiv": "2204.05862"}}, ("semantic_scholar", "2204.05862")),
        # DOI form of the same: no ArXiv field, but the DOI is an arXiv DataCite DOI.
        (
            {"externalIds": {"DOI": "https://doi.org/10.48550/arXiv.2212.08073"}},
            ("semantic_scholar", "2212.08073"),
        ),
        # An arXiv id wins over a co-present journal DOI (stable when S2 later attaches a venue).
        (
            {"externalIds": {"ArXiv": "2203.02155v2", "DOI": "10.1145/3743127"}},
            ("semantic_scholar", "2203.02155"),
        ),
        # Ordinary DOI, normalized (lowercased, resolver prefix stripped; D103).
        ({"externalIds": {"DOI": "10.1145/3743127"}}, ("semantic_scholar", "10.1145/3743127")),
        ({"externalIds": {"DOI": "HTTPS://doi.org/10.1145/ABC"}},
         ("semantic_scholar", "10.1145/abc")),
        # No DOI → the pmid: fallback the channels.yaml v1-import mapping table documents.
        ({"externalIds": {"PubMed": "12345678"}}, ("semantic_scholar", "pmid:12345678")),
        # S2 ships a literal "None" string for a missing pmid (v1 had the same scar) — not an id,
        # so this falls through to the paperId.
        (
            {"externalIds": {"PubMed": "None"}, "paperId": "abc123"},
            ("semantic_scholar", "abc123"),
        ),
        # Nothing else to key on → the bare S2 paperId, so the hit is discovered not dropped.
        ({"paperId": "deadbeef"}, ("semantic_scholar", "deadbeef")),
        # Truly unkeyable.
        ({}, None),
        ({"externalIds": {}, "paperId": ""}, None),
    ],
)
def test_paper_identity(paper: dict[str, Any], expect: tuple[str, str] | None) -> None:
    assert s2.paper_identity(paper) == expect


def test_paper_keys_carry_the_arxiv_bridge() -> None:
    """THE bridge: an arXiv deposit with a journal DOI types both DOIs, the arxiv id and the S2
    paperId, so gather merges it with the arXiv sweep's source and the journal record."""
    paper = {"paperId": HEX_A, "externalIds": {"ArXiv": "2203.02155", "DOI": "10.1145/3743127",
                                                "PubMed": "None", "CorpusId": 12345}}
    assert s2.paper_keys(paper) == [
        ("doi", "10.1145/3743127"), ("doi", "10.48550/arxiv.2203.02155"),
        ("arxiv", "2203.02155"), ("s2", HEX_A)]


def test_paper_keys_drop_junk_and_corpus_ids() -> None:
    """A non-hex paperId is not an ``s2`` key (D103), a corpus id never is, a junk DOI gives no
    key; a pmid is typed."""
    paper = {"paperId": "deadbeef", "externalIds": {"DOI": "not-a-doi", "PubMed": "0042",
                                                     "CorpusId": 99}}
    assert s2.paper_keys(paper) == [("pmid", "42")]


@pytest.mark.parametrize(
    "paper, expect",
    [
        ({"externalIds": {"ArXiv": "2204.05862"}}, "https://arxiv.org/abs/2204.05862"),
        ({"externalIds": {"DOI": "10.1145/3743127"}}, "https://doi.org/10.1145/3743127"),
        ({"paperId": "abc"}, "https://www.semanticscholar.org/paper/abc"),
    ],
)
def test_paper_url(paper: dict[str, Any], expect: str) -> None:
    ident = s2.paper_identity(paper)
    assert ident is not None
    assert s2.paper_url(paper, ident[1]) == expect


@pytest.mark.parametrize(
    "paper, expect",
    [
        ({"publicationTypes": ["JournalArticle"]}, "paper"),
        ({"publicationTypes": ["Conference", "Review"]}, "paper"),
        ({"publicationTypes": ["Preprint"]}, "preprint"),
        ({"publicationTypes": ["Dataset"]}, "dataset"),
        ({"publicationTypes": None}, "other"),
        ({"publicationTypes": ["Editorial"]}, "other"),
        # An arXiv deposit is a preprint even when S2 has since attached a journal type: the thing
        # discovered here — and the thing acquire will fetch from the abs page — is the preprint.
        ({"publicationTypes": ["JournalArticle"], "externalIds": {"ArXiv": "2101.00001"}},
         "preprint"),
    ],
)
def test_doc_type_of(paper: dict[str, Any], expect: str) -> None:
    assert s2.doc_type_of(paper) == expect


@pytest.mark.parametrize(
    "paper, expect",
    [
        ({"publicationDate": "2024-03-05"}, "2024-03-05"),
        ({"publicationDate": "2024-03-05T00:00:00"}, "2024-03-05"),
        # Year-only papers are widened so they still carry a usable date (and still gate on year).
        ({"year": 2021}, "2021-01-01"),
        ({"publicationDate": "", "year": 2021}, "2021-01-01"),
        ({}, None),
        ({"year": True}, None),  # bool is an int subclass — must not read as year 1
    ],
)
def test_paper_pub_date(paper: dict[str, Any], expect: str | None) -> None:
    assert s2.paper_pub_date(paper) == expect


# ─────────────────────────────────────────────────────────────────────────────
# (B) Query construction — the misuse × ai_context intersection
# ─────────────────────────────────────────────────────────────────────────────
def test_group_query_ors_quoted_phrases() -> None:
    """Phrase-matching is the point: each term is quoted, so "language model" cannot token-AND."""
    assert s2.group_query(["RLHF", "reward model"], [], False) == '"RLHF" | "reward model"'


def test_group_query_intersects_misuse_with_ai_context() -> None:
    """THE reason this channel exists: a bare dangerous-capability term returns the whole
    biosecurity literature, so a misuse group is ANDed with the AI-context disjunction."""
    q = s2.group_query(["bioweapon", "pathogen"], ["large language model", "frontier model"], True)
    assert q == '("bioweapon" | "pathogen") + ("large language model" | "frontier model")'


def test_group_query_drops_unquotable_terms() -> None:
    """A term containing a double quote would break out of the quoting and silently change the
    query's meaning, so it is dropped rather than sent."""
    assert s2.group_query(['say "hi"', "debate"], [], False) == '"debate"'


def test_group_query_ai_context_ignored_for_non_misuse() -> None:
    assert s2.group_query(["RLHF"], ["large language model"], False) == '"RLHF"'


def test_plan_includes_misuse_groups_with_their_intersection() -> None:
    """collect.openalex skips every misuse group; this channel must NOT — and each one must arrive
    already intersected with ai_context."""
    cfg = load(CONFIG_DIR)
    plan = s2.plan_groups(cfg)
    misuse = [item for item in plan if item["misuse"]]
    assert [item["id"] for item in misuse] == ["misuse-bio", "misuse-chem", "misuse-cyber"]
    for item in misuse:
        assert " + (" in item["s2_query"], item
        assert '"large language model"' in item["s2_query"]
    # …and the non-misuse groups are here too, as a phrase-match second opinion on OpenAlex.
    assert any(item["id"] == "rlhf" for item in plan)
    assert all(item["query"] == f"group:{item['id']}" for item in plan)


def test_plan_skips_misuse_groups_when_ai_context_is_empty() -> None:
    """An unintersected misuse query would pull the entire biosecurity literature in and then claim
    coverage for it — worse than not running. Non-misuse groups are unaffected."""
    cfg = load(CONFIG_DIR)
    cfg.channels.ai_context = []
    plan = s2.plan_groups(cfg)
    assert not [item for item in plan if item["misuse"]]
    assert any(item["id"] == "rlhf" for item in plan)


def test_plan_honours_the_channel_group_selection() -> None:
    """`concept_groups:` on the channel is the seam v1's per-source subset (`sources.europepmc`)
    lands on, so an explicit list must narrow the plan."""
    cfg = load(CONFIG_DIR)
    chan = cfg.channels.channel("semantic_scholar")
    assert s2._selected_group_ids(chan, ["a", "b"]) == ["a", "b"]  # channel says 'all'
    narrowed = ChannelDef(
        id="semantic_scholar", kind="search", license_class="restricted",
        concept_groups=["misuse-bio"], channel_group=chan.channel_group,
    )
    assert s2._selected_group_ids(narrowed, ["rlhf", "misuse-bio"]) == ["misuse-bio"]


def test_plan_intersects_needs_ai_context_groups_like_misuse() -> None:
    """A `needs_ai_context` group (SR scope: "decision theory" alone is operations research and
    medicine) goes out ANDed with ai_context, and is skipped without one; a plain SR group is not
    intersected."""
    cfg = load(CONFIG_DIR)
    cfg.channels.ai_context = ["language model"]
    plan = {item["id"]: item for item in s2.plan_groups(cfg)}
    assert plan["decision-theory"]["s2_query"] == (
        '("decision theory" | "Newcomb\'s problem") + ("language model")')
    assert plan["decision-theory"]["needs_ai_context"] and not plan["decision-theory"]["misuse"]
    assert plan["shard-theory"]["s2_query"] == '"shard theory"'
    cfg.channels.ai_context = []
    ids = {item["id"] for item in s2.plan_groups(cfg)}
    assert "decision-theory" not in ids and "steerability" not in ids and "shard-theory" in ids


def test_s2_terms_override_drops_stemmed_terms_from_s2_only() -> None:
    """`s2_terms` (D229): "steerability" (S2 stems it to "steer") and "heuristic arguments" (0 of
    14 relevant) are not sent to S2; the arXiv keyword family still phrase-matches steerability;
    a non-empty override replaces the group's terms on S2."""
    cfg = load(CONFIG_DIR)
    cfg.channels.ai_context = ["language model"]
    groups = {g.id: g for g in cfg.channels.concept_groups}
    assert groups["steerability"].s2_terms == [] and groups["heuristic-arguments"].s2_terms == []
    assert groups["steerability"].terms == ["steerability"]  # phrase-match channels keep it
    ids = {item["id"] for item in s2.plan_groups(cfg)}
    assert "steerability" not in ids and "heuristic-arguments" not in ids and "shard-theory" in ids
    groups["shard-theory"].s2_terms = ["shard theory of human values"]
    plan = {item["id"]: item for item in s2.plan_groups(cfg)}
    assert plan["shard-theory"]["s2_query"] == '"shard theory of human values"'


def test_only_queries_narrows_the_plan_and_refuses_unknown_labels() -> None:
    plan = s2.plan_groups(load(CONFIG_DIR))
    assert s2.only_queries(plan, None) == plan
    got = s2.only_queries(plan, ["group:shard-theory", "group:rlhf"])
    assert [item["query"] for item in got] == ["group:rlhf", "group:shard-theory"]  # plan order
    assert [item["query"] for item in s2.only_queries(plan, "group:rlhf")] == ["group:rlhf"]
    with pytest.raises(ValueError, match="queries"):
        s2.only_queries(plan, ["group:rlhf", "group:nope"])
    with pytest.raises(ValueError, match="queries"):  # skipped on S2 by `s2_terms: []` (D229)
        s2.only_queries(plan, ["group:steerability"])


def test_search_params_sends_open_ended_lower_bound_only() -> None:
    """A resumed pass sends `<from>:` — open-ended on the right. A first-ever pass sends NO date
    filter, so papers with no publication date (which S2's own filter drops) are seen once."""
    assert "publicationDateOrYear" not in s2.search_params("q", None)
    assert s2.search_params("q", "2024-01-01")["publicationDateOrYear"] == "2024-01-01:"


# ─────────────────────────────────────────────────────────────────────────────
# (C) The page retry ladder
# ─────────────────────────────────────────────────────────────────────────────
def test_get_page_does_not_retry_a_403(monkeypatch: Any) -> None:
    """Key-less, a 403 is a genuine refusal of the shared pool — do not loop on it; the status is
    kept for the window's error class."""
    monkeypatch.setattr(s2, "_sleep", lambda s: None)
    calls = {"n": 0}

    def fake(url: str, params: dict[str, Any], headers: dict[str, str], **kw: Any) -> Any:
        calls["n"] += 1
        return 403, None

    monkeypatch.setattr(s2, "http_get_json", fake)
    stats: dict[str, Any] = {}
    assert s2._get_page({"query": "q"}, {}, stats) is None
    assert calls["n"] == 1
    assert s2.error_class_of(stats["last_status"]) == ("provider_auth", False)


def test_get_page_records_retry_stats(monkeypatch: Any) -> None:
    """503 then success: the retry is visible in the run counts instead of vanishing, and it
    sleeps v3's first ladder step."""
    slept: list[float] = []
    monkeypatch.setattr(s2, "_sleep", slept.append)
    seq = [(503, None), (200, {"data": []})]

    def fake(url: str, params: dict[str, Any], headers: dict[str, str], **kw: Any) -> Any:
        return seq.pop(0)

    monkeypatch.setattr(s2, "http_get_json", fake)
    stats: dict[str, Any] = {}
    assert s2._get_page({"query": "q"}, {}, stats) == {"data": []}
    assert stats["http_5xx"] == 1 and stats["retries"] == 1
    assert slept == [s2.RETRY_DELAYS_S[0]]


def test_get_page_does_not_ladder_a_429(monkeypatch: Any) -> None:
    """D550: a 429 that comes back through ``http_get_json`` has already been waited out by the
    transport (the Gate's backoff), so the ladder does not re-send it: the page fails
    ``rate_limit`` at once."""
    slept: list[float] = []
    monkeypatch.setattr(s2, "_sleep", slept.append)
    seq = [(429, None), (200, {"data": []})]

    def fake(url: str, params: dict[str, Any], headers: dict[str, str], **kw: Any) -> Any:
        return seq.pop(0)

    monkeypatch.setattr(s2, "http_get_json", fake)
    stats: dict[str, Any] = {}
    assert s2._get_page({"query": "q"}, {}, stats) is None
    assert stats["http_429"] == 1 and stats.get("retries", 0) == 0 and slept == []
    assert s2.error_class_of(stats["last_status"]) == ("rate_limit", False)


# ─────────────────────────────────────────────────────────────────────────────
# (D) Truncation / pagination
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_group_truncates_at_the_limit(monkeypatch: Any) -> None:
    monkeypatch.setattr(s2, "_sleep", lambda s: None)
    monkeypatch.setattr(
        s2, "_get_page", _fake_s2([[{"paperId": str(i)} for i in range(3)], [{"paperId": "x"}]])
    )
    papers, exhausted = s2.fetch_group("q", None, 2, {})
    assert exhausted == "truncated" and len(papers) == 2


def test_fetch_group_completes_when_the_token_runs_out(monkeypatch: Any) -> None:
    monkeypatch.setattr(s2, "_sleep", lambda s: None)
    monkeypatch.setattr(s2, "_get_page", _fake_s2([[{"paperId": "a"}], [{"paperId": "b"}]]))
    papers, exhausted = s2.fetch_group("q", None, 100, {})
    assert exhausted == "complete" and [p["paperId"] for p in papers] == ["a", "b"]


def test_fetch_group_stops_at_the_page_budget(monkeypatch: Any) -> None:
    """The run's ``max_pages`` budget: a token still pending when the budget is spent is a
    truncated pass, never a complete one."""
    monkeypatch.setattr(s2, "_get_page", _fake_s2([[{"paperId": "a"}], [{"paperId": "b"}]]))
    stats: dict[str, Any] = {}
    papers, exhausted = s2.fetch_group("q", None, 100, {}, stats, max_pages=1)
    assert exhausted == "truncated" and [p["paperId"] for p in papers] == ["a"]
    assert stats["pages_fetched"] == 1


def test_ignored_date_filter_is_detected() -> None:
    """The honesty backstop: if the server returns papers older than the lower bound it ignored the
    filter, so the query must not claim [eff_from, today]. Undated papers prove nothing either way.
    """
    old = [{"paperId": "x", "publicationDate": "2020-06-01"}]
    fresh = [{"paperId": "y", "publicationDate": "2026-07-20"}]
    undated = [{"paperId": "z"}]
    assert s2._filter_ignored(old, "2026-07-01") is True
    assert s2._filter_ignored(fresh, "2026-07-01") is False
    assert s2._filter_ignored(undated, "2026-07-01") is False
    # a paper one day either side of the bound is on-window (date-granularity/timezone slack)
    assert s2._filter_ignored([{"paperId": "s", "publicationDate": "2026-06-30"}], "2026-07-01") \
        is False


# ─────────────────────────────────────────────────────────────────────────────
# (E) End to end: run_stage on a temp store over tests/fakes/http.py
# ─────────────────────────────────────────────────────────────────────────────
FLOOR = "2024-01-01"
_ENV = ("STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES",
        "STACK_S2_FROM", "STACK_S2_FULL")

# A: an arXiv deposit with a journal DOI and an abstract; B: DOI only, no abstract (S2 withholds
# it); C: paperId only, pmid "None"; OLD: below the floor (gated, and the honor-detection witness).
A = {"paperId": HEX_A, "title": "Debate helps oversight", "abstract": "We study debate between models as an oversight method.",
     "externalIds": {"ArXiv": "2402.00001", "DOI": "10.1145/3743127", "CorpusId": 1},
     "authors": [{"name": "Ann Author"}, {"name": "Bob Writer"}], "year": 2024,
     "publicationDate": "2024-02-03", "publicationTypes": ["JournalArticle"], "venue": "V",
     "citationCount": 3}
B = {"paperId": HEX_B, "title": "Reward hacking survey", "abstract": None,
     "externalIds": {"DOI": "10.5555/abc.1"}, "authors": [{"name": "Cy Coder"}], "year": 2025,
     "publicationDate": "2025-05-06", "publicationTypes": ["Review"], "citationCount": 0}
C = {"paperId": "c" * 40, "title": "No ids", "externalIds": {"PubMed": "None"}, "year": 2025,
     "publicationTypes": None}
OLD = {"paperId": "d" * 40, "title": "Old", "externalIds": {"DOI": "10.5555/old"}, "year": 2019,
       "publicationDate": "2019-01-01"}


def _params(query: str, eff_from: str | None = None, token: str | None = None) -> dict[str, Any]:
    """The params ``fetch_group`` sends (same order, so FakeHttp's URL key matches)."""
    p = s2.search_params(query, eff_from)
    if token:
        p["token"] = token
    return p


def _plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.semantic_scholar", "prior": ["sources", "coverage"],
         "options": options}]})


def _view(store: pathlib.Path, chain_id: str) -> list[dict[str, Any]]:
    return read_view(resolve(store, sdir(chain_id))).to_pylist()


def _first_two_groups() -> tuple[dict[str, Any], dict[str, Any]]:
    import stack.config as sc  # through the module: `no_keyword_cuts` patches `sc.load`

    umbrella, second = s2.plan_groups(sc.load(CONFIG_DIR))[:2]
    return umbrella, second


@pytest.mark.slow  # real store + runner
@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_semantic_scholar_end_to_end(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    # T-RUN item 6: the one key name is sent as x-api-key; v3's second name never is.
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "fake-s2-key-sent")
    monkeypatch.setenv("STACK_S2_API_KEY", "must-not-be-sent")
    g1, g2 = _first_two_groups()
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date().isoformat()
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    fake = FakeHttp()
    # run 1: g1 two pages; g2 fails past the retry ladder. FLOOR is past the channel's from_date,
    # so the first pass already sends it as the lower bound (B7a; at the channel floor it sends
    # none: test_page_budget_stops_the_run).
    fake.add(s2.API_BASE, _params(g1["s2_query"], FLOOR), json={"data": [A, B, OLD], "token": "t1"})
    fake.add(s2.API_BASE, _params(g1["s2_query"], FLOOR, token="t1"), json={"data": [C]})
    fake.add(s2.API_BASE, _params(g2["s2_query"], FLOOR), json={"data": [A]})
    fake.inject(s2.API_BASE, _params(g2["s2_query"], FLOOR), status=503, times=4)  # 1 + 3 retries
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    opts = {"max_queries": 2, "from": FLOOR}

    rec1 = run_line(store, _plan(**opts), "collect.semantic_scholar")
    assert rec1.status == "ok", rec1.error
    # R3-2 (round 5): g2's failed_transient window leaves the scope incomplete (the next run or
    # pass retries it; a build lists it under `incomplete`, reason windows_failed)
    assert rec1.scope_complete is False and rec1.counts["windows_failed_transient"] == 1
    assert incomplete_reasons(rec1.counts) == ["windows_failed"]
    h1 = {k: read_header(store / o["path"]) for k, o in rec1.outputs.items()}
    assert {k: (h["chain_id"], h["kind"]) for k, h in h1.items()} == {
        "sources": ("sources.semantic_scholar", "full"),
        "coverage": ("coverage.semantic_scholar", "full")}
    assert rec1.counts["minted_ids"] == 3 and rec1.counts["inband_minted"] == 1
    assert rec1.counts["below_floor"] == 1 and rec1.counts["arxiv_bridged"] == 1
    assert f"publicationDateOrYear={FLOOR}%3A" in fake.calls[0].url and rec1.counts["windowed"] == 2
    assert rec1.counts["filter_ignored"] == 1  # OLD came back: still the floor range [FLOOR, today]
    ladder = [s for s in slept if s in s2.RETRY_DELAYS_S]
    assert ladder == list(s2.RETRY_DELAYS_S)  # v3's ladder on g2, once
    assert any(0.5 < s <= s2.DELAY_S for s in slept)  # the transport paced at the channel's 2.5 s
    assert all(c.headers.get("x-api-key") == "fake-s2-key-sent" for c in fake.calls)
    assert all("must-not-be-sent" not in str(c.headers) + c.url for c in fake.calls)
    assert all("fake-s2-key-sent" not in c.url for c in fake.calls)
    assert rec1.counts["s2_keyed"] == 1

    src1 = {r["identity"]["external_id"]: r for r in _view(store, "sources.semantic_scholar")}
    assert sorted(src1) == ["10.5555/abc.1", "2402.00001", "c" * 40]
    ra = src1["2402.00001"]
    assert ra["identity"]["channel"] == "semantic_scholar"
    assert ra["identity"]["canonical_url"] == "https://arxiv.org/abs/2402.00001"
    assert ra["identity"]["keys"] == [
        {"kind": "doi", "value": "10.1145/3743127"},
        {"kind": "doi", "value": "10.48550/arxiv.2402.00001"},
        {"kind": "arxiv", "value": "2402.00001"}, {"kind": "s2", "value": HEX_A}]
    md = ra["metadata"]
    assert (md["title"], md["doc_type"], md["published_at"], md["abstract"]) == (
        "Debate helps oversight", "preprint", d("2024-02-03"), "We study debate between models as an oversight method.")
    assert [a["name"] for a in md["authors"]] == ["Ann Author", "Bob Writer"]
    assert md["license_class"] == "restricted"
    (entry,) = content_entries(ra)
    assert (entry["content_role"], entry["raw"], entry["text"]["media_type"]) == ("abstract", None, "text/plain")
    blobs = open_blobs(store)
    paper = json_record(blobs, ra)  # D522: the paper as S2 returned it
    assert paper["paperId"] == HEX_A and paper["title"] == "Debate helps oversight"
    assert blobs.get(entry["text"]["sha256"]).decode() == "We study debate between models as an oversight method."
    assert content_entries(src1["10.5555/abc.1"]) == []
    assert src1["c" * 40]["identity"]["keys"] == [{"kind": "s2", "value": "c" * 40}]

    cov1 = {r["query"]: r for r in _view(store, "coverage.semantic_scholar")}
    k1, k2 = (s2.coverage_query(g) for g in (g1, g2))
    from stack.collect.base import query_text
    c1, c2 = cov1[query_text(k1)], cov1[query_text(k2)]
    assert (c1["status"], c1["window_start_at"], c1["window_end_at"]) == (
        "ok", d(FLOOR), d(today))
    assert (c1["seen"], c1["kept"]) == (3, 3)
    assert (c2["status"], c2["error_class"], c2["window_start_at"], c2["window_end_at"]) == (
        "failed_transient", "http_5xx", d(FLOOR), d(FLOOR))

    # run 2 (next day; the same day g1 is covered through today, D226): no flags (the stage line
    # pins the own chains). g1 resumes from its watermark less the 30-day index lag (D551; date
    # filter sent) but the server ignores it (OLD comes back) → claims the floor range; B gained an
    # abstract → a new row on the same source_id; g2 retries from the floor and succeeds.
    t1 = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t1)
    tomorrow = t1.date().isoformat()
    b2 = {**B, "abstract": "Now S2 ships an abstract for this survey too."}
    lag = (dt.date.fromisoformat(today) - dt.timedelta(days=s2.DEFAULT_LOOKBACK_DAYS)).isoformat()
    fake.add(s2.API_BASE, _params(g1["s2_query"], lag), json={"data": [A, b2, OLD],
                                                              "token": "t1"})
    fake.add(s2.API_BASE, _params(g1["s2_query"], lag, token="t1"), json={"data": [C]})
    n_calls = len(fake.calls)
    rec2 = run_line(store, _plan(**opts), "collect.semantic_scholar", kind="part")
    assert rec2.status == "ok", rec2.error
    assert rec2.scope_complete is True  # g2 retried and ok: nothing failed this run
    assert f"publicationDateOrYear={lag}%3A" in fake.urls()[n_calls]
    assert rec2.counts["minted_ids"] == 0 and rec2.counts["filter_ignored"] == 1
    assert rec2.counts["sources_written"] == 1 and rec2.counts["inband_minted"] == 1
    assert read_header(store / rec2.outputs["sources"]["path"])["kind"] == "part"
    src2 = {r["identity"]["external_id"]: r for r in _view(store, "sources.semantic_scholar")}
    assert {k: r["source_id"] for k, r in src2.items()} == {
        k: r["source_id"] for k, r in src1.items()}
    assert src2["10.5555/abc.1"]["metadata"]["abstract"] == "Now S2 ships an abstract for this survey too."
    assert src2["2402.00001"]["observed_at"] == ra["observed_at"]  # unchanged: no new row
    cov2 = {r["query"]: r for r in pq.read_table(store / rec2.outputs["coverage"]["path"]).to_pylist()}
    c1b, c2b = cov2[query_text(k1)], cov2[query_text(k2)]
    assert (c1b["status"], c1b["window_start_at"], c1b["window_end_at"]) == (
        "ok", d(FLOOR), d(tomorrow))  # the floor range, not [today, tomorrow]
    assert (c2b["status"], c2b["window_end_at"], c2b["kept"], c2b["attempts"]) == (
        "ok", d(tomorrow), 0, 1)  # attempts count per window key; the failed one was [floor, floor]

    results = {r.check: r for r in run_checks(store)}
    red = {n: r.items for n, r in results.items() if r.status in ("failed", "warn")}
    assert red == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_page_budget_stops_the_run(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """``max_pages: 1`` (the live smoke): one request; the query it cut is ``partial`` over
    ``[floor, floor]``; the next query gets no row; the run is a planned stop. The floor is the
    channel's own ``from_date``: the first pass sends no date filter."""
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    g1, _ = _first_two_groups()
    store = tmp_path / "store"
    fake = FakeHttp()
    fake.add(s2.API_BASE, _params(g1["s2_query"]), json={"data": [A, B], "token": "t1"})
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    rec = run_line(store, _plan(max_queries=2, max_pages=1, **{"from": "2020-01-01"}),
                    "collect.semantic_scholar")
    assert rec.status == "ok", rec.error
    assert len(fake.calls) == 1
    assert rec.scope_complete is False and rec.counts["budget_stopped"] == 1
    # D492: the page cap cut the query's window (`limit`); the next query got none (`budget`)
    assert incomplete_reasons(rec.counts) == ["limit", "budget"] and rec.counts["windows_capped"] == 1
    (cov,) = _view(store, "coverage.semantic_scholar")
    assert (cov["status"], cov["is_exhausted"], cov["window_start_at"], cov["window_end_at"]) == (
        "partial", False, dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    assert "publicationDateOrYear" not in fake.calls[0].url
    assert len(_view(store, "sources.semantic_scholar")) == 2


def test_floor_moved_back_pulls_only_the_gap(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """B7a: the key leaves the floor out, so after a first window from 2025-10-01 a run with
    `from: 2024-11-30` the same day sends one bounded request for the hole
    (publicationDateOrYear=2024-11-30:2025-09-30) and writes [2024-11-30, 2025-10-01]; the covered
    tail is not pulled again; a third run sends nothing."""
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    g1, _ = _first_two_groups()
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date().isoformat()
    new = {**B, "publicationDate": "2025-11-04", "year": 2025}
    old = {**A, "publicationDate": "2025-02-03", "year": 2025}
    fake = FakeHttp()
    fake.add(s2.API_BASE, _params(g1["s2_query"], "2025-10-01"), json={"data": [new]})
    gap = {**s2.search_params(g1["s2_query"], "2024-11-30", "2025-09-30")}
    assert gap["publicationDateOrYear"] == "2024-11-30:2025-09-30"
    fake.add(s2.API_BASE, gap, json={"data": [old]})
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    opts = {"queries": [g1["query"]]}
    rec1 = run_line(store, _plan(**opts, **{"from": "2025-10-01"}), "collect.semantic_scholar")
    assert rec1.status == "ok", rec1.error
    assert len(fake.calls) == 1
    rec2 = run_line(store, _plan(**opts, **{"from": "2024-11-30"}), "collect.semantic_scholar")
    assert rec2.status == "ok", rec2.error
    assert [c.url for c in fake.calls[1:]] == [fake.urls()[1]]  # the gap only
    assert rec2.counts["holes"] == 1 and rec2.counts["minted_ids"] == 1
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    cov = sorted((r["window_start_at"], r["window_end_at"], r["status"])
                 for r in _view(store, "coverage.semantic_scholar"))
    assert cov == [(d("2024-11-30"), d("2025-10-01"), "ok"), (d("2025-10-01"), d(today), "ok")]
    rec3 = run_line(store, _plan(**opts, **{"from": "2024-11-30"}), "collect.semantic_scholar")
    assert rec3.status == "ok" and len(fake.calls) == 2 and rec3.counts["already_covered"] == 1


def _s2_server(corpus: list[dict[str, Any]], *, page: int = 1000) -> Any:
    """A :class:`~stack.collect.base.Sender` modelling S2 bulk search over ``corpus``: the
    ``publicationDateOrYear=<from>:<to>`` filter is honoured on ``publicationDate``, results come in
    ``paperId`` order, ``page`` per answer with a ``token`` while more remain. Records each URL."""
    import json
    import urllib.parse

    from stack.http import HttpResponse

    urls: list[str] = []

    def send(method: str, url: str, **kw: Any) -> HttpResponse:
        urls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        lo, _, hi = (q.get("publicationDateOrYear") or ":").partition(":")
        hits = sorted((p for p in corpus if (not lo or p["publicationDate"] >= lo)
                       and (not hi or p["publicationDate"] <= hi)), key=lambda p: p["paperId"])
        start = int(q.get("token") or 0)
        body: dict[str, Any] = {"data": hits[start:start + page]}
        if start + page < len(hits):
            body["token"] = str(start + page)
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(body).encode(), url)

    send.urls = urls  # type: ignore[attr-defined]
    return send


def _paper(pid: str, date: str) -> dict[str, Any]:
    return {"paperId": pid * 40, "title": f"Paper {pid}", "abstract": f"Abstract of paper {pid} on oversight.",
            "externalIds": {"DOI": f"10.5555/{pid}"}, "year": int(date[:4]), "publicationDate": date,
            "publicationTypes": ["JournalArticle"], "authors": [{"name": "Ann Author"}]}


@pytest.mark.usefixtures("no_keyword_cuts")
@pytest.mark.parametrize("lookback", [None, 0], ids=["default-30", "off"])
def test_s2_tail_rescans_the_index_lag_below_the_watermark(tmp_path: pathlib.Path, monkeypatch: Any,
                                                           lookback: int | None) -> None:
    """D551: run 2 (ten days after run 1) asks from watermark − 30 days. A paper dated 20 days before
    the watermark that S2 indexed after run 1 is collected; one dated 45 days before it is not; a
    paper run 1 saw comes back unchanged and writes no row; the window is still written from the
    watermark. With ``lookback_days: 0`` (the old behaviour) the late paper is missed."""
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    g1, _ = _first_two_groups()
    store = tmp_path / "store"
    day1 = dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)
    iso = lambda t, days=0: (t + dt.timedelta(days=days)).date().isoformat()
    seen = _paper("a", iso(day1, -5))
    corpus = [_paper("o", iso(day1, -60)), seen]
    server = _s2_server(corpus)
    monkeypatch.setattr("stack.collect.base.urllib_send", server)
    opts: dict[str, Any] = {"queries": [g1["query"]], "from": iso(day1, -90)}
    if lookback is not None:
        opts["lookback_days"] = lookback
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: day1)
    rec1 = run_line(store, _plan(**opts), "collect.semantic_scholar")
    assert rec1.status == "ok", rec1.error
    assert rec1.counts["sources_written"] == 2

    corpus += [_paper("i", iso(day1, -20)), _paper("x", iso(day1, -45)), _paper("n", iso(day1, 3))]
    day2 = day1 + dt.timedelta(days=10)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: day2)
    rec2 = run_line(store, _plan(**opts), "collect.semantic_scholar", kind="part")
    assert rec2.status == "ok", rec2.error
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(server.urls[-1]).query)["publicationDateOrYear"]
    got = {r["identity"]["external_id"] for r in _view(store, "sources.semantic_scholar")}
    if lookback is None:
        assert sent == [f"{iso(day1, -30)}:"]
        assert "10.5555/i" in got and "10.5555/x" not in got
        assert (rec2.counts["sources_written"], rec2.counts["sources_unchanged"]) == (2, 1)
    else:
        assert sent == [f"{iso(day1)}:"]
        assert "10.5555/i" not in got and "10.5555/x" not in got
        assert rec2.counts["sources_written"] == 1 and rec2.counts.get("sources_unchanged", 0) == 0
    rows = pq.read_table(store / rec2.outputs["sources"]["path"]).to_pylist()
    assert sorted(r["identity"]["external_id"] for r in rows) == sorted(
        ["10.5555/i", "10.5555/n"] if lookback is None else ["10.5555/n"])  # no duplicate of `seen`
    cov = pq.read_table(store / rec2.outputs["coverage"]["path"]).to_pylist()
    assert [(r["status"], r["window_start_at"].date().isoformat(), r["window_end_at"].date().isoformat())
            for r in cov] == [("ok", iso(day1), iso(day2))]


@pytest.mark.usefixtures("no_keyword_cuts")
def test_s2_lookback_capped_asks_again_from_the_watermark(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """D551: when the cap fills inside the look-back the query is asked again from the watermark, so
    the window is still covered (``ok``) instead of capped every run; the look-back's hits are kept."""
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    g1, _ = _first_two_groups()
    store = tmp_path / "store"
    day1 = dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC)
    iso = lambda t, days=0: (t + dt.timedelta(days=days)).date().isoformat()
    corpus = [_paper("o", iso(day1, -60))]
    server = _s2_server(corpus, page=2)
    monkeypatch.setattr("stack.collect.base.urllib_send", server)
    opts = {"queries": [g1["query"]], "from": iso(day1, -90), "max_records": 2}
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: day1)
    assert run_line(store, _plan(**opts), "collect.semantic_scholar").status == "ok"
    corpus += [_paper(c, iso(day1, -d)) for c, d in (("p", 25), ("q", 15), ("r", 5))]
    corpus.append(_paper("n", iso(day1, 2)))
    day2 = day1 + dt.timedelta(days=7)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: day2)
    rec2 = run_line(store, _plan(**opts), "collect.semantic_scholar", kind="part")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["lookback_capped"] == 1 and rec2.scope_complete is True
    got = {r["identity"]["external_id"] for r in _view(store, "sources.semantic_scholar")}
    assert {"10.5555/n", "10.5555/p"} <= got  # the capped look-back's paperId-ordered two; n again from the mark
    cov = pq.read_table(store / rec2.outputs["coverage"]["path"]).to_pylist()
    assert [(r["status"], r["window_start_at"].date().isoformat()) for r in cov] == [("ok", iso(day1))]
