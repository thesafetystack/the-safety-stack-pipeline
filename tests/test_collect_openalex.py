"""Tests for ``collect.openalex`` (the broad academic-search axis, v1 ``search_openalex.py``) and
the v1 search-config parity both search sweeps rely on. The blog cases moved to
``tests/test_collect_blogs.py`` (K#8: T2.3a and T2.6 do not share a file).

Pure-unit tests cover the parts that decide IDENTITY, because identity mistakes are the expensive
kind: the arXiv-DOI bridge (now the ``arxiv`` key a work's arXiv DOI yields, so gather merges it
with the author sweep's source) and the OpenAlex type filter (v1's ``type:article`` now excludes
every arXiv work). v3's DB-backed ``run(services, config, run)`` tests became the end-to-end cases
in (F): ``run_stage`` on a temp store over ``tests/fakes/http.py`` (no network).
"""

from __future__ import annotations

import functools
import json
import importlib.util
import pathlib
from typing import Any

import pytest
import yaml

from stack.build import incomplete_reasons
from stack.config import load
from stack.collect.inband import json_record
from stack.collect.roster import parse_roster
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


openalex = _load_wf("openalex")


# ─────────────────────────────────────────────────────────────────────────────
# (A) OpenAlex: identity, filters, normalisation
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "work, expect",
    [
        # an arXiv DOI stays on this channel (DOI else W…, §5.6); the bridge is the `arxiv` KEY
        # (test_work_keys_bridge_arxiv_doi) — v3 re-keyed it to ('arxiv', id), which on the
        # harness would mint a second source_id for the arXiv chain's (arxiv, id) pair (C32)
        ({"doi": "https://doi.org/10.48550/arXiv.2204.05862"},
         ("openalex", "10.48550/arxiv.2204.05862")),
        ({"doi": "10.48550/ARXIV.2305.04388v2"}, ("openalex", "10.48550/arxiv.2305.04388")),
        # ordinary DOI: normalized, this channel
        ({"doi": "https://doi.org/10.1145/3743127"}, ("openalex", "10.1145/3743127")),
        # a DOI outside the `doi` normal form (D103) falls back to the work id
        ({"doi": "10.1/x", "id": "https://openalex.org/W9"}, ("openalex", "W9")),
        # no DOI: keyed on the OpenAlex work id rather than dropped
        ({"id": "https://openalex.org/W123456"}, ("openalex", "W123456")),
        # nothing to key on at all
        ({}, None),
    ],
)
def test_work_identity(work: dict[str, Any], expect: tuple[str, str] | None) -> None:
    assert openalex.work_identity(work) == expect


def test_work_keys_bridge_arxiv_doi() -> None:
    """Keys are typed by stack.scholarly_records (D103): the work id, the DOI, the PMID from
    ``ids``; an arXiv DataCite DOI also yields the ``arxiv`` key — THE bridge that lets gather
    merge the OpenAlex record with the arXiv sweep's source instead of double-counting it."""
    work = {"id": "https://openalex.org/W42", "doi": "https://doi.org/10.48550/arXiv.2204.05862v1",
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/0012345"}}
    assert openalex.work_keys(work) == [
        ("doi", "10.48550/arxiv.2204.05862"), ("arxiv", "2204.05862"), ("openalex", "W42"),
        ("pmid", "12345")]
    assert openalex.work_keys({"id": "https://openalex.org/W7", "doi": "10.1/junk"}) == [
        ("openalex", "W7")]
    assert openalex.work_url(work, "10.48550/arxiv.2204.05862") == "https://arxiv.org/abs/2204.05862"


@pytest.mark.parametrize("status, expect", [
    (429, ("rate_limit", False)), (503, ("http_5xx", False)), (403, ("provider_auth", False)),
    (400, ("query_invalid", True)), (0, ("network", False)), (200, ("network", False))])
def test_error_class_of(status: int, expect: tuple[str, bool]) -> None:
    """A failed page's last status → the coverage outcome; only a rejected filter is permanent."""
    assert openalex.error_class_of(status) == expect


def test_type_filter_keeps_preprints() -> None:
    """v1 filtered ``type:article``. OpenAlex has since retyped arXiv works as ``preprint``, so
    that filter now drops every one of them — a live probe of one group returned 1,536 works vs
    5,210. The regression this test guards is a well-meaning "port v1 exactly"."""
    assert "preprint" in openalex.ARTICLE_TYPES
    filt = openalex.group_filter(["RLHF"], "2020-01-01", False)
    assert "type:article|preprint" in filt
    assert "from_publication_date:2020-01-01" in filt
    assert "title_and_abstract.search:RLHF" in filt


def test_type_filter_keeps_conference_papers_in_every_family() -> None:
    """D653: OpenAlex types proceedings papers ``conference-paper`` (22-33 % of every family's hits); one
    whose arXiv copy OpenAlex merged into the proceedings record is reachable only under that type (Corrupted Reward
    Channel, IJCAI 2017). Every family's filter carries it: groups, institutions, author buckets."""
    assert "conference-paper" in openalex.ARTICLE_TYPES
    cfg = load(CONFIG_DIR)
    items = (openalex.plan_groups(cfg) + openalex.plan_institutions(cfg)
             + openalex.plan_authors(parse_roster({"names": [{"name": "X", "openalex_ids": ["A1"]}]})))
    assert {i["kind"] for i in items} == {"group", "institution", "authors"}
    for item in items:
        types = openalex._filter_for(item, "2015-01-01", None).split(",", 1)[0]
        assert types == "type:article|preprint|review|book-chapter|conference-paper", item["query"]


def test_conference_paper_type_changes_every_coverage_key(monkeypatch: Any) -> None:
    """D2's cost: the type list is in every query's filter, so every coverage key changes and every query (the
    repo's channels.yaml and roster: all three families) re-sweeps from the floor once; no key survives."""
    from stack.collect.roster import default_path, load_roster_file

    cfg = load(CONFIG_DIR)
    items = (openalex.plan_groups(cfg) + openalex.plan_institutions(cfg)
             + openalex.plan_authors(load_roster_file(default_path(cfg.config_dir))))
    new = [openalex.coverage_query(i, None) for i in items]
    monkeypatch.setattr(openalex, "ARTICLE_TYPES", ("article", "preprint", "review", "book-chapter"))
    old = [openalex.coverage_query(i, None) for i in items]
    assert len({json.dumps(k, sort_keys=True) for k in new}) == len(items)
    assert [i["query"] for i, a, b in zip(items, old, new, strict=True) if a == b] == []


def test_group_filter_drops_comma_terms_and_adds_cs_field() -> None:
    """Commas separate ANDed OpenAlex filters, so a term containing one would silently rewrite
    the whole expression. Broad groups get the CS field filter (v1's flood tamer)."""
    filt = openalex.group_filter(["safe, aligned", "RLAIF"], "2020-01-01", True)
    assert "title_and_abstract.search:RLAIF" in filt
    assert "safe, aligned" not in filt
    assert f"primary_topic.field.id:{openalex.CS_FIELD_ID}" in filt


def test_no_built_filter_uses_a_created_or_updated_axis() -> None:
    """D582: OpenAlex refuses ``from_created_date`` / ``from_updated_date`` below a Premium plan (a 429
    "Plan upgrade required"), so no family's filter may carry either; the builders take no such
    argument. Every family and window shape: floor, look-back start, bounded window, the key."""
    cfg = load(CONFIG_DIR)
    roster_item = openalex.plan_authors(parse_roster({"names": [{"name": "X", "openalex_ids": ["A1"]}]}))
    items = openalex.plan_groups(cfg) + openalex.plan_institutions(cfg) + roster_item
    assert {i["kind"] for i in items} == {"group", "institution", "authors"}
    built = [openalex._filter_for(i, frm, to) for i in items
             for frm, to in (("2025-10-01", None), ("2026-08-16", None), ("2024-11-30", "2025-09-30"),
                             (openalex.KEY_FLOOR, None))]
    assert built and not [f for f in built if "created" in f or "updated" in f]


@pytest.mark.parametrize(
    "type_, expect",
    [("preprint", "preprint"), ("article", "paper"), ("review", "paper"), ("conference-paper", "paper"),
     ("dataset", "other")],
)
def test_doc_type_of(type_: str, expect: str) -> None:
    assert openalex.doc_type_of({"type": type_}) == expect


def test_openalex_get_page_records_retry_stats(monkeypatch: Any) -> None:
    """_get_page silently backs off on 429/5xx/timeout; passing a stats dict makes that visible.
    A 429-then-200 sequence returns the page AND leaves a trace (http_429 + retries) — retry
    behaviour is unchanged, so a success-after-retry is no longer invisible (missing-mailto
    debugging incident)."""
    monkeypatch.setattr(openalex, "_sleep", lambda s: None)
    seq: list[tuple[int, Any]] = [(429, None), (200, {"results": [], "meta": {}})]
    state = {"i": 0}

    def fake(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, Any]:
        i = state["i"]
        state["i"] = i + 1
        return seq[i]

    monkeypatch.setattr(openalex, "http_get_json", fake)
    stats: dict[str, int] = {"retries": 0, "http_429": 0, "http_5xx": 0, "timeouts": 0}
    data = openalex._get_page({"filter": "x"}, stats)
    assert data == {"results": [], "meta": {}}                 # result correct after the retry
    assert stats["http_429"] == 1 and stats["retries"] == 1    # the backoff is now visible
    assert stats["http_5xx"] == 0 and stats["timeouts"] == 0
    # without a stats dict the behaviour is identical (and does not crash)
    state["i"] = 0
    assert openalex._get_page({"filter": "x"}) == {"results": [], "meta": {}}


def test_openalex_get_page_fails_at_once_on_a_returned_cooldown_answer(monkeypatch: Any) -> None:
    """D588: the transport returns the Gate's synthetic cooldown 429 only once its own three cooldown
    waits are spent; ``_get_page`` then fails the page (``rate_limit``) at once instead of spending
    the 2/8/20 s ladder on it (a step would only meet our own cooldown). A real 429 still ladders."""
    sleeps: list[float] = []
    monkeypatch.setattr(openalex, "_sleep", sleeps.append)
    calls: list[int] = []

    def fake(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, Any]:
        calls.append(1)
        return 429, {"retryAfter": 5.0, "message": "Provider cooldown active"}  # throttled_open's body

    monkeypatch.setattr(openalex, "http_get_json", fake)
    stats: dict[str, int] = {"retries": 0, "http_429": 0, "http_5xx": 0, "timeouts": 0}
    assert openalex._get_page({"filter": "x"}, stats) is None
    assert len(calls) == 1 and sleeps == [] and stats["retries"] == 0
    assert openalex.error_class_of(stats["last_status"]) == ("rate_limit", False)


# ─────────────────────────────────────────────────────────────────────────────
# (A2) OpenAlex: institution-id sweep (Part B) + affiliation/venue/citation capture (Part A)
# ─────────────────────────────────────────────────────────────────────────────
def test_institutions_parsed_from_config() -> None:
    """The 25 v1 institution ids and the three Chinese labs of the Chinese-coverage work load, and the `fields` volume guard joins
    with `|` on the generic big-company entities (frontier labs pull unfiltered); the Chinese labs take the CS field
    alone."""
    insts = load(CONFIG_DIR).channels.institutions()
    assert len(insts) == 28
    plan = openalex.plan_institutions(load(CONFIG_DIR))
    assert len(plan) == 28
    cn = {p["org"]: p for p in plan if p["org"] in ("shanghai-ai-lab", "baai", "zhipu")}
    assert {o: (p["id"], p["fields"]) for o, p in cn.items()} == {
        "shanghai-ai-lab": ("I4391012619", [17]), "baai": ("I4210100255", [17]), "zhipu": ("I4401726915", [17])}
    assert "primary_topic.field.id:17," in openalex.institution_filter("I4391012619", "2015-01-01", [17]) + ","
    # one org that carries a field guard, one that does not
    alibaba = next(p for p in plan if p["org"] == "alibaba")
    assert alibaba["fields"] == [17, 33, 18, 32, 26]
    assert alibaba["query"].startswith("inst:I")
    assert alibaba["topic_keys"] == ["org:alibaba"]
    anthropic = next(p for p in plan if p["org"] == "anthropic")
    assert anthropic["fields"] == []


def test_institution_filter_with_and_without_fields() -> None:
    """The institution filter carries the SAME type bound the group queries use (a deliberate v3
    departure from v1, which ran this family untyped), plus `institutions.id` and the floor; the
    `primary_topic.field.id` guard appears only when the org configured fields, joined with `|`."""
    plain = openalex.institution_filter("I4387930290", "2020-01-01", [])
    assert "type:article|preprint|review|book-chapter|conference-paper" in plain     # v3 type bound, not in v1
    assert "institutions.id:I4387930290" in plain
    assert "from_publication_date:2020-01-01" in plain
    assert "primary_topic.field.id" not in plain                    # no guard without fields
    guarded = openalex.institution_filter("I45928872", "2020-01-01", [17, 33, 18, 32, 26])
    assert "primary_topic.field.id:17|33|18|32|26" in guarded


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),  # full URL -> bare
        ("0000-0002-1825-009X", "0000-0002-1825-009X"),                    # already bare, X kept
        ("0000-0002-1825-009x", "0000-0002-1825-009X"),                    # lowercase x -> X
        ("", None),
        (None, None),
        ("not-an-orcid", None),
    ],
)
def test_normalize_orcid(raw: Any, expect: str | None) -> None:
    assert openalex.normalize_orcid(raw) == expect


def test_work_authors_detail_prefers_institutions_falls_back_to_raw() -> None:
    """authors_detail carries {name, orcid?, institutions}. Institution display names win; when
    OpenAlex resolved none, the raw_affiliation_strings stand in so an affiliated author is not
    dropped as affiliation-less. ORCID is normalized to the bare id and omitted when absent."""
    work = {
        "authorships": [
            {
                "author": {
                    "display_name": "Alice Researcher",
                    "orcid": "https://orcid.org/0000-0002-1825-0097",
                },
                "institutions": [{"display_name": "Anthropic"}, {"display_name": "UC Berkeley"}],
                "raw_affiliation_strings": ["ignored when institutions resolve"],
            },
            {
                "author": {"display_name": "Bob Unresolved"},   # no orcid
                "institutions": [],
                "raw_affiliation_strings": ["Independent Lab, Someplace"],
            },
        ]
    }
    assert openalex.work_authors_detail(work) == [
        {
            "name": "Alice Researcher",
            "orcid": "0000-0002-1825-0097",
            "institutions": ["Anthropic", "UC Berkeley"],
        },
        {"name": "Bob Unresolved", "institutions": ["Independent Lab, Someplace"]},
    ]


def test_metadata_authors_carry_orcid_and_affiliation_line() -> None:
    """``sources.metadata.authors`` (A.5 AUTHOR) keeps v3's authors_detail as ``orcid`` and one
    ``affiliation_raw`` line per author (institutions joined), ``None`` when there are none."""
    work = {"authorships": [
        {"author": {"display_name": "Alice", "orcid": "https://orcid.org/0000-0002-1825-0097"},
         "institutions": [{"display_name": "Anthropic"}, {"display_name": "UC Berkeley"}]},
        {"author": {"display_name": "Bob"}, "institutions": [], "raw_affiliation_strings": []}]}
    assert openalex.metadata_authors(work) == [
        {"name": "Alice", "orcid": "0000-0002-1825-0097",
         "affiliation_raw": "Anthropic; UC Berkeley"},
        {"name": "Bob", "orcid": None, "affiliation_raw": None}]
    assert openalex.metadata_authors({}) is None


# ─────────────────────────────────────────────────────────────────────────────
# (D) v1 search-config parity — the recall strategy IS this config
# ─────────────────────────────────────────────────────────────────────────────
SR_GROUPS = {  # the SR-scope extension block (2026-10-12; docs/scope/shallow-review.md (c), (d))
    "decision-theory-af", "decision-theory", "acausal", "anthropics", "infra-bayes",
    "natural-abstractions", "tiling", "program-equilibrium", "game-theory-ai",
    "gradual-disempowerment", "alignment-targets", "social-choice-ai", "model-psychology",
    "introspection", "agi-metrics", "heuristic-arguments", "brain-like-agi", "scientist-ai",
    "steerability", "shard-theory"}
SR_NEEDS_AI_CONTEXT = {"decision-theory", "anthropics", "game-theory-ai", "social-choice-ai",
                       "introspection", "steerability"}
#: The 2006-2019 vocabulary block (channels.yaml 2.2.0, D646): new groups, quoted `openalex_terms`.
LEGACY_GROUPS = {"legacy-alignment", "legacy-safety"}
#: The Chinese-coverage group (CS-filtered quoted phrases on OpenAlex, no forum queries).
COVERAGE_GROUPS = frozenset({"llm-safety"})


def test_concept_groups_match_v1_search_config() -> None:
    """channels.yaml carries v1's full concept-group set. Before the port it held three groups,
    so both sweeps ran at ~7% of v1's query breadth while looking healthy. The SR-scope block is
    and the legacy block (D646) are the only additions (no techniques: no taxonomy entry;
    `needs_ai_context` on the broad words)."""
    v1 = yaml.safe_load((REPO / "tests/fixtures/v1-search_config.yaml").read_text())
    v1_groups = {g["id"].replace("_", "-"): g for g in v1["concept_groups"]}
    every = {g.id: g for g in load(CONFIG_DIR).channels.concept_groups}
    added = SR_GROUPS | LEGACY_GROUPS | COVERAGE_GROUPS
    assert set(every) == set(v1_groups) | added and not set(v1_groups) & added
    cov = every["llm-safety"]
    assert cov.broad and not (cov.techniques or cov.misuse or cov.needs_ai_context) and cov.forum_terms == []
    assert cov.openalex_terms == [f'"{t}"' for t in cov.terms]
    sr = {gid: g for gid, g in every.items() if gid in SR_GROUPS}
    assert {gid for gid, g in sr.items() if g.needs_ai_context} == SR_NEEDS_AI_CONTEXT
    assert not any(g.techniques or g.misuse or g.broad for g in sr.values())
    legacy = {gid: g for gid, g in every.items() if gid in LEGACY_GROUPS}
    assert {gid for gid, g in legacy.items() if g.needs_ai_context} == {"legacy-alignment"}
    assert not any(g.techniques or g.misuse or g.broad for g in legacy.values())
    assert all(g.openalex_terms == [f'"{t}"' for t in g.terms] for g in legacy.values())
    ours = {gid: g for gid, g in every.items() if gid not in added}
    assert not any(g.needs_ai_context for g in ours.values())
    for gid, g in ours.items():
        assert g.terms == v1_groups[gid]["terms"], gid
        assert g.broad == bool(v1_groups[gid].get("broad")), gid
        assert g.misuse == bool(v1_groups[gid].get("misuse")), gid
        assert g.openalex_terms == v1_groups[gid].get("openalex_terms"), gid


def test_openalex_plan_skips_misuse_and_phrase_only_groups() -> None:
    """v1's two OpenAlex exclusions, both live-consumed: `misuse` groups (token-AND cannot express
    the AI-context intersection) and groups whose `openalex_terms` override is empty (no viable
    term at all). Everything else queries, with the override replacing `terms` where set."""
    cfg = load(CONFIG_DIR)
    plan = {p["id"]: p for p in openalex.plan_groups(cfg)}
    groups = {g.id: g for g in cfg.channels.concept_groups}
    assert not any(groups[gid].misuse for gid in plan)
    assert "agent-foundations" not in plan          # openalex_terms: [] in v1
    assert plan["sparse-coding"]["terms"] == [
        "sparse autoencoder interpretability", "dictionary learning interpretability",
        "polysemanticity", "monosemanticity",
    ]
    assert plan["rlhf"]["terms"] == groups["rlhf"].terms   # no override -> plain terms
    expected = sum(
        1 for g in groups.values() if not g.misuse and g.terms_for_openalex()
    )
    assert len(plan) == expected


def test_phrase_query_ands_quoted_phrases_with_ai_context() -> None:
    """The boolean phrase form the 2026-10-12 probe showed OpenAlex honours: quoted (an already
    quoted openalex_terms entry is the same phrase), OR'd, ANDed; a comma or inner quote drops."""
    assert openalex.phrase_query(['decision theory', '"Newcomb\'s problem"', "a, b"],
                                 ["language model", 'x "y"', "AGI"]) == (
        '("decision theory" OR "Newcomb\'s problem") AND ("language model" OR "AGI")')
    assert openalex.phrase_query(["decision theory"], []) == ""


def test_openalex_plan_intersects_needs_ai_context_groups() -> None:
    """A `needs_ai_context` group runs, as the boolean phrase query; a plain SR group runs on its
    quoted openalex_terms (phrase match); with no ai_context the needs_ai_context groups skip."""
    cfg = load(CONFIG_DIR)
    cfg.channels.ai_context = ["language model", "AGI"]
    plan = {p["id"]: p for p in openalex.plan_groups(cfg)}
    # steerability: `openalex_terms: []` (OpenAlex stems it to "steer"; the 2026-10-12 window)
    assert SR_NEEDS_AI_CONTEXT - set(plan) == {"steerability"}
    f = openalex._filter_for(plan["decision-theory"], "2026-09-01", None)
    assert f == ("type:article|preprint|review|book-chapter|conference-paper,from_publication_date:2026-09-01,"
                 "title_and_abstract.search:(\"decision theory\" OR \"Newcomb's problem\") AND "
                 "(\"language model\" OR \"AGI\")")
    assert plan["rlhf"]["ai_context"] == [] and "AND" not in openalex._filter_for(
        plan["rlhf"], "2026-09-01", None)
    assert openalex._filter_for(plan["shard-theory"], "2026-09-01", None).endswith(
        'title_and_abstract.search:"shard theory"')
    cfg.channels.ai_context = []
    assert not SR_NEEDS_AI_CONTEXT & {p["id"] for p in openalex.plan_groups(cfg)}


def test_forum_plan_covers_v1_curated_keywords_and_tags() -> None:
    """v1's curated LW keyword list is a subset of the concept-group terms EXCEPT two forum-native
    usages, which are queried as bare keywords; tags and the karma floor match v1 exactly."""
    v1 = yaml.safe_load((REPO / "tests/fixtures/v1-search_config.yaml").read_text())
    cfg = load(CONFIG_DIR)
    forums = _load_wf("forums")
    plan = forums.plan_queries(cfg, cfg.channels.channel("lesswrong"), forums.DEFAULT_TAGS)
    queried = {str(p["term"]).lower() for p in plan if p["kind"] == "term"}
    for kw in v1["lesswrong"]["keywords"]:
        assert kw.lower() in queried, kw
    assert list(forums.DEFAULT_TAGS) == v1["lesswrong"]["tags"]
    assert forums.DEFAULT_MIN_KARMA == v1["lesswrong"]["min_karma"]


# ─────────────────────────────────────────────────────────────────────────────
# (E) D-INBAND1 — OpenAlex in-band abstract capture (de-inverted index → partial snapshot)
# ─────────────────────────────────────────────────────────────────────────────


def test_deinvert_abstract_round_trips_a_small_index() -> None:
    """The inverted index rebuilds the abstract exactly: each token placed at its positions, sorted,
    single-space joined — including a token that recurs at multiple positions."""
    idx = {"the": [0], "quick": [1], "brown": [2], "fox": [3]}
    assert openalex.deinvert_abstract(idx) == "the quick brown fox"
    recurring = {"a": [0, 2], "b": [1], "c": [3]}
    assert openalex.deinvert_abstract(recurring) == "a b a c"


@pytest.mark.parametrize(
    "bad",
    [None, {}, "not a dict", 123, [], {"tok": "not a list"}, {"tok": []}, {"x": [True], "y": [False]}],
)
def test_deinvert_abstract_returns_none_for_malformed(bad: Any) -> None:
    """None/empty/wrong-type indices and indices with no usable integer positions (incl. bool,
    which is an int subclass) yield None rather than crashing the sweep."""
    assert openalex.deinvert_abstract(bad) is None


def test_deinvert_abstract_skips_nonint_positions_defensively() -> None:
    """A stray non-int position (str/float/None) inside an otherwise-valid index is skipped, the
    valid positions still reconstruct."""
    idx = {"hello": [0, "bad", None], "world": [1, 2.5]}
    assert openalex.deinvert_abstract(idx) == "hello world"


# ═════════════════════════════════════════════════════════════════════════════
# OA-PDF capture at discovery (best_oa_location -> collect.meta oa_pdf)
# ═════════════════════════════════════════════════════════════════════════════
def test_select_fields_request_best_oa_location() -> None:
    # fetch.metadata self-skips OpenAlex for this channel's sources, so the OA-PDF URL must
    # arrive at discovery. D525: no select by default (full works carry it); a `select:` option
    # must keep it (and every field the sweep reads).
    from stack.collect.provider_keys import openalex_select

    assert "best_oa_location" in openalex.SELECT_FIELDS
    assert "select" not in openalex._base_params("f")
    assert openalex._base_params("f", "id")["select"] == "id"
    assert openalex_select(None, openalex.SELECT_FIELDS) is None
    full = openalex_select(openalex.SELECT_FIELDS.split(",") + ["topics"], openalex.SELECT_FIELDS)
    assert full == openalex.SELECT_FIELDS + ",topics"
    with pytest.raises(ValueError, match="best_oa_location"):
        openalex_select(openalex.SELECT_FIELDS.replace(",best_oa_location", ""), openalex.SELECT_FIELDS)
    with pytest.raises(ValueError, match="comma list"):
        openalex_select(3, openalex.SELECT_FIELDS)


# ═════════════════════════════════════════════════════════════════════════════
# OpenAlex is metered (2026): the daily budget wall is a HALT, not 59 errored queries
# ═════════════════════════════════════════════════════════════════════════════

_BUDGET_429 = {
    "error": "Rate limit exceeded",
    "message": "Insufficient budget. This request costs $0.001 but you only have $0 remaining. "
               "Resets at midnight UTC.",
    "retryAfter": 17827,
}


def test_budget_429_is_distinguished_from_a_burst_429(monkeypatch: Any) -> None:
    """A 429 whose body says 'come back in 5 hours' is the day's budget, spent: raise at once,
    no backoff ladder. A bare 429, or one with a short retryAfter, is a burst and keeps the
    2/8/20 s ladder (the existing behaviour every fake relies on)."""
    monkeypatch.setattr(openalex, "_sleep", lambda s: None)
    # budget: raises immediately, ONE request spent, zero retries
    monkeypatch.setattr(openalex, "http_get_json", lambda url, params, **kw: (429, _BUDGET_429))
    stats: dict[str, Any] = {"retries": 0, "http_429": 0, "http_5xx": 0, "timeouts": 0}
    with pytest.raises(openalex.OpenAlexBudgetExhausted) as info:
        openalex._get_page({"filter": "x"}, stats)
    assert info.value.retry_after_s == 17827 and "Insufficient budget" in info.value.message
    assert stats["http_429"] == 1 and stats["retries"] == 0
    # burst with a SHORT retryAfter: ladder, then the page
    seq = [(429, {"retryAfter": 5}), (200, {"results": [], "meta": {}})]
    monkeypatch.setattr(openalex, "http_get_json", lambda url, params, **kw: seq.pop(0))
    stats = {"retries": 0, "http_429": 0, "http_5xx": 0, "timeouts": 0}
    assert openalex._get_page({"filter": "x"}, stats) == {"results": [], "meta": {}}
    assert stats["http_429"] == 1 and stats["retries"] == 1


def test_lookback_start_is_the_watermark_minus_lookback_clamped_at_the_floor() -> None:
    """D582: a resumed open-ended tail asks from max(floor, watermark − lookback_days), never above
    where its window starts; 0 days = from the watermark (the old created axis is gone)."""
    import datetime as dt

    mark = dt.date(2026, 10, 14)
    assert openalex.DEFAULT_LOOKBACK_DAYS == 60
    assert openalex.lookback_start("2025-10-01", "2026-10-14", mark, 60) == "2026-08-15"
    assert openalex.lookback_start("2026-09-01", "2026-10-14", mark, 60) == "2026-09-01"  # clamped
    assert openalex.lookback_start("2026-09-01", "2026-10-14", mark, 0) == "2026-10-14"
    assert openalex.lookback_start("2026-11-01", "2026-11-01", mark, 3) == "2026-11-01"  # floor past the mark


def test_refusals_are_classified_and_credential_free(monkeypatch: Any) -> None:
    """D583: a plan refusal (any status) is failed_permanent[provider_plan], a 401/403 failed_transient
    [provider_auth]; a plain 429, a 5xx and a 200 are not refusals. The reason never carries the key."""
    monkeypatch.setenv("OPENALEX_API_KEY", "k-test-secret-123")
    plan = openalex._refusal(429, {"error": "Plan upgrade required", "message": "needs Premium"})
    assert plan is not None and (plan.error_class, plan.permanent, plan.reason) == (
        "provider_plan", True, "HTTP 429: needs Premium")
    auth = openalex._refusal(403, {"error": "Forbidden", "message": "bad key k-test-secret-123 at "
                                   "https://api.openalex.org/works?api_key=k-test-secret-123&x=1"})
    assert auth is not None and (auth.error_class, auth.permanent) == ("provider_auth", False)
    assert "k-test-secret" not in auth.reason and auth.reason.count("<redacted>") == 2
    assert openalex._refusal(401, None) is not None
    assert openalex._refusal(429, {"error": "Rate limit exceeded"}) is None
    assert openalex._refusal(503, None) is None and openalex._refusal(200, {"results": []}) is None


# ═════════════════════════════════════════════════════════════════════════════
# (F) End to end: run_stage on a temp store over tests/fakes/http.py (no network)
# ═════════════════════════════════════════════════════════════════════════════
API = "https://api.openalex.org/works"
FLOOR = "2026-09-01"
_AUTHOR = {"author": {"display_name": "Ethan Perez", "orcid": "https://orcid.org/0000-0002-1825-0097"},
           "institutions": [{"display_name": "Anthropic"}], "raw_affiliation_strings": []}


def _work(n: int, *, doi: str | None, type_: str = "article", title: str | None = None,
          cited: int = 0, pmid: str | None = None) -> dict[str, Any]:
    wid = f"https://openalex.org/W10{n}"
    return {"id": wid, "doi": doi, "title": title or f"Work {n}",
            "ids": {"openalex": wid, **({"doi": doi} if doi else {}), **({"pmid": pmid} if pmid else {})},
            "type": type_, "publication_date": f"2026-09-0{n}", "authorships": [_AUTHOR],
            "primary_location": {"landing_page_url": f"https://pub.test/{n}"}, "cited_by_count": cited,
            "best_oa_location": None,
            "abstract_inverted_index": {"We": [0], "study": [1], "debate": [2], "as": [3],
                                        "scalable": [4], "oversight": [5], f"case{n}": [6]}}


W1 = _work(1, doi="https://doi.org/10.48550/arXiv.2609.00001", type_="preprint")
W2 = _work(2, doi="https://doi.org/10.1145/3743127", pmid="https://pubmed.ncbi.nlm.nih.gov/123")
W3 = _work(3, doi=None, type_="review")
_BUDGET_429 = {"error": "Rate limit exceeded", "retryAfter": 17827,
               "message": "Insufficient budget. This request costs $0.001 but you only have $0 "
                          "remaining. Resets at midnight UTC."}


def _oa_plan(**options: Any) -> Any:
    from stack.plan import Plan

    return Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.openalex", "prior": ["sources", "coverage"], "out": {"kind": "part"},
         "options": options}]})


@functools.cache
def _groups() -> dict[str, dict[str, Any]]:
    return {p["query"]: p for p in openalex.plan_groups(load(CONFIG_DIR))}


def _filters(**kw: Any) -> dict[str, str]:
    """The filter each test query sends (built from the repo's channels.yaml groups) from ``floor``
    (the floor, or a look-back start) to ``to``."""
    groups = _groups()
    return {q: openalex.group_filter(groups[q]["terms"], kw.get("floor", FLOOR), groups[q]["broad"],
                                     to_date=kw.get("to"))
            for q in ("group:superalignment", "group:rlhf")}


def _params(filt: str, cursor: str = "*") -> dict[str, Any]:
    return {"filter": filt, "per_page": openalex.PAGE_SIZE,
            "cursor": cursor}


def _page(works: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"results": works, "meta": {"next_cursor": cursor}}


def _view(store: pathlib.Path, chain_id: str) -> list[dict[str, Any]]:
    from stack.store.chain import read_view, resolve
    from tests.synth.records import sdir

    return read_view(resolve(store, sdir(chain_id))).to_pylist()


@pytest.fixture
def oa_env(monkeypatch: Any) -> Any:
    """No ambient sampling/limits/mailto; a set OPENALEXAPIKEY must never be sent (only
    STACK_MAILTO is read); a FakeHttp at the harness transport; sleeps recorded."""
    import time

    from tests.fakes.http import FakeHttp

    for var in ("STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES",
                "STACK_OPENALEX_FROM", "STACK_OPENALEX_FULL", "STACK_OPENALEX_RETRY_FAILED_RUN",
                "STACK_MAILTO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENALEXAPIKEY", "test-openalex-secret")
    fake = FakeHttp()
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return fake, slept


def test_http_get_json_outside_a_run_returns_the_429_body(monkeypatch: Any) -> None:
    """The seam outside a run (tools/probe_filters.py) sends one request through the harness's
    sender with the UA and no key, and a non-2xx body is decoded: OpenAlex's budget verdict
    (``retryAfter``) lives only there. The rate-limit headers are remembered."""
    from tests.fakes.http import FakeHttp

    monkeypatch.setenv("OPENALEXAPIKEY", "test-openalex-secret")
    monkeypatch.setattr(openalex, "_T", None)
    fake = FakeHttp()
    fake.add(API, {"filter": "x"}, status=429, json=_BUDGET_429,
             headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Remaining-USD": "0"})
    monkeypatch.setattr(openalex, "urllib_send", fake)
    assert openalex.http_get_json(API, {"filter": "x"}) == (429, _BUDGET_429)
    (call,) = fake.calls
    assert call.headers == {"User-Agent": openalex.BROWSER_UA} and "secret" not in call.url
    assert openalex._ratelimit_detail() == {"remaining": "0", "remaining_usd": "0"}


@pytest.mark.slow  # real store + runner
@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_openalex_end_to_end(tmp_path: pathlib.Path, oa_env: Any, monkeypatch: Any) -> None:
    """Run 1 starts both series (the arXiv-DOI work carries the bridged keys, abstract in-band, a
    failed query banks a non-advancing window); run 2 the next day over the same pages mints no new
    id, writes no row for an unchanged work (a counter move is not a change, C23) and resumes the
    complete query from its look-back start (D582: watermark − ``lookback_days``), writing the window from
    the watermark; run 3 a day later hits the day's budget: halted, uncharged,
    nothing after it; a failed-only retry of run 3 waits out its retry_after (D226), then re-runs
    just its failed query; verify passes."""
    import datetime as dt

    import pyarrow.parquet as pq

    from stack.checks.verify import run_checks
    from stack.store.blobs import open_blobs
    from stack.store.parquet import read_header
    from tests.synth.records import run_line

    fake, slept = oa_env
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date().isoformat()
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    t_day = dt.datetime.now(dt.UTC).date()
    back = lambda days: (t_day - dt.timedelta(days=days)).isoformat()
    f0, fc = _filters(), _filters(floor=back(3))  # run 2 resumes rlhf 3 days below run 1's watermark
    sup, rlhf = "group:superalignment", "group:rlhf"
    fake.add(API, _params(f0[rlhf]), json=_page([W1, W2], "c2"))
    fake.add(API, _params(f0[rlhf], "c2"), json=_page([W3]))
    fake.add(API, _params(f0[sup]), json=_page([W1, W2]))  # run 2: found again, no new id
    fake.inject(API, _params(f0[sup]), status=503, times=4)  # run 1: v3's ladder, all spent
    fake.add(API, _params(fc[rlhf]), json=_page([{**W1, "cited_by_count": 99},
                                                  {**W2, "title": "Work 2, revised"}]))
    opts = {"queries": [sup, rlhf], "from": FLOOR, "lookback_days": 3}

    # run 1: starts both series
    rec1 = run_line(store, _oa_plan(**opts), "collect.openalex")
    # R3-2 (round 5): sup's failed_transient window leaves the scope incomplete (retried next run)
    assert rec1.status == "ok" and rec1.scope_complete is False, rec1.error
    h1 = {k: read_header(store / o["path"]) for k, o in rec1.outputs.items()}
    assert {k: (h["chain_id"], h["kind"]) for k, h in h1.items()} == {
        "sources": ("sources.openalex", "full"), "coverage": ("coverage.openalex", "full")}
    assert rec1.counts["minted_ids"] == 3 and rec1.counts["inband_minted"] == 3
    assert [s for s in slept if s >= 1] == list(openalex.RETRY_DELAYS_S)  # v3's ladder
    assert all("Authorization" not in c.headers and "secret" not in c.url for c in fake.calls)
    src1 = {r["identity"]["external_id"]: r for r in _view(store, "sources.openalex")}
    assert sorted(src1) == ["10.1145/3743127", "10.48550/arxiv.2609.00001", "W103"]
    r1 = src1["10.48550/arxiv.2609.00001"]
    assert r1["identity"]["channel"] == "openalex"
    assert r1["identity"]["canonical_url"] == "https://arxiv.org/abs/2609.00001"
    assert r1["identity"]["keys"] == [{"kind": "doi", "value": "10.48550/arxiv.2609.00001"},
                                      {"kind": "arxiv", "value": "2609.00001"},
                                      {"kind": "openalex", "value": "W101"}]
    assert {k["kind"]: k["value"] for k in src1["10.1145/3743127"]["identity"]["keys"]}["pmid"] == "123"
    md = r1["metadata"]
    assert (md["title"], md["doc_type"], md["published_at"], md["license_class"]) == (
        "Work 1", "preprint", d("2026-09-01"), "open")
    assert md["authors"] == [{"name": "Ethan Perez", "orcid": "0000-0002-1825-0097",
                              "affiliation_raw": "Anthropic"}]
    assert md["abstract"] == "We study debate as scalable oversight case1"
    (entry,) = content_entries(r1)
    assert (entry["content_role"], entry["raw"]) == ("abstract", None)
    blobs = open_blobs(store)
    work = json_record(blobs, r1)  # D522: the work as OpenAlex returned it (no select, D525)
    assert work["id"].endswith("W101") and "abstract_inverted_index" in work and "cited_by_count" in work
    assert all("select=" not in c.url for c in fake.calls)
    assert blobs.get(entry["text"]["sha256"]).decode() == "We study debate as scalable oversight case1"
    assert entry["text"]["bytes"] == len("We study debate as scalable oversight case1")
    cov1 = {openalex.query_label(r["query"]): r for r in _view(store, "coverage.openalex")}
    assert (cov1[rlhf]["status"], cov1[rlhf]["window_start_at"], cov1[rlhf]["window_end_at"],
            cov1[rlhf]["seen"], cov1[rlhf]["kept"]) == ("ok", d(FLOOR), d(today), 3, 3)
    assert (cov1[sup]["status"], cov1[sup]["error_class"], cov1[sup]["window_end_at"],
            cov1[sup]["attempts"]) == ("failed_transient", "http_5xx", d(FLOOR), 1)

    # run 2 (next day): no flags; the failed query re-runs from the floor, the complete one resumes
    # (the same day it is covered through today and not asked, D226)
    t0 = dt.datetime.now(dt.UTC)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=1))
    n_calls = len(fake.calls)
    rec2 = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["minted_ids"] == 0 and rec2.counts["inband_minted"] == 0
    assert rec2.counts["sources_unchanged"] >= 2  # W1 (counter moved only) and W2 via sup
    filters2 = sorted(c.url for c in fake.calls[n_calls:])
    assert filters2 == sorted(fake.key("GET", API, _params(p))[1] for p in (f0[sup], fc[rlhf]))
    cov2 = sorted((r["window_start_at"], r["window_end_at"], r["status"]) for r in _view(store, "coverage.openalex")
                  if openalex.query_label(r["query"]) == rlhf)
    # the look-back is re-asked, not claimed: the new window starts at run 1's watermark
    assert cov2 == [(d(FLOOR), d(today), "ok"), (d(today), d(back(-1)), "ok")]
    assert rec2.counts["lookback_days"] == 3 and rec2.counts["lookback_capped"] == 0
    part2 = pq.read_table(store / rec2.outputs["sources"]["path"]).to_pylist()
    assert [(r["identity"]["external_id"], r["metadata"]["title"]) for r in part2] == [
        ("10.1145/3743127", "Work 2, revised")]
    assert part2[0]["source_id"] == src1["10.1145/3743127"]["source_id"]

    # run 3 (a day later): the day's budget is spent on the first request: halt, nothing after it
    fc = _filters(floor=back(2))  # run 2's watermark (today + 1) − 3 days
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=2))
    n_calls = len(fake.calls)
    fake.inject(API, _params(fc[sup]), status=429, json=_BUDGET_429)
    rec3 = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec3.status == "ok" and rec3.scope_complete is False, rec3.error
    assert len(fake.calls) == n_calls + 1 and rec3.counts["budget_exhausted"] == 1
    cov3 = {openalex.query_label(r["query"]): r
            for r in pq.read_table(store / rec3.outputs["coverage"]["path"]).to_pylist()}
    assert set(cov3) == {sup}  # rlhf never reached: no row
    halted = cov3[sup]
    assert (halted["status"], halted["error_class"], halted["window_end_at"]) == (
        "failed_transient", "rate_limit", d(FLOOR))
    assert halted["attempts"] == 1  # uncharged: run 1's count at this window, not 2
    assert halted["retry_after"] > dt.datetime.now(dt.UTC) + dt.timedelta(hours=4)
    assert "sources" not in rec3.outputs

    # failed-only retry of run 3: held until the budget's retry_after, then just the halted query
    fake.add(API, _params(fc[sup]), json=_page([]))
    n_calls = len(fake.calls)
    held = run_line(store, _oa_plan(**opts, retry_failed_run=rec3.run_id), "collect.openalex")
    assert held.status == "ok" and held.scope_complete is False, held.error
    assert len(fake.calls) == n_calls and held.counts["held_retry_after"] == 1
    monkeypatch.setattr("stack.collect.base.utcnow",
                        lambda: halted["retry_after"] + dt.timedelta(minutes=1))
    rec4 = run_line(store, _oa_plan(**opts, retry_failed_run=rec3.run_id), "collect.openalex")
    assert rec4.status == "ok", rec4.error
    assert [c.url for c in fake.calls[n_calls:]] == [fake.key("GET", API, _params(fc[sup]))[1]]

    results = {r.check: r for r in run_checks(store)}
    red = {n: r.items for n, r in results.items() if r.status in ("failed", "warn")}
    assert red == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


@pytest.mark.slow  # real store + runner
def test_openalex_bounded_window(tmp_path: pathlib.Path, oa_env: Any) -> None:
    """The dev plan's window (``window_days: 3, window_end_days_ago: 14``): the publication window
    is ``[today-17, today-14]`` (``to_publication_date`` sent), ``max_records`` caps a pull to a
    ``partial`` row, and a re-run asks the same bounded filter again (a bounded window's end is a
    publication date, not the day of the pass, nor does a later open-ended run inherit it); no filter
    carries a created axis (D582)."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date()
    floor, to = ((today - dt.timedelta(days=n)).isoformat() for n in (17, 14))
    f = _filters(floor=floor, to=to)
    sup, rlhf = "group:superalignment", "group:rlhf"
    assert f"to_publication_date:{to}" in f[rlhf]
    fake.add(API, _params(f[sup]), json=_page([W1, W2], "c2"))  # cap 2 reached: partial
    fake.add(API, _params(f[rlhf]), json=_page([W3]))
    # relist_after_h 0: the capped query is asked again at once (D226; by default not within 12 h)
    opts = {"queries": [sup, rlhf], "window_days": 3, "window_end_days_ago": 14, "max_records": 2,
            "relist_after_h": 0}
    for _ in range(2):
        rec = run_line(store, _oa_plan(**opts), "collect.openalex")
        assert rec.status == "ok", rec.error
        # D492: the capped query leaves each run short, reason `limit`
        assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]
    # run 2 asks the partial query only: the ok one is covered through its window end (D226)
    assert all("from_created_date" not in c.url for c in fake.calls) and len(fake.calls) == 3
    cov = {openalex.query_label(r["query"]): r for r in _view(store, "coverage.openalex")}
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    assert (cov[rlhf]["status"], cov[rlhf]["window_start_at"], cov[rlhf]["window_end_at"]) == (
        "ok", d(floor), d(to))
    assert (cov[sup]["status"], cov[sup]["is_exhausted"], cov[sup]["window_end_at"],
            cov[sup]["attempts"]) == ("partial", False, d(floor), 2)
    # the open-ended query over the same floor is another coverage key: the bounded ok window's
    # end (a publication date) must not become its watermark (a false completeness claim)
    fake.add(API, _params(_filters(floor=floor)[rlhf]), json=_page([W3]))
    rec = run_line(store, _oa_plan(queries=[rlhf], **{"from": floor}), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert fake.calls[-1].url == fake.key("GET", API, _params(_filters(floor=floor)[rlhf]))[1]  # from the floor


@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_openalex_floor_moved_back_pulls_only_the_gap(tmp_path: pathlib.Path, oa_env: Any) -> None:
    """B7a: the key leaves the floor out, so after a first window from 2025-10-01 a run with
    `from: 2024-11-30` the same day sends one bounded request for the hole (publication
    2024-11-30..2025-09-30, no look-back) and writes [2024-11-30, 2025-10-01]; the covered tail
    is not pulled again; a third run sends nothing."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    today = dt.datetime.now(dt.UTC).date().isoformat()
    rlhf = "group:rlhf"
    first = _filters(floor="2025-10-01")[rlhf]
    gap = _filters(floor="2024-11-30", to="2025-09-30")[rlhf]
    fake.add(API, _params(first), json=_page([W1]))
    fake.add(API, _params(gap), json=_page([W2]))
    rec1 = run_line(store, _oa_plan(queries=[rlhf], **{"from": "2025-10-01"}), "collect.openalex")
    assert rec1.status == "ok", rec1.error
    assert [c.url for c in fake.calls] == [fake.urls()[0]]
    rec2 = run_line(store, _oa_plan(queries=[rlhf], **{"from": "2024-11-30"}), "collect.openalex")
    assert rec2.status == "ok", rec2.error
    assert [c.url for c in fake.calls[1:]] == [fake.urls()[1]]  # the gap only
    assert rec2.counts["holes"] == 1 and rec2.counts["minted_ids"] == 1
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    cov = sorted((r["window_start_at"], r["window_end_at"], r["status"])
                 for r in _view(store, "coverage.openalex"))
    assert cov == [(d("2024-11-30"), d("2025-10-01"), "ok"), (d("2025-10-01"), d(today), "ok")]
    rec3 = run_line(store, _oa_plan(queries=[rlhf], **{"from": "2024-11-30"}), "collect.openalex")
    assert rec3.status == "ok" and len(fake.calls) == 2 and rec3.counts["already_covered"] == 1


_PLAN_429 = {"error": "Plan upgrade required",
             "message": "The \"from_created_date:2026-10-01\" filter requires a Premium, Institutional, or "
                        "Partner plan. See https://openalex.org/pricing for details."}


@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_openalex_plan_refusal_halts_the_run_permanently(tmp_path: pathlib.Path, oa_env: Any,
                                                         monkeypatch: Any) -> None:
    """D583: run 2 resumes both queries; OpenAlex answers the first with the plan-upgrade 429. One
    request, no sleep, no further query; the window is failed_permanent[provider_plan] (charged, the
    message in its `error`, no retry_after); the run ends ok, scope incomplete (`halted`), the message
    in the record's `error`. A 403 instead halts as failed_transient[provider_auth], uncharged."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, slept = oa_env
    store = tmp_path / "store"
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    sup, rlhf = "group:superalignment", "group:rlhf"
    f0 = _filters()
    for q in (sup, rlhf):
        fake.add(API, _params(f0[q]), json=_page([W1]))
    opts = {"queries": [sup, rlhf], "from": FLOOR, "lookback_days": 3}
    rec1 = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec1.status == "ok" and rec1.scope_complete and rec1.error is None
    t0 = dt.datetime.now(dt.UTC)
    resumed = _filters(floor=(t0.date() - dt.timedelta(days=3)).isoformat())

    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=1))
    fake.inject(API, _params(resumed[sup]), status=429, json=_PLAN_429, times=5)
    n = len(fake.calls)
    rec2 = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec2.status == "ok" and rec2.scope_complete is False and rec2.error_class is None
    assert rec2.error == "provider_plan: HTTP 429: " + _PLAN_429["message"]
    assert len(fake.calls) == n + 1 and [s for s in slept if s >= 1] == []
    c = rec2.counts
    assert (c["halted"], c["refused.provider_plan"], c["windows_failed_permanent.provider_plan"]) == (1, 1, 1)
    assert c["budget_exhausted"] == 0  # a refusal, not the day's budget
    assert "halted" in incomplete_reasons(c)
    rows = [r for r in _view(store, "coverage.openalex") if r["run_id"] == rec2.run_id]
    assert [(openalex.query_label(r["query"]), r["status"], r["error_class"], r["window_start_at"],
             r["window_end_at"], r["attempts"], r["retry_after"]) for r in rows] == [
        (sup, "failed_permanent", "provider_plan", d(FLOOR), d(FLOOR), 1, None)]
    assert rows[0]["error"] == "HTTP 429: " + _PLAN_429["message"]

    # a 403 on the next day's first request: halted as failed_transient[provider_auth], uncharged
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=2))
    fake.faults.clear()
    fake.inject(API, _params(resumed[sup]), status=403, json={"error": "Forbidden", "message": "Invalid API key"})
    n = len(fake.calls)
    rec3 = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec3.status == "ok" and rec3.scope_complete is False
    assert rec3.error == "provider_auth: HTTP 403: Invalid API key" and len(fake.calls) == n + 1
    (row,) = [r for r in _view(store, "coverage.openalex") if r["run_id"] == rec3.run_id]
    assert (row["status"], row["error_class"], row["attempts"], row["retry_after"]) == (
        "failed_transient", "provider_auth", 1, None)  # run 2's charge, not a second one


@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_openalex_lookback_capped_asks_again_from_the_watermark(tmp_path: pathlib.Path, oa_env: Any,
                                                                 monkeypatch: Any) -> None:
    """D582: when the cap fills inside the look-back (cursor order is not date order), its hits are
    kept and the query is asked again from the watermark in the same window, so the look-back cannot
    keep a query capped (`lookback_capped`); the window is [watermark, today]. A negative
    `lookback_days` is refused."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    rlhf = "group:rlhf"
    t0 = dt.datetime.now(dt.UTC)
    today, tomorrow = t0.date().isoformat(), (t0.date() + dt.timedelta(days=1)).isoformat()
    fake.add(API, _params(_filters()[rlhf]), json=_page([W1]))
    opts = {"queries": [rlhf], "from": FLOOR, "lookback_days": 3, "max_records": 2}
    assert run_line(store, _oa_plan(**opts), "collect.openalex").scope_complete
    look = _filters(floor=(t0.date() - dt.timedelta(days=3)).isoformat())[rlhf]
    fake.add(API, _params(look), json=_page([W1, W2], "c2"))  # the cap (2) fills: truncated
    fake.add(API, _params(_filters(floor=today)[rlhf]), json=_page([W3]))  # from the watermark
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=1))
    n = len(fake.calls)
    rec = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec.status == "ok" and rec.scope_complete, rec.error
    assert len(fake.calls) == n + 2 and rec.counts["lookback_capped"] == 1
    assert rec.counts["minted_ids"] == 2  # W2 (from the look-back) and W3
    (row,) = [r for r in _view(store, "coverage.openalex") if r["run_id"] == rec.run_id]
    assert (row["status"], row["window_start_at"], row["window_end_at"], row["seen"], row["kept"]) == (
        "ok", d(today), d(tomorrow), 3, 2)
    bad = run_line(store, _oa_plan(**{**opts, "lookback_days": -1}), "collect.openalex")
    assert bad.status == "failed" and "lookback_days" in (bad.error or "")
