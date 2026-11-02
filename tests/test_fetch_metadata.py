"""Tests for ``workflows/fetch/metadata/main.py`` — no longer a stage: the shared code of the two scholarly
lookups ``collect.openalex-lookup`` and ``collect.s2-lookup`` (D92; their stage tests are
``tests/test_collect_lookup.py``). Offline, pure-unit only: the shared
usability predicate (>=100 chars), the legacy env names and the lookup batches (S2, OpenAlex) over a
fake HTTP seam; plus the v3-ported normalizer cases of :mod:`stack.scholarly_records` (venue_type
priority, external-id routing, the OpenAlex Jan-1 date quirk, per-API normalization goldens),
imported from there. v3's DB-backed ``run`` tests retired with the database (T1.8,
T4.5); the tests of v3's test-only provider helpers (abstract waterfall, venue acronyms, self-skip,
OAI withdrawal and versions, circuit breaker, Zenodo siblings, Crossref/EPMC/Atom fetchers) went with
that code. The module is loaded by PATH, as the lookups load it.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

from stack import scholarly_records as sr
from stack.collect.base import full_url
from tests.fakes.http import FakeHttp

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"
WORKFLOWS_DIR = REPO / "workflows"

_spec = importlib.util.spec_from_file_location(
    "_fetch_metadata_under_test", WORKFLOWS_DIR / "fetch" / "metadata" / "main.py"
)
assert _spec is not None and _spec.loader is not None
sch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sch)


# ═════════════════════════════════════════════════════════════════════════════
# (A) shared usability predicate — the ≥100-char const, one definition
# ═════════════════════════════════════════════════════════════════════════════
def test_usability_predicate_shared_const() -> None:
    assert sch.MIN_USABLE_ABSTRACT_CHARS == 100
    assert not sch.is_usable_abstract("x" * 99)
    assert sch.is_usable_abstract("x" * 100)
    assert not sch.is_usable_abstract("   " + "x" * 90 + "   ")  # stripped, still short
    assert not sch.is_usable_abstract("")




# ═════════════════════════════════════════════════════════════════════════════
# (B) venue_type priority (ported)
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "kw, expect",
    [
        ({"is_arxiv": True}, "preprint"),
        ({"is_arxiv": True, "oa_source_type": "journal"}, "preprint"),  # arXiv override wins
        ({"oa_source_type": "journal"}, "journal"),
        ({"oa_source_type": "conference"}, "conference"),
        ({"oa_source_type": "repository"}, "preprint"),
        ({"oa_source_type": "book series"}, "book"),
        ({"s2_types": ["Conference"]}, "conference"),
        ({"s2_types": ["JournalArticle"]}, "journal"),
        ({"oa_source_type": "journal", "s2_types": ["Conference"]}, "journal"),  # OA outranks S2
        ({"crossref_type": "proceedings-article"}, "conference"),
        ({"crossref_type": "journal-article"}, "journal"),
        ({"rec_type": "preprint"}, "preprint"),
        ({"rec_type": "post"}, "blog"),
        ({"rec_type": "conference-paper"}, "conference"),  # OpenAlex proceedings type (collected since D653)
        ({}, ""),  # genuinely unknown
    ],
)
def test_venue_type_priority(kw: dict[str, Any], expect: str) -> None:
    assert sr.venue_type_from(**kw) == expect






# ═════════════════════════════════════════════════════════════════════════════
# (C) external-id routing
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "channel, ext, kind, arxiv_id, doi, pmid",
    [
        ("arxiv", "2409.15747", "arxiv", "2409.15747", "10.48550/arxiv.2409.15747", ""),
        ("openalex", "10.48550/arXiv.2409.15747", "arxiv", "2409.15747",
         "10.48550/arxiv.2409.15747", ""),
        ("arxiv", "cs/9901001", "arxiv", "cs/9901001", "10.48550/arxiv.cs/9901001", ""),
        ("manual", "10.1234/foo.bar", "doi", "", "10.1234/foo.bar", ""),
        ("europepmc", "pmid:12345678", "pmid", "", "", "12345678"),
        ("lesswrong", "abc123", "other", "", "", ""),
        ("openalex", "10.5281/zenodo.42", "doi", "", "10.5281/zenodo.42", ""),
        # REGRESSION (live smoke): a plain DOI containing a `dddd.ddddd` substring
        # (`3442188.3445922` -> `2188.34459`) must NOT be misread as arXiv.
        ("manual", "10.1145/3442188.3445922", "doi", "", "10.1145/3442188.3445922", ""),
        ("manual", "10.1038/s41586-021-03819-2", "doi", "", "10.1038/s41586-021-03819-2", ""),
    ],
)
def test_classify_external_id(
    channel: str, ext: str, kind: str, arxiv_id: str, doi: str, pmid: str
) -> None:
    e = sr.classify_external_id(channel, ext)
    assert (e.kind, e.arxiv_id, e.doi, e.pmid) == (kind, arxiv_id, doi, pmid)


def test_arxiv_id_anchored_never_matches_doi_substring() -> None:
    """`_arxiv_id_of` is anchored: only an arXiv DOI, an arxiv.org/abs URL, or a bare id (whole
    string) — never a `dddd.ddddd` substring inside an unrelated DOI (the live-smoke bug)."""
    assert sr._arxiv_id_of("10.1145/3442188.3445922") == ""
    assert sr._arxiv_id_of("10.1038/s41586-021-03819-2") == ""
    assert sr._arxiv_id_of("http://arxiv.org/abs/2409.15747v2") == "2409.15747"
    assert sr._arxiv_id_of("10.48550/arXiv.2409.15747") == "2409.15747"
    assert sr._arxiv_id_of("2409.15747") == "2409.15747"
    assert sr._arxiv_id_of("cs/9901001") == "cs/9901001"


def test_zenodo_classification() -> None:
    e = sr.classify_external_id("openalex", "10.5281/zenodo.20420288")
    assert e.is_zenodo and e.zenodo_id == "20420288"
    assert not sr.classify_external_id("manual", "10.1/notzenodo").is_zenodo




# ═════════════════════════════════════════════════════════════════════════════
# (D) date quirks (ported)
# ═════════════════════════════════════════════════════════════════════════════
def test_openalex_jan1_date_quirk() -> None:
    assert sr.month_from("2024-01-01", "openalex") is None     # fabricated Jan-1 -> unknown
    assert sr.month_from("2024-01-01", "crossref") == 1        # trusted
    assert sr.month_from("2024-06-12", "openalex") == 6
    assert sr.pub_date_from("", 2024) == "2024-01-01"          # year-only fallback
    assert sr.pub_date_from("2024-06-12T09:00:00Z") == "2024-06-12"
    assert sr.pub_date_from("", None) is None


# ═════════════════════════════════════════════════════════════════════════════
# (E) per-API normalization goldens
# ═════════════════════════════════════════════════════════════════════════════
def test_normalize_openalex_golden() -> None:
    work = {
        "title": "Attention Is All You Need", "publication_year": 2017,
        "publication_date": "2017-06-12", "type": "article", "cited_by_count": 90000,
        "open_access": {"is_oa": True},
        "primary_location": {"landing_page_url": "https://x",
                             "source": {"display_name": "NeurIPS", "type": "conference"}},
        "authorships": [{"author": {"display_name": "A Vaswani"}},
                        {"author": {"display_name": "N Shazeer"}}],
        "biblio": {"volume": "30", "first_page": "1", "last_page": "11"},
        "abstract_inverted_index": {"The": [0], "model": [1]},
    }
    frag = sr.normalize_openalex(work, is_arxiv=False)
    assert frag["title"] == "Attention Is All You Need"
    assert frag["authors"] == ["A Vaswani", "N Shazeer"]
    assert frag["pub_date"] == "2017-06-12"
    assert frag["venue"] == "NeurIPS" and frag["venue_type"] == "conference"
    assert frag["citations"] == 90000 and frag["oa"] is True
    assert frag["pages"] == "1–11" and frag["volume"] == "30"
    assert frag["abstract"] == "The model"


def test_normalize_openalex_arxiv_forces_preprint() -> None:
    work = {"title": "T", "publication_date": "2024-01-01", "type": "article",
            "primary_location": {"source": {"display_name": "some repo", "type": "repository"}}}
    frag = sr.normalize_openalex(work, is_arxiv=True)
    assert frag["venue"] == "arXiv" and frag["venue_type"] == "preprint"
    assert "volume" not in frag  # biblio suppressed for arXiv


def test_normalize_s2_golden() -> None:
    paper = {
        "title": "Deep Paper", "year": 2024, "publicationDate": "2024-09-24",
        "abstract": "a" * 130, "publicationVenue": {"name": "ICLR", "type": "conference"},
        "publicationTypes": ["Conference"], "externalIds": {"DOI": "10.1/iclr"},
        "authors": [{"name": "Jane Doe"}],
    }
    frag = sr.normalize_s2(paper)
    assert frag["title"] == "Deep Paper" and frag["authors"] == ["Jane Doe"]


# ═════════════════════════════════════════════════════════════════════════════
# (I) no sleeps; the legacy env names (v3's DB-backed run tests retired at T1.8/T4.5)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: Any) -> None:
    monkeypatch.setattr(sch.time, "sleep", lambda *_a, **_k: None)


@pytest.mark.parametrize(
    "legacy,fix",
    [
        # the message names only variables the v4 code reads: STACK_METADATA_LIMIT
        # (max_records), STACK_MAILTO; REFRESH/ONLY have no v4 equivalent
        ("STACK_SCHOLARLY_LIMIT", "STACK_SCHOLARLY_LIMIT -> STACK_METADATA_LIMIT"),
        ("STACK_SCHOLARLY_MAILTO", "STACK_SCHOLARLY_MAILTO -> STACK_MAILTO"),
        ("STACK_SCHOLARLY_REFRESH", "STACK_SCHOLARLY_REFRESH: no v4 equivalent, unset it"),
        ("STACK_SCHOLARLY_ONLY", "STACK_SCHOLARLY_ONLY: no v4 equivalent, unset it"),
    ],
)
def test_legacy_environment_names_fail_loudly(monkeypatch: Any, legacy: str, fix: str) -> None:
    monkeypatch.setenv(legacy, "legacy-value")
    with pytest.raises(ValueError) as err:
        sch._reject_legacy_env()
    assert fix in str(err.value)


# ═════════════════════════════════════════════════════════════════════════════
# (K) OA-PDF capture — the D4-legitimate waterfall (openalex -> crossref -> s2)
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
# (L) Latency + the metered OpenAlex: one probe per run, and the wall defers rather than kills
# ═════════════════════════════════════════════════════════════════════════════


def _seam(monkeypatch: Any, fake: FakeHttp) -> None:
    """Route the lookup's request seam to ``fake`` (one request per call; the retry ladder and the
    429 halt are the harness's, tested in test_collect_base.py)."""
    monkeypatch.setattr(sch, "http_fetch", lambda method, url, *, params=None, headers=None,
                        body=None: fake(method, full_url(url, params), headers=headers or {},
                                        body=body, timeout=1.0, max_bytes=1 << 20))


@pytest.mark.parametrize("status,error", [(429, "rate_limit"), (500, "http_5xx"),
                                          (503, "http_5xx"), (403, "provider_auth")])
def test_provider_outages_are_transient(monkeypatch: Any, status: int, error: str) -> None:
    fake = FakeHttp()
    fake.add(sch.S2_BATCH_URL, {"fields": sch.S2_FIELDS}, status=status, method="POST")
    _seam(monkeypatch, fake)
    batch = sch.s2_chunk(["DOI:10.1000/example"])
    assert (batch.hits, batch.error) == ({}, (error, False))  # transient: the keys stay due


def test_s2_null_result_is_a_real_miss(monkeypatch: Any) -> None:
    fake = FakeHttp()
    fake.add(sch.S2_BATCH_URL, {"fields": sch.S2_FIELDS}, json=[None, {"paperId": "a" * 40}],
             method="POST")
    _seam(monkeypatch, fake)
    batch = sch.s2_chunk(["DOI:10.1000/x", "ARXIV:2401.00001"])
    assert batch.error is None and list(batch.hits) == ["ARXIV:2401.00001"]


def test_too_large_batches_are_halved(monkeypatch: Any) -> None:
    fake = FakeHttp()
    url, by = sch.OPENALEX_URL, "doi"

    def params(vals: list[str]) -> dict[str, Any]:
        return {"filter": f"{by}:" + "|".join(vals), "select": sch.OPENALEX_SELECT,
                "per_page": sch.OPENALEX_BATCH}
    fake.add(url, params(["10.1000/a", "10.1000/b"]), status=414)
    fake.add(url, params(["10.1000/a"]), json={"results": [{"doi": "https://doi.org/10.1000/A"}]})
    fake.add(url, params(["10.1000/b"]), json={"results": []})
    _seam(monkeypatch, fake)
    # a `select:` option (D524) travels into both halves
    batch = sch.openalex_chunk(["10.1000/a", "10.1000/b"], by=by, mailto=None, select=sch.OPENALEX_SELECT)
    assert batch.error is None and list(batch.hits) == ["10.1000/a"]  # matched in key normal form
    assert len(fake.calls) == 3


@pytest.mark.parametrize("provider", ["openalex", "s2"])
def test_malformed_batches_are_unavailable(monkeypatch: Any, provider: str) -> None:
    fake = FakeHttp()
    fake.add(sch.OPENALEX_URL, {"filter": "doi:10.8/a", "per_page": sch.OPENALEX_BATCH}, json={})
    fake.add(sch.S2_BATCH_URL, {"fields": sch.S2_FIELDS}, json=[{}, {}], method="POST")  # misaligned
    _seam(monkeypatch, fake)
    batch = (sch.openalex_chunk(["10.8/a"], by="doi", mailto=None) if provider == "openalex"
             else sch.s2_chunk(["DOI:10.8/a"]))
    assert (batch.hits, batch.error) == ({}, ("parse_error", True))


@pytest.mark.parametrize("provider,kind,value,expect", [
    ("openalex", "arxiv", "2610.00006", ("doi", "10.48550/arxiv.2610.00006")),  # its DataCite DOI
    ("openalex", "pmid", "123", ("pmid", "123")),
    ("s2", "arxiv", "2610.00006", ("ids", "ARXIV:2610.00006")),
    ("s2", "s2", "a" * 40, ("ids", "a" * 40)),
])
def test_lookup_request_forms(provider: str, kind: str, value: str, expect: tuple[str, str]) -> None:
    assert sch._request_key(sch.PROVIDERS[provider], sch.Due(kind, value)) == expect
