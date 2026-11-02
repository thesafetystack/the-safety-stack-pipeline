"""Pure cases of the arXiv lane (``workflows/acquire/lanes/arxiv.py``, ADAPTed from v3's
``fetch.arxiv``) and of :mod:`stack.arxiv` and the history's ``REVISION`` helpers
(:mod:`workflows.acquire.history`).

Offline throughout: the HTTP layer is a fake getter that synthesises Atom feeds from the
``id_list`` in the request URL and routes every other URL through a table; the pacer's clock is
injected. The lane's runner-level cases (a v2 moves the pointer, the halt, withdrawal) are in
``tests/test_acquire_lanes.py``.

Coverage: batch shaping and pacing of the Atom sweep; the garbled-page retry and the 429 halt;
the version gate in both directions; every outcome the lane emits is in the §2.6 vocabulary;
validators replay only against the versioned URL; the markup rungs (kept in ``stack.arxiv``);
the ``REVISION`` list an Atom answer gives (a real withdrawal entry included: no marker parsed).
"""

from __future__ import annotations

import ast
import pathlib
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import pytest

import stack.http as stack_http
from stack.arxiv import parse_atom_versions
from stack.schemas.vocab import ERROR_CLASSES
from workflows.acquire.history import atom_revisions, merge_revisions
from workflows.acquire.lanes import arxiv as arx
from workflows.acquire.lanes.http import HttpSession

REPO = pathlib.Path(__file__).resolve().parent.parent
fh = stack_http

ATOM = "https://export.arxiv.org/api/query"
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 20_000
HTML_BYTES = (
    b"<!doctype html><html><body><article>" + b"<p>LaTeXML rendering</p>" * 2000
    + b"</article></body></html>"
)


# ─────────────────────────────────────────────────────────────────────────────
# fakes
# ─────────────────────────────────────────────────────────────────────────────
def _resp(status: int | None, body: bytes = b"", *, headers: dict[str, str] | None = None,
          error: str | None = None, url: str = "http://x.test") -> Any:
    return fh.HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()},
                           body, url, error)


def _feed(entries: dict[str, tuple[int, str, str]], ids: list[str]) -> bytes:
    """An Atom feed for the ids the request asked about — one entry per KNOWN id."""
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>ArXiv Query</title>']
    for aid in ids:
        if aid not in entries:
            continue
        v, pub, upd = entries[aid]
        body.append(
            f"<entry><id>http://arxiv.org/abs/{aid}v{v}</id>"
            f"<published>{pub}T00:00:00Z</published><updated>{upd}T00:00:00Z</updated>"
            f"<title>paper {aid}</title></entry>"
        )
    body.append("</feed>")
    return "\n".join(body).encode()


class AtomFake:
    """Serves the Atom API from a table, everything else from a route map. ``garble`` makes
    the first N Atom responses a well-formed interstitial (arXiv's throttling shape); ``status``
    answers every Atom request with that HTTP status instead."""

    def __init__(self, versions: dict[str, tuple[int, str, str]] | None = None,
                 routes: dict[str, Any] | None = None, garble: int = 0, status: int | None = None) -> None:
        self.versions = versions or {}
        self.routes = routes or {}
        self.garble = garble
        self.status = status
        self.calls: list[str] = []
        self.atom_batches: list[list[str]] = []

    def __call__(self, url: str, *, headers: Any, timeout: float, max_bytes: int) -> Any:
        self.calls.append(url)
        if url.startswith(ATOM):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            ids = q["id_list"][0].split(",")
            self.atom_batches.append(ids)
            if self.status is not None:
                return _resp(self.status, url=url)
            if self.garble > 0:
                self.garble -= 1
                return _resp(200, b"<html><body>403 Forbidden</body></html>", url=url)
            return _resp(200, _feed(self.versions, ids), url=url)
        r = self.routes.get(url)
        if r is None:
            return _resp(404, url=url)
        return r


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _sweep(getter: AtomFake, ids: list[str], clock: Clock | None = None) -> Any:
    clock = clock or Clock()
    session = HttpSession(getter, sleep=clock.sleep, monotonic=clock.monotonic)
    return arx.sweep_versions(lambda u: session.fetch_url(u, "arxiv"), ATOM, ids, sleep=clock.sleep)


PDF1 = "https://arxiv.org/pdf/2401.00001v2"


# ═════════════════════════════════════════════════════════════════════════════
# (C) the Atom sweep — batch shape, pacing, garbled pages, the halt
# ═════════════════════════════════════════════════════════════════════════════
def test_atom_sweep_batches_and_paces() -> None:
    ids = [f"2401.{i:05d}" for i in range(250)]
    getter = AtomFake({i: (1, "2024-01-01", "2024-01-01") for i in ids})
    clock = Clock()
    sweep = _sweep(getter, ids, clock)
    assert [len(b) for b in getter.atom_batches] == [100, 100, 50]
    assert sweep.requests == 3 and sweep.retries == 0 and not sweep.failed and sweep.halted is None
    assert len(sweep.versions) == 250
    # rate_rps 0.33 (channels.yaml) → ~3 s between requests; the first is free.
    assert len(clock.sleeps) == 2 and all(abs(s - 1 / 0.33) < 0.01 for s in clock.sleeps)
    assert all(u.startswith(ATOM) for u in getter.calls)


def test_atom_sweep_retries_a_garbled_page_once() -> None:
    """One interstitial → one retry after 10 s, then the feed; two → the batch is ``failed``
    (an outage must be seen, never read as "these ids have no entries")."""
    ids = ["2401.00001", "2401.00002"]
    clock = Clock()
    sweep = _sweep(AtomFake({i: (2, "2024-01-01", "2024-02-01") for i in ids}, garble=1), ids, clock)
    assert (sweep.requests, sweep.retries, sweep.failed) == (2, 1, set())
    assert sweep.versions["2401.00001"].version == 2 and arx.ATOM_RETRY_DELAY_S in clock.sleeps
    sweep = _sweep(AtomFake({i: (2, "2024-01-01", "2024-02-01") for i in ids}, garble=2), ids)
    assert sweep.failed == set(ids) and not sweep.versions and sweep.halted is None


@pytest.mark.parametrize("status", [429, 503])
def test_atom_sweep_halts_on_slow_down(status: int) -> None:
    """A 429/503 after the session's own retries halts the sweep: no further batch is asked and
    every unasked id is ``failed``."""
    ids = [f"2401.{i:05d}" for i in range(150)]
    getter = AtomFake(status=status)
    sweep = _sweep(getter, ids)
    assert sweep.halted == f"arXiv answered HTTP {status}" and sweep.failed == set(ids)
    assert len(getter.atom_batches) == 1 + stack_http.MAX_HTTP_RETRIES  # one batch, retried, then stop


def test_version_gate_both_directions() -> None:
    atom = arx.AtomVersion("2401.00001", 2, "2024-01-01", "2024-02-01")
    assert arx.version_gate(atom, set(), withdrawn=False, atom_failed=False) == ("never_fetched", 2)
    assert arx.version_gate(atom, {"v1"}, withdrawn=False, atom_failed=False) == ("version_changed", 2)
    assert arx.version_gate(atom, {"v1", "v2"}, withdrawn=False, atom_failed=False) == ("version_held", 2)
    assert arx.version_gate(atom, {"v1"}, withdrawn=True, atom_failed=False) == ("withdrawn", None)
    assert arx.version_gate(None, set(), withdrawn=False, atom_failed=True) == ("atom_unavailable", None)
    assert arx.version_gate(None, set(), withdrawn=False, atom_failed=False) == ("atom_unknown", None)


def test_arxiv_id_of_a_member() -> None:
    assert arx.arxiv_id_of({"key_kind": "arxiv", "key_value": "2401.00001"}) == "2401.00001"
    assert arx.arxiv_id_of({"key_value": None, "canonical_url": "https://arxiv.org/abs/2401.00001v3"}) == "2401.00001"
    assert arx.arxiv_id_of({"key_value": "W123", "canonical_url": None}) == ""
    # The DOI form arXiv-channel members carry on the real store, and OpenAlex/S2 members that the
    # amended D11 sends to this lane (T3.6-f3a).
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": "10.48550/arxiv.2609.36750"}) == "2609.36750"
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": "10.48550/ARXIV.2609.36750"}) == "2609.36750"
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": "10.48550/arXiv.hep-th/9901001"}) == "hep-th/9901001"


#: Publisher DOIs whose suffix holds a ``YYMM.NNNNN``-shaped run of digits: the two the 2026-10 fetch
#: smoke met (ACM), and IEEE and Springer shapes.
_DOTTED_DOIS = ["10.1145/3847353.3847499", "10.1145/3746027.3754512", "10.1109/TPAMI.2025.3561234",
                "10.1109/ICASSP49660.2025.10889012", "10.1007/978-3-032-0412.34567_8"]


@pytest.mark.parametrize("doi", _DOTTED_DOIS)
def test_arxiv_id_of_never_reads_an_id_out_of_a_publisher_doi(doi: str) -> None:
    """D580: a member joined by a publisher DOI names no arXiv id, neither by its key nor by a
    ``doi.org`` or publisher URL; untyped, the value is not read as an id either."""
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": doi}) == ""
    assert arx.arxiv_id_of({"key_value": doi}) == ""
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": doi, "canonical_url": f"https://doi.org/{doi}"}) == ""
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": doi,
                            "canonical_url": f"https://dl.acm.org/doi/{doi}"}) == ""


def test_arxiv_id_of_prefers_the_documents_arxiv_key_over_a_doi() -> None:
    """D580: a DOI member of a document whose ``identity.keys`` hold an arXiv id (``KEY_ARXIV_ID``)
    gets that id; arXiv's own DOI still parses, as key or as URL; an arXiv URL is the last resort."""
    doi = _DOTTED_DOIS[0]
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": doi, arx.KEY_ARXIV_ID: "2401.01234"}) == "2401.01234"
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": doi, arx.KEY_ARXIV_ID: "2401.01234",
                            "canonical_url": "https://arxiv.org/abs/2502.00002"}) == "2401.01234"
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": "10.48550/arXiv.2401.01234"}) == "2401.01234"
    assert arx.arxiv_id_of({"key_value": "10.48550/arXiv.2401.01234"}) == "2401.01234"
    assert arx.arxiv_id_of({"key_kind": "doi", "key_value": doi,
                            "canonical_url": "https://doi.org/10.48550/arXiv.2401.01234"}) == "2401.01234"
    assert arx.arxiv_id_of({"key_kind": "url", "key_value": "https://example.org/p",
                            "canonical_url": "https://arxiv.org/pdf/2401.01234v2"}) == "2401.01234"


def test_the_history_targets_a_doi_members_html_role_by_the_documents_arxiv_key() -> None:
    """D580, the history step: an ``html`` role of a member joined by an ACM DOI is swept under the
    arXiv id in ``identity.keys``, not under ``7353.38474``; without such a key it is not swept."""
    def row(keys: list[dict[str, str]]) -> dict[str, Any]:
        doi = _DOTTED_DOIS[0]
        member = {"source_id": "s1", "is_live": True, "channel": "openalex", "key_kind": "doi", "key_value": doi,
                  "canonical_url": f"https://doi.org/{doi}"}
        return {"identity": {"is_masked": False, "members": [member],
                             "keys": [{"kind": "doi", "value": doi, "source_id": "s1"}, *keys]},
                "content": {"roles": [{"source_id": "s1", "lane": "arxiv", "content_role": "html",
                                       "status": "not_attempted"}]}}
    keyed = row([{"kind": "arxiv", "value": "2609.00011", "source_id": "s1"}])
    assert list(arx.ArxivLane()._html_targets([keyed, row([])], None)) == ["2609.00011"]  # type: ignore[arg-type]


# ═════════════════════════════════════════════════════════════════════════════
# (D) the lane's emitted outcomes
# ═════════════════════════════════════════════════════════════════════════════
def test_every_emit_site_outcome_is_in_the_vocabulary() -> None:
    """Every literal ``FetchOutcome(status, error_class, …)`` in the lane uses a §2.6 pair."""
    tree = ast.parse((REPO / "workflows" / "acquire" / "lanes" / "arxiv.py").read_text())
    pairs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FetchOutcome":
            args = [a.value if isinstance(a, ast.Constant) else ... for a in node.args[:2]]
            if args and args[0] is not ...:
                cls = args[1] if len(args) > 1 else None
                if cls is not ...:  # a computed class (transient_class) is checked by its own table
                    pairs.add((args[0], cls))
    assert len(pairs) >= 6, pairs
    for status, cls in pairs:
        if status in ("ok", "empty"):
            assert cls is None, (status, cls)
        else:
            assert cls in ERROR_CLASSES[status], (status, cls)


# ═════════════════════════════════════════════════════════════════════════════
# (F) the markup rungs, ported from fetch.http with the channel — now over stack.arxiv
# ═════════════════════════════════════════════════════════════════════════════
import stack.arxiv as sa

# arXiv's LaTeXML rendering. Real pages measured 282KB–1.7MB; padded past ARXIV_HTML_MIN_BYTES
# so the size floor is not what makes these tests pass.
ARXIV_HTML = (
    b'<!DOCTYPE html><html lang="en"><head><title>A paper</title></head><body>'
    b'<article class="ltx_document"><p class="ltx_p">Body.</p></article>'
    b"<!-- " + b"pad " * 6000 + b" --></body></html>"
)
# arXiv's miss page, verbatim in the part that matters. Served with status 404 — a REAL 404.
ARXIV_NO_HTML = (
    b"<!doctype html><html><body><h1>No HTML for '2303.16200'</h1>"
    b"<p>HTML is not available for the source. This could be due to the source files not being "
    b"HTML, LaTeX, or a conversion failure.</p></body></html>"
)


def test_markup_is_acceptable_rejects_the_soft_404_three_ways() -> None:
    """arXiv reports a missing rendering as status 404 with a complete, real HTML page. Minting
    that as the paper would be silently catastrophic, so no single signal is trusted."""
    assert sa.markup_is_acceptable(ARXIV_HTML, "html", 200) is None
    assert sa.markup_is_acceptable(ARXIV_NO_HTML, "html", 404) == "http_404"
    assert sa.markup_is_acceptable(b"", "html", 304) == "http_304"
    assert sa.markup_is_acceptable(b"<html>tiny</html>", "html", 200) == "too_small"
    big_miss = ARXIV_NO_HTML + b"<!-- " + b"pad " * 6000 + b" -->"
    assert len(big_miss) > sa.ARXIV_HTML_MIN_BYTES
    assert sa.markup_is_acceptable(big_miss, "html", 200) == "soft_404"
    assert sa.markup_is_acceptable(PDF_BYTES, "html", 200) == "not_html"
    assert sa.markup_is_acceptable(ARXIV_HTML, "xml", 200) == "not_xml"


def test_urls_pin_a_version_only_when_given_one() -> None:
    """Bare → the CURRENT version (fetch.http's historical behaviour, and what a caller with no
    version in hand must get); with a version → pinned, so bytes and recorded version agree."""
    assert sa.arxiv_html_url("2401.00001") == "https://arxiv.org/html/2401.00001"
    assert sa.arxiv_html_url("2401.00001", 3) == "https://arxiv.org/html/2401.00001v3"
    assert sa.arxiv_pdf_url("2401.00001") == "https://arxiv.org/pdf/2401.00001"
    assert sa.arxiv_pdf_url("2401.00001", 3) == "https://arxiv.org/pdf/2401.00001v3"


def test_normalize_arxiv_id_and_version() -> None:
    assert sa.normalize_arxiv_id("http://arxiv.org/abs/2401.00001v2") == "2401.00001"
    assert sa.normalize_arxiv_id("arXiv:2401.00001") == "2401.00001"
    assert sa.normalize_arxiv_id("https://arxiv.org/pdf/2401.00001v7.pdf") == "2401.00001"
    assert sa.normalize_arxiv_id("hep-th/9901001v2") == "hep-th/9901001"
    assert sa.normalize_arxiv_id("not an id") == ""
    assert sa.arxiv_version_of("http://arxiv.org/abs/2401.00001v2") == 2
    assert sa.arxiv_version_of("2401.00001") is None


def test_parse_atom_versions_rejects_an_interstitial() -> None:
    """A well-formed non-feed body (a proxy's 403 page) is None, never an empty answer."""
    assert sa.parse_atom_versions("<html><body>403 Forbidden</body></html>") is None
    assert sa.parse_atom_versions("<<garbage") is None
    feed = _feed({"2401.00001": (3, "2024-01-01", "2024-06-01")}, ["2401.00001", "2401.00002"])
    got = sa.parse_atom_versions(feed.decode())
    assert got is not None and set(got) == {"2401.00001"}
    assert got["2401.00001"] == sa.AtomVersion("2401.00001", 3, "2024-01-01", "2024-06-01")


def test_validators_replay_only_against_the_versioned_url() -> None:
    """An ETag ``http`` stored for the bare URL, or one from an earlier version, is never
    replayed, nor one whose bytes are not held: a 304 must honestly mean "this version,
    unchanged"."""
    role = {"etag": '"e"', "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT", "validator_url": PDF1}
    assert arx.conditional_headers(role, PDF1, backed=True) == {
        "If-None-Match": '"e"', "If-Modified-Since": "Mon, 01 Jan 2024 00:00:00 GMT"}
    assert arx.conditional_headers(role, PDF1, backed=False) == {}
    assert arx.conditional_headers(role, "https://arxiv.org/pdf/2401.00001v3", backed=True) == {}
    assert arx.conditional_headers(role, "https://arxiv.org/pdf/2401.00001", backed=True) == {}
    bare = {"etag": '"e"', "validator_url": "https://arxiv.org/pdf/2401.00001"}
    assert arx.conditional_headers(bare, PDF1, backed=True) == {}


# ═════════════════════════════════════════════════════════════════════════════
# (G) the REVISION list of an Atom answer (workflows/acquire/history.py; the OAI-PMH listing went
#     on 2026-10-13)
# ═════════════════════════════════════════════════════════════════════════════
#: A withdrawal as the Atom ``id_list`` API served it on 2026-10-13 (live probe of 2609.39891,
#: entry kept verbatim but the abstract cut and one author): ``<id>`` names the withdrawal
#: version, the title, the abstract and the PDF link are the paper's, the only trace is the free-
#: text ``<arxiv:comment>``. Its versioned PDF answered HTTP 404 (text/html, "This version of
#: 2609.39891v2 has been withdrawn and is unavailable").
WITHDRAWN_FEED = (
    '<?xml version=\'1.0\' encoding=\'UTF-8\'?>\n<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
    'xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">'
    '<id>https://arxiv.org/api/TkW8abyjirsLRkW9ffpsvJv22Ys</id><updated>2026-10-13T13:07:18Z</updated>'
    '<entry><id>http://arxiv.org/abs/2609.39891v2</id>'
    '<title>Improved existence time for a Boussinesq system</title><updated>2026-10-02T01:57:55Z</updated>'
    '<link href="https://arxiv.org/abs/2609.39891v2" rel="alternate" type="text/html"/>'
    '<link href="https://arxiv.org/pdf/2609.39891v2" rel="related" type="application/pdf" title="pdf"/>'
    '<summary>We consider a strongly dispersive Boussinesq-type system arising as a model for the propagation '
    'of weakly nonlinear water waves.</summary>'
    '<category term="math.AP" scheme="http://arxiv.org/schemas/atom"/><published>2026-09-30T14:58:39Z</published>'
    '<arxiv:comment>Withdrawn by the authors because this version was submitted before both authors had '
    'approved the final manuscript</arxiv:comment><arxiv:primary_category term="math.AP"/>'
    '<author><name>Achenef Tesfahun</name></author></entry></feed>')


def test_atom_revisions_list_v1_and_the_current_version() -> None:
    """``vN`` dated by ``<updated>``, ``v1`` by ``<published>`` when N > 1 (aware UTC midnights,
    no title); a v1 answer is one entry; an undated feed gives ``None`` dates. The real withdrawal
    entry parses as an ordinary v2 (no withdrawal marker is read from the Atom answer)."""
    (got,) = parse_atom_versions(WITHDRAWN_FEED).values()
    assert (got.arxiv_id, got.version, got.published, got.updated) == ("2609.39891", 2, "2026-09-30", "2026-10-02")
    assert atom_revisions(got) == [
        {"revision": "v1", "published_at": datetime(2026, 9, 30, tzinfo=UTC), "title": None},
        {"revision": "v2", "published_at": datetime(2026, 10, 2, tzinfo=UTC), "title": None}]
    assert set(vars(got)) == {"arxiv_id", "version", "published", "updated"}
    assert atom_revisions(arx.AtomVersion("2401.00001", 1, "2024-01-01", "2024-01-01")) == [
        {"revision": "v1", "published_at": datetime(2024, 1, 1, tzinfo=UTC), "title": None}]
    assert atom_revisions(arx.AtomVersion("2401.00001", 3, "", "")) == [
        {"revision": "v1", "published_at": None, "title": None},
        {"revision": "v3", "published_at": None, "title": None}]


def test_withdrawal_notice_reads_arxivs_page_for_the_asked_version() -> None:
    """arXiv's 404 page for a withdrawn version (both saved live pages, 2026-10-13) is a
    withdrawal of exactly the version it names; another version, another status, the "No HTML"
    soft 404 or a plain 404 are not."""
    pages = REPO / "tests" / "fixtures" / "arxiv_withdrawn"
    for aid, v in (("2609.39891", 2), ("0910.4008", 2)):
        body = (pages / f"pdf-{aid}v{v}.html").read_bytes()
        assert arx.withdrawal_notice(_resp(404, body), aid, v)
        assert arx.withdrawal_notice(_resp(410, body), aid, v)
        assert not arx.withdrawal_notice(_resp(404, body), aid, v + 1)
        assert not arx.withdrawal_notice(_resp(404, body), "2609.3989", v)
        assert not arx.withdrawal_notice(_resp(200, body), aid, v)
    assert not arx.withdrawal_notice(_resp(404, b"<h1>No HTML for '2609.39891v2'</h1>"), "2609.39891", 2)
    assert not arx.withdrawal_notice(_resp(404, b"withdrawn"), "2609.39891", 2)
    assert not arx.withdrawal_notice(_resp(None, error="timeout"), "2609.39891", 2)
    assert not arx.withdrawal_notice(None, "2609.39891", 2)


def test_merge_revisions_never_forgets_a_version() -> None:
    t = datetime(2024, 1, 1, tzinfo=UTC)
    prior = [{"revision": "v1", "published_at": t, "title": "old"}, {"revision": "v10", "published_at": None,
                                                                     "title": None}]
    new = [{"revision": "v2", "published_at": t, "title": None}, {"revision": "v1", "published_at": None,
                                                                  "title": None}]
    assert [r["revision"] for r in merge_revisions(prior, new)] == ["v1", "v2", "v10"]
    assert merge_revisions(prior, new)[0] == {"revision": "v1", "published_at": t, "title": "old"}
