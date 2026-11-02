"""Tests for ``collect.cards`` — the per-lab model-card / policy-doc watcher (10-design §5.1
watch kind).

Pure-unit tests cover the ported v1 id/slug helpers and the url key (the DOC-ID + url_key dedup keys —
golden-ported from v1's ``pipeline/tests/test_watch_docs_ids.py``), the include/exclude/policy/
text-slug candidate filtering, and — crucially — that ``collect.cards`` mints the IDENTICAL
``(channel, external_id)`` that ``collect.v1_import``'s ``parse_stem`` produces for the same doc
(so a freshly-watched card and a v1-imported one dedup).

Stage tests (section D) drive ``collect.cards`` through ``stack.stage_runner.run_stage`` on a
temporary store over ``tests/fakes/http.py`` at the harness transport seam (no network). v3's
DB-side assertions are now on the emitted ``sources`` / ``coverage`` rows: per-source identity
(``url`` key, ``doc_type``, listing date), one honest coverage row per listing surface (a blocked
listing is ``failed_*``, never an empty ``ok``), a re-run that mints nothing and writes no row for
an unchanged listing, the github-org watermark bootstrap + increment, the NEW-source cap
(``partial``), GitHub 403 rate-limit handling, and V1/V2 over the store.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
from typing import Any

import pytest

from stack.build import incomplete_reasons
from stack import stage_runner
from stack.checks.verify import run_checks
from stack.collect import listing
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.store.chain import read_view, resolve
from stack.urls import url_key
from tests.fakes.http import FakeHttp

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"
WORKFLOWS_DIR = REPO / "workflows"


def _load_module(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, WORKFLOWS_DIR / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cards = _load_module("collect/cards/main.py", "_cards_under_test")


# ─────────────────────────────────────────────────────────────────────────────
# (A) pure-unit: ported id/slug/norm goldens (from v1 test_watch_docs_ids.py + _self_test)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "kind, org_slug, url, want",
    [
        ("systemcard", "anthropic", "https://anthropic.com/claude-opus-4-8-system-card",
         "systemcard:anthropic-claude-opus-4-8"),
        ("systemcard", "openai", "https://deploymentsafety.openai.com/gpt-5-5",
         "systemcard:openai-gpt-5-5"),
        ("systemcard", "google-deepmind",
         "https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-2-5-Pro-Model-Card.pdf",
         "systemcard:google-deepmind-gemini-2-5-pro"),
        ("systemcard", "xai", "https://data.x.ai/2025-08-20-grok-4-model-card.pdf",
         "systemcard:xai-grok-4"),
    ],
)
def test_url_to_doc_id_goldens(kind: str, org_slug: str, url: str, want: str) -> None:
    slug, _date = listing.slug_from_url(url)
    got = listing.make_doc_id(kind, org_slug, slug)
    assert got == want
    assert "--" not in got


def test_slug_from_url_extracts_date_prefix() -> None:
    assert listing.slug_from_url("https://data.x.ai/2025-08-20-grok-4-model-card.pdf") == (
        "grok-4-model-card", "2025-08-20")
    assert listing.slug_from_url("https://a.com/claude-opus-4-8-system-card") == (
        "claude-opus-4-8-system-card", "")


def test_make_doc_id_never_double_dashes_and_strips_noise() -> None:
    got = listing.make_doc_id("systemcard", "google-deepmind", "gemini-3-pro-model-card")
    assert got == "systemcard:google-deepmind-gemini-3-pro"
    assert "--" not in got
    got = listing.make_doc_id("policy", "openai", "openai-model-card")
    assert got.startswith("policy:openai-")


def test_slug_from_repo_path_goldens() -> None:
    assert listing.slug_from_repo_path("models/llama3_2/MODEL_CARD_VISION.md") == "llama-3-2-vision"
    assert listing.slug_from_repo_path("models/llama4/MODEL_CARD.md") == "llama-4"
    assert listing.slug_from_repo_path("Llama-Guard3/11B-vision/MODEL_CARD.md") == (
        "llama-guard-3-11b-vision")


def test_url_key_is_a_stable_dedup_key() -> None:
    # v3's norm_url, retired for stack.urls.url_key (track U, round 4): www. now drops too
    variants = [
        "https://anthropic.com/news/rsp/",
        "http://ANTHROPIC.com/news/rsp",
        "https://anthropic.com/news/rsp?utm=x#top",
        "https://www.anthropic.com/news/rsp",
    ]
    assert {url_key(v) for v in variants} == {"https://anthropic.com/news/rsp"}
    # raw.githubusercontent refs collapse across main/HEAD/sha.
    assert url_key(
        "https://raw.githubusercontent.com/meta-llama/llama-models/main/models/llama2/MODEL_CARD.md"
    ) == url_key(
        "https://raw.githubusercontent.com/meta-llama/llama-models/HEAD/models/llama2/MODEL_CARD.md"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (B) pure-unit: external_id ↔ collect.v1_import parse_stem CONSISTENCY
# collect.v1_import retired at T1.8 (D40); the ids its ``parse_stem`` returned for these docs are
# pinned below (computed at 9312c8c), so the corpus ids stay continuous with the v1 import.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "kind, org_slug, url, v1_ext",
    [
        ("systemcard", "anthropic",
         "https://www.anthropic.com/claude-opus-4-1-system-card", "anthropic-claude-opus-4-1"),
        ("systemcard", "google-deepmind",
         "https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-2-5-Pro.pdf",
         "google-deepmind-gemini-2-5-pro"),
        ("policy", "alibaba", "https://x/qwen-safety-policy", "alibaba-qwen-safety-policy"),
    ],
)
def test_external_id_matches_v1_import_routing(
    kind: str, org_slug: str, url: str, v1_ext: str
) -> None:
    """A freshly-watched card and a v1-imported one must land on the SAME external_id."""
    slug, _ = listing.slug_from_url(url)
    doc_id = listing.make_doc_id(kind, org_slug, slug)
    assert listing.external_id_of(doc_id) == v1_ext


# ─────────────────────────────────────────────────────────────────────────────
# (C) pure-unit: include/exclude/policy/text-slug candidate filtering + classification
# ─────────────────────────────────────────────────────────────────────────────
def test_candidate_include_exclude_and_hex_slug() -> None:
    Surface = cards.Surface
    spec = Surface(key="k", method="html-index", include_re=r"\.pdf", exclude_re=r"#page=")
    assert listing.candidate_of(spec, "https://x/a.pdf", "A Card") is not None
    assert listing.candidate_of(spec, "https://x/a.pdf#page=3", "A Card") is None  # exclude_re
    assert listing.candidate_of(spec, "https://x/a.html", "A Card") is None       # include_re miss
    # opaque hex CDN slug: needs usable anchor text, else dropped.
    hexspec = Surface(key="k", method="html-index", include_re=r"\.pdf")
    c = listing.candidate_of(hexspec, "https://cdn/0123456789abcdef.pdf", "Grok 4 Model Card")
    assert c is not None and c.slug == "grok-4-model-card"
    assert listing.candidate_of(hexspec, "https://cdn/0123456789abcdef.pdf", "download") is None


def test_opaque_cdn_slug_uses_anchor_text_and_strips_hash_tail() -> None:
    """Hashed CDN names (media.x.ai) take identity from the listing label; dated data.x.ai
    filenames and readable+hash framework names stay URL-derived (hash tail stripped only)."""
    spec = cards.Surface(key="k", method="html-index", include_re=r"\.pdf")
    c46 = listing.candidate_of(
        spec, "https://media.x.ai/v1/website/card-4p6-4cd2dc57.pdf", "Grok 4.6")
    assert c46 is not None and c46.slug == "grok-4-6"
    c45 = listing.candidate_of(
        spec, "https://media.x.ai/v1/website/4p5-5184fdf9.pdf", "Grok 4.5")
    assert c45 is not None and c45.slug == "grok-4-5"
    assert listing.candidate_of(
        spec, "https://media.x.ai/v1/website/card-4p6-4cd2dc57.pdf", "download") is None
    dated = listing.candidate_of(
        spec, "https://data.x.ai/2025-08-20-grok-4-model-card.pdf", "Grok 4")
    assert dated is not None and dated.slug == "grok-4-model-card"
    assert dated.date_hint == "2025-08-20"
    fw = listing.candidate_of(
        spec,
        "https://media.x.ai/v1/website/"
        "xai-frontier-artificial-intelligence-framework-30-june-2026-99c40684.pdf",
        "Frontier AI Framework",
    )
    assert fw is not None
    assert fw.slug == "xai-frontier-artificial-intelligence-framework-30-june-2026"
    assert listing._is_opaque_slug("card-4p6-4cd2dc57")
    assert listing._is_opaque_slug("4p5-5184fdf9")
    assert not listing._is_opaque_slug("grok-4-model-card")


def test_xai_media_ids_match_v1_import_routing() -> None:
    for slug, ext in (("grok-4-5", "xai-grok-4-5"), ("grok-4-6", "xai-grok-4-6")):
        doc_id = listing.make_doc_id("systemcard", "xai", slug)
        assert listing.external_id_of(doc_id) == ext  # == v1_import.parse_stem's id at 9312c8c


def test_scan_pinned_single_and_multi() -> None:
    """Single-url Meta path unchanged; pins= emits one candidate per entry, slug = stripped id."""
    single = listing.Surface(
        key="meta-frontier", method="pinned-pdf",
        urls=["https://ai.meta.com/static-resource/meta-frontier-ai-framework/"],
        pinned_id="policy:meta-frontier-ai-framework",
        title_prefix="Meta Frontier AI Framework",
    )
    scan = listing._scan_pinned(single)
    assert scan.exhausted == "complete" and len(scan.candidates) == 1
    assert scan.candidates[0].slug == "meta-frontier-ai-framework"
    assert scan.note["external_id"] == "meta-frontier-ai-framework"

    multi = listing.Surface(
        key="xai-archive-cards", method="pinned-pdf",
        pins=[
            {"url": "https://data.x.ai/a.pdf", "pinned_id": "systemcard:xai-grok-4-1",
             "title": "Grok 4.1 Model Card", "date": "2025-11-17"},
            {"url": "https://data.x.ai/b.pdf", "pinned_id": "systemcard:xai-grok-4-20",
             "title": "Grok 4.20 Model Card", "date": "2026-04-07"},
        ],
    )
    scan = listing._scan_pinned(multi)
    assert scan.exhausted == "complete" and [c.slug for c in scan.candidates] == [
        "xai-grok-4-1", "xai-grok-4-20"]
    assert scan.candidates[0].date_hint == "2025-11-17"
    assert "external_id" not in scan.note  # multi-pin: identity lives on the candidate
    assert scan.note["pinned"] == 2


def test_candidate_text_slug_template_and_exclude_text() -> None:
    Surface = cards.Surface
    spec = Surface(
        key="anthropic-rsp", method="html-index", kind="policy", include_re=r"\.pdf",
        exclude_text_re=r"(?i)redline", text_slug_re=r"(?i)version\s+(\d+)\.(\d+)",
        text_slug_template="rsp-v{0}-{1}",
    )
    c = listing.candidate_of(spec, "https://a/rsp.pdf", "Version 3.4")
    assert c is not None and c.slug == "rsp-v3-4"
    assert listing.candidate_of(spec, "https://a/rsp-redline.pdf", "Redline Version 3.4") is None
    assert listing.candidate_of(spec, "https://a/rsp.pdf", "no version here") is None  # text_slug miss


def test_classify_kind_and_doc_type_split() -> None:
    Surface = cards.Surface
    spec = Surface(key="k", method="html-index", doc_type="system_card",
                   policy_re=r"(?i)preparedness|framework")
    assert cards._classify_kind(spec, "https://x/gpt-card", "GPT Card") == "systemcard"
    assert cards._classify_kind(spec, "https://x/preparedness-framework", "") == "policy"
    assert cards._doc_type_for(spec, "systemcard") == "system_card"
    assert cards._doc_type_for(spec, "policy") == "framework_policy"
    mc = Surface(key="k", method="github-tree", doc_type="model_card")
    assert cards._doc_type_for(mc, "systemcard") == "model_card"


# ─────────────────────────────────────────────────────────────────────────────
# (D) the stage on the harness: run_stage on a temp store over tests/fakes/http.py
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_CARDS = "https://www.anthropic.com/system-cards"
RSP = "https://www.anthropic.com/rsp-updates"
GH = "https://api.github.com"
REPOS = (f"{GH}/orgs/deepseek-ai/repos", {"per_page": "100", "sort": "created"})
TREE = {"recursive": "1"}
INDEX_V1 = (
    '<html><body><div>'
    '<article><a href="/claude-opus-4-8-system-card">Claude Opus 4.8 System Card</a>'
    '<span>June 3, 2026</span></article>'
    '<article><a href="https://www.anthropic.com/claude-haiku-5-system-card">Read</a></article>'
    '<a href="/news/other-post">not a card</a>'
    '</div></body></html>')
_ENVS = ("STACK_CARDS_LIMIT", "STACK_CARDS_FULL", "STACK_CARDS_ONLY", "STACK_CARDS_FIXTURES",
         "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES")


def _repo(name: str, created: str) -> dict[str, Any]:
    return {"name": name, "created_at": created}


def _tree(*paths: str) -> dict[str, Any]:
    return {"tree": [{"type": "blob", "path": p} for p in paths], "truncated": False}


def _cards_plan(**options: Any) -> Plan:
    opts = {"only": ["anthropic_cards", "deepseek_cards"], **options}
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.cards", "prior": ["sources", "coverage"], "options": opts}]})


@pytest.fixture
def cards_stage(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    """``(store, fake)``: the runner loads this test's module object (seams pinned), the fake
    serves the transport."""
    for var in _ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(stage_runner, "load_stage_module", lambda name: cards)
    monkeypatch.setattr(cards, "_sleep", lambda s: None)
    fake = FakeHttp()
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    return tmp_path / "store", fake


def _go(store: pathlib.Path, **options: Any) -> Any:
    rec = run_line(store, _cards_plan(**options), "collect.cards")
    assert rec.status == "ok", rec.error
    return rec


def _sources(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    return {r["identity"]["external_id"]: r
            for r in read_view(resolve(store, sdir("sources.cards"))).to_pylist()}


def _coverage(store: pathlib.Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in read_view(resolve(store, sdir("coverage.cards"))).to_pylist():
        out.setdefault(r["query"], []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["window_end_at"])
    return out


@pytest.mark.slow  # real store + runner
def test_cards_stage_rows_coverage_rerun_and_github_watermark(cards_stage: Any) -> None:
    store, fake = cards_stage
    fake.add(SYSTEM_CARDS, body=INDEX_V1)
    fake.add(RSP, status=403, body="<html>Forbidden</html>")  # a block page is not an empty index
    fake.add(*REPOS, json=[_repo("DeepSeek-V3", "2025-01-01T00:00:00Z"),
                           _repo("DeepSeek-R1", "2025-02-01T00:00:00Z")])

    # run 1: starts both chains; github-org bootstraps its watermark (no history sweep)
    rec1 = _go(store)
    assert rec1.counts["minted_ids"] == 2 and rec1.counts["sources_written"] == 2
    src = _sources(store)
    assert sorted(src) == ["anthropic-claude-haiku-5", "anthropic-claude-opus-4-8"]
    opus = src["anthropic-claude-opus-4-8"]
    url = "https://www.anthropic.com/claude-opus-4-8-system-card"
    assert (opus["state"], opus["identity"]["channel"], opus["identity"]["canonical_url"]) == (
        "observed", "anthropic_cards", url)
    # the url key drops www. (stack.urls.url_key, url-normalisation.md §2); canonical_url keeps the listed form
    assert opus["identity"]["keys"] == [{"kind": "url", "value": "https://anthropic.com/claude-opus-4-8-system-card"}]
    md = opus["metadata"]
    assert (md["title"], md["doc_type"], md["published_at"], md["license_class"]) == (
        "Claude Opus 4.8 System Card", "system_card", dt.datetime(2026, 6, 3, tzinfo=dt.UTC),
        "public_web")
    assert opus["inband"]["entries"] == []  # listing only: no item content at collect time
    haiku = src["anthropic-claude-haiku-5"]["metadata"]
    assert (haiku["title"], haiku["published_at"]) == ("Claude Haiku 5 System Card", None)
    cov = _coverage(store)
    (idx,) = cov["anthropic-systemcards"]
    assert (idx["status"], idx["seen"], idx["kept"], idx["is_exhausted"]) == ("ok", 2, 2, True)
    (rsp,) = cov["anthropic-rsp"]
    assert (rsp["status"], rsp["error_class"], rsp["seen"]) == ("failed_permanent", "blocked", 0)
    (gh,) = cov["deepseek-github"]
    newest = dt.datetime(2025, 2, 1, tzinfo=dt.UTC)
    assert (gh["status"], gh["window_start_at"], gh["window_end_at"]) == ("ok", newest, newest)
    assert not [c for c in fake.calls if "/git/trees/" in c.url]  # bootstrap: no tree reads

    # run 2: same index (unchanged: no rows), one repo newer than the watermark with a paper PDF
    fake.add(*REPOS, json=[_repo("DeepSeek-V3", "2025-01-01T00:00:00Z"),
                           _repo("DeepSeek-R1", "2025-02-01T00:00:00Z"),
                           _repo("DeepSeek-V4", "2026-04-01T00:00:00Z")])
    fake.add(f"{GH}/repos/deepseek-ai/DeepSeek-V4/git/trees/HEAD", TREE,
             json=_tree("DeepSeek_V4.pdf", "docs/inner.pdf", "README.md"))
    rec2 = _go(store, relist_after_h=0)  # re-list at once (default: not within 12 h, D226)
    assert rec2.counts["minted_ids"] == 1 and rec2.counts["sources_unchanged"] == 2
    src2 = _sources(store)
    assert {k: r["source_id"] for k, r in src2.items() if k in src} == {
        k: r["source_id"] for k, r in src.items()}
    v4 = src2["deepseek-v-4"]
    assert (v4["identity"]["channel"], v4["metadata"]["doc_type"]) == ("deepseek_cards", "system_card")
    assert v4["identity"]["canonical_url"] == \
        "https://raw.githubusercontent.com/deepseek-ai/DeepSeek-V4/HEAD/DeepSeek_V4.pdf"
    # the url key is the ref-collapsed url_key (main/HEAD/sha are one document; owner/repo lower case)
    assert v4["identity"]["keys"] == [{"kind": "url", "value":
        "https://raw.githubusercontent.com/deepseek-ai/deepseek-v4/-/DeepSeek_V4.pdf"}]
    trees = [c.url for c in fake.calls if "/git/trees/" in c.url]
    assert trees == [f"{GH}/repos/deepseek-ai/DeepSeek-V4/git/trees/HEAD?recursive=1"]
    gh2 = _coverage(store)["deepseek-github"][-1]
    assert (gh2["status"], gh2["window_start_at"], gh2["window_end_at"], gh2["kept"]) == (
        "ok", newest, dt.datetime(2026, 4, 1, tzinfo=dt.UTC), 1)

    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


def test_cards_new_source_cap_is_partial_and_github_rate_limit_fails(cards_stage: Any) -> None:
    store, fake = cards_stage
    fake.add(SYSTEM_CARDS, body=INDEX_V1)
    fake.add(RSP, body="<html><body></body></html>")  # read, and genuinely empty: ok
    fake.add(*REPOS, status=403, json={"message": "API rate limit exceeded for 1.2.3.4."})
    rec = _go(store, max_urls=1)
    assert rec.counts["minted_ids"] == 1 and rec.counts["items_capped"] == 1
    cov = _coverage(store)
    (idx,) = cov["anthropic-systemcards"]
    assert (idx["status"], idx["is_exhausted"], idx["seen"], idx["kept"]) == ("partial", False, 2, 1)
    # D492: the max_urls cap is a cap (`limit`), beside the 403'd surface (`windows_failed`)
    assert rec.scope_complete is False and rec.counts["windows_capped"] == 1
    assert incomplete_reasons(rec.counts) == ["windows_failed", "limit"]
    assert cov["anthropic-rsp"][0]["status"] == "ok"
    (gh,) = cov["deepseek-github"]
    assert (gh["status"], gh["error_class"]) == ("failed_transient", "rate_limit")


@pytest.mark.slow  # real store + runner
def test_cards_full_sweeps_github_history_25_repos_a_run_and_resumes(cards_stage: Any) -> None:
    """D555: with ``full`` a github-org surface sweeps its history oldest first, ``max_repos`` (25) a run.
    Before, ``full`` ignored the watermark and a capped scan was ``partial``: every run re-read the same
    25 oldest repos and the run stayed capped (build reason ``limit``) for good. Now run 1 banks the 25
    oldest as an ``ok`` window from the oldest repo and ends scope-incomplete (``limit``); run 2 resumes
    after them at once (a banked window is not exhausted, so ``relist_after_h`` does not hold it), reads the
    last 5 and completes; run 3 is not due (D226); run 4, relisted, reads only the listing."""
    store, fake = cards_stage
    repos = [_repo(f"r{i:02d}", f"2024-01-{i:02d}T00:00:00Z") for i in range(1, 31)]
    fake.add(*REPOS, json=list(reversed(repos)))  # the API: newest first
    for r in repos:
        fake.add(f"{GH}/repos/deepseek-ai/{r['name']}/git/trees/HEAD", TREE, json=_tree(f"{r['name']}.pdf"))
    trees = lambda: [c.url for c in fake.calls if "/git/trees/" in c.url]
    day = lambda i: dt.datetime(2024, 1, i, tzinfo=dt.UTC)

    rec1 = run_line(store, _cards_plan(only=["deepseek_cards"], full=True), "collect.cards")
    assert rec1.status == "ok", rec1.error
    assert len(trees()) == 25 and rec1.counts["minted_ids"] == 25
    assert rec1.scope_complete is False and rec1.counts["surfaces_capped"] == 1
    assert incomplete_reasons(rec1.counts) == ["limit"]
    (gh1,) = _coverage(store)["deepseek-github"]
    assert (gh1["status"], gh1["window_start_at"], gh1["window_end_at"]) == ("ok", day(1), day(25))

    n = len(trees())
    rec2 = run_line(store, _cards_plan(only=["deepseek_cards"], full=True), "collect.cards")
    assert rec2.status == "ok", rec2.error
    assert trees()[n:] == [f"{GH}/repos/deepseek-ai/r{i}/git/trees/HEAD?recursive=1" for i in range(26, 31)]
    assert rec2.scope_complete is True and rec2.counts["minted_ids"] == 5 and len(_sources(store)) == 30
    gh2 = _coverage(store)["deepseek-github"][-1]
    assert (gh2["status"], gh2["window_start_at"], gh2["window_end_at"]) == ("ok", day(25), day(30))

    assert (gh1["is_exhausted"], gh2["is_exhausted"]) == (False, True)  # banked, then caught up
    n, lists = len(trees()), len(fake.calls)
    rec3 = run_line(store, _cards_plan(only=["deepseek_cards"], full=True), "collect.cards")
    assert rec3.status == "ok" and rec3.scope_complete is True
    assert len(fake.calls) == lists and rec3.counts["not_due"] == 1  # caught up: D226's relist rule again
    rec4 = run_line(store, _cards_plan(only=["deepseek_cards"], full=True, relist_after_h=0), "collect.cards")
    assert rec4.status == "ok" and len(trees()) == n and len(fake.calls) == lists + 1  # the listing only


# ─────────────────────────────────────────────────────────────────────────────
# (E) titles and doc_type (D759): multi-block anchors, slug titles, model cards; known sources keep theirs
# ─────────────────────────────────────────────────────────────────────────────
DS = "https://deploymentsafety.openai.com/"
OPENAI_SITEMAP = "https://openai.com/sitemap.xml"
#: deploymentsafety.openai.com's card list (2026-10-30): a dateline, the title and a description in one anchor
DS_INDEX = (
    '<html><body><div>'
    '<a href="/gpt-6-october"><span aria-hidden="true"></span><div><div><span>Oct 07, 2026</span></div></div>'
    '<div><p class="text-h4">GPT-6 Sol and GPT-6 Luna: October 2026 update</p></div></a>'
    '<a href="/gpt-6-1-sol"><div><span>Sep 29, 2026</span></div><div><p>Addendum to GPT-6 Astra System Card: '
    'GPT-6.1 Sol</p><p>Safety evaluations and safeguards for GPT-6.1 Sol.</p></div></a>'
    '</div></body></html>')
SITEMAP = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://openai.com/index/gpt-oss-model-card/</loc></url>'
           '<url><loc>https://openai.com/index/gpt-5-1-codex-max-system-card/</loc></url></urlset>')


def test_anchor_blocks_title_and_unchanged_text() -> None:
    (a, b) = listing.parse_anchors(DS_INDEX, DS)
    assert a == ("https://deploymentsafety.openai.com/gpt-6-october",
                 "Oct 07, 2026GPT-6 Sol and GPT-6 Luna: October 2026 update",  # the run-together text, as before
                 ["Oct 07, 2026", "GPT-6 Sol and GPT-6 Luna: October 2026 update"])
    assert listing.parse_links(DS_INDEX, DS) == [(a[0], a[1]), (b[0], b[1])]
    assert listing.title_from_blocks(b[2]) == "Addendum to GPT-6 Astra System Card: GPT-6.1 Sol"
    assert listing.title_from_blocks(["Read system card"]) == ""  # one block: the text is the title
    assert listing.title_from_blocks(["7 October 2026", "2026-10-07"]) == ""  # dates only
    spec = cards.WATCH_SPECS["openai_cards"].surfaces[0]
    c = listing.candidate_of(spec, b[0], b[1], blocks=b[2])
    assert c is not None and c.slug == "gpt-6-1-sol" and c.text == b[1]
    assert cards._title_hint(c, spec, "system_card") == "Addendum to GPT-6 Astra System Card: GPT-6.1 Sol"
    assert cards._legacy_title_hint(c, spec, "system_card") == b[1]


@pytest.mark.parametrize("slug, doc_word, want", [
    ("claude-opus-4-8-system-card", "System Card", "Claude Opus 4.8 System Card"),
    ("claude-fable-5-1-mythos-5-1-system-card", "System Card", "Claude Fable 5.1 Mythos 5.1 System Card"),
    ("gemini-2-5-computer-use", "Model Card", "Gemini 2.5 Computer Use Model Card"),
    ("llama-guard-3-11b-vision", "Model Card", "Llama Guard 3 11B Vision Model Card"),
    ("gpt-oss-model-card", "System Card", "Gpt Oss Model Card"),
    ("gpt-5-system-card-sensitive-conversations", "System Card", "Gpt 5 System Card Sensitive Conversations"),
    ("veo-3", "Model Card", "Veo 3 Model Card"),
])
def test_slug_title(slug: str, doc_word: str, want: str) -> None:
    assert cards.slug_title(slug, doc_word) == want


def test_model_card_items_of_a_system_card_surface() -> None:
    spec = cards.WATCH_SPECS["xai_cards"].surfaces[0]
    mc = listing.Candidate(url="https://data.x.ai/2025-08-20-grok-4-model-card.pdf", slug="grok-4", text="Grok 4")
    sc = listing.Candidate(url="https://media.x.ai/v1/website/4p5-5184fdf9.pdf", slug="grok-4-5", text="Grok 4.5")
    assert cards._doc_type_of(spec, "systemcard", mc) == "model_card"
    assert cards._doc_type_of(spec, "systemcard", sc) == "system_card"
    assert cards._doc_type_of(spec, "policy", mc) == "framework_policy"


@pytest.mark.slow  # real store + runner
def test_known_sources_keep_their_titles_and_doc_type_new_ones_get_the_fixes(
        cards_stage: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A title or doc_type the code before D759 stored is kept (a changed field re-triages the document); a new source
    gets the title block, the dotted slug title and model_card."""
    store, fake = cards_stage
    fake.add(DS, body=DS_INDEX)
    fake.add(OPENAI_SITEMAP, body=SITEMAP)
    with monkeypatch.context() as mp:  # run 1 as the code before D759
        mp.setattr(cards, "_title_hint", cards._legacy_title_hint)
        mp.setattr(cards, "_doc_type_of", lambda spec, kind, cand: cards._doc_type_for(spec, kind))
        _go(store, only=["openai_cards"])
    before = {k: (r["source_id"], r["metadata"]["title"], r["metadata"]["doc_type"]) for k, r in _sources(store).items()}
    assert before["openai-gpt-6-october"][1] == "Oct 07, 2026GPT-6 Sol and GPT-6 Luna: October 2026 update"
    assert before["openai-gpt-oss"][1:] == ("Gpt Oss Model Card System Card", "system_card")
    assert before["openai-gpt-5-1-codex-max"][1] == "Gpt 5 1 Codex Max System Card"

    fake.add(DS, body=DS_INDEX.replace('</div></body>', '<a href="/gpt-live-1-1"><div><span>Oct 15, 2026</span></div>'
                                                      '<div><p>GPT-Live-1.1 system card</p></div></a></div></body>'))
    fake.add(OPENAI_SITEMAP, body=SITEMAP.replace(
        "</urlset>", "<url><loc>https://openai.com/index/gpt-5-3-codex-model-card/</loc></url></urlset>"))
    rec = _go(store, only=["openai_cards"], relist_after_h=0)
    assert rec.counts["minted_ids"] == 2 and rec.counts["sources_unchanged"] == 4
    after = {k: (r["source_id"], r["metadata"]["title"], r["metadata"]["doc_type"]) for k, r in _sources(store).items()}
    assert {k: after[k] for k in before} == before
    assert after["openai-gpt-live-1-1"][1:] == ("GPT-Live-1.1 system card", "system_card")
    assert after["openai-gpt-5-3-codex"][1:] == ("Gpt 5.3 Codex Model Card", "model_card")
