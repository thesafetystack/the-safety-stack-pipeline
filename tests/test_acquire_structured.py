"""Structured text first (round 5, T-RUN B8): JATS (role ``xml``) and arXiv's LaTeXML HTML (role
``html``) behind the acquire line's ``options.structured``, the PDF as the fallback.

Pure cases: the option's parsing and the pin (unchanged with the option off), the pointer's ranking
(a structured text stands in for its own source's PDF of the same revision once its text is ``ok``),
convert's and quality's scope, the structure markers, the docling-free LaTeXML path on a fixture
(``tests/fixtures/convert/latexml-sample.html``; JATS: ``jats-sample.xml``). Runner cases (slow):
the arXiv ``html`` episode on a fake transport (available, missing, the 180-day "no HTML" memory, a
new version, the option off), the JATS and LaTeXML texts converted in the real child next to the PDF
with both quality verdicts and the comparison counts, and the fallback when the structured text
fails the quality gate. No network: an unrouted URL is a ``LookupError`` in the fake.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from stack.arxiv import ARXIV_HTML_MIN_BYTES
from stack.conversion import ladder
from stack.conversion.quality import structure_markers
from stack.plan import Plan
from stack.stage_runner import StageError
from tests.fakes.http import FakeHttp
from tests.fakes.lanes import FakeFetchpdf
from tests.synth.rows import blob, sid
from workflows.acquire.convert import (
    CONFIGS,
    convert_pin,
    guard_pin,
    pin_document,
    pin_of,
    to_convert,
)
from workflows.acquire.point import Structured, point, structured_of
from workflows.acquire.quality import judged_snapshots

Row = dict[str, Any]
FIXTURES = Path(__file__).parent / "fixtures" / "convert"
LATEXML = (FIXTURES / "latexml-sample.html").read_bytes()
JATS = (FIXTURES / "jats-sample.xml").read_bytes()
PIN = "p" * 64
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


# ---------------------------------------------------------------- the option and the pin
def test_structured_option_parsing() -> None:
    assert structured_of({}) == Structured() and not Structured().on and Structured().roles == frozenset()
    assert structured_of({"structured": {"jats": True}}).roles == {"xml"}
    assert structured_of({"structured": {"arxiv_html": True}}).roles == {"html"}
    assert structured_of({"structured": {"jats": True, "arxiv_html": True}}).roles == {"xml", "html"}
    for bad in ({"jats": "yes"}, {"html": True}, ["jats"]):
        with pytest.raises(ValueError, match="options.structured"):
            structured_of({"structured": bad})


def acq(convert: str | None = "pymupdf4llm-v1") -> Any:
    data: dict[str, Any] = {"plan": "t", "schema_version": 2, "stages": []}
    if convert is not None:
        data["acquire"] = {"convert": {"config": convert}}
    return Plan.model_validate(data).acquire


def test_pin_unchanged_with_the_option_off() -> None:
    """Off (absent, or both false) the pin document is exactly the old one: no ``structured`` key,
    the config's own tools only. On adds the flags and fetchpdf/lxml, and each setting is its own
    pin (a structured text is a different text)."""
    cfg = CONFIGS["pymupdf4llm-v1"]
    off = pin_document(cfg)
    assert set(off) == {"config", "tier", "mem_gb", "tools"} and set(off["tools"]) == set(cfg.tools)
    assert pin_document(cfg, Structured()) == off and pin_of(cfg, Structured(False, False)) == pin_of(cfg)
    assert convert_pin(acq()) == convert_pin(acq(), Structured()) == pin_of(cfg)
    on = pin_document(cfg, Structured(True, True))
    assert on["structured"] == {"jats": True, "arxiv_html": True}
    assert set(on["tools"]) == {*cfg.tools, "fetchpdf", "lxml"}
    pins = {pin_of(cfg), pin_of(cfg, Structured(True, False)), pin_of(cfg, Structured(False, True)),
            pin_of(cfg, Structured(True, True))}
    assert len(pins) == 4


def test_the_guard_reads_the_option() -> None:
    counts: dict[str, int] = {}
    on = {"structured": {"jats": True}, "allow_degraded_convert": True}
    assert guard_pin(acq(), on, counts) == pin_of(CONFIGS["pymupdf4llm-v1"], Structured(jats=True))
    assert guard_pin(acq(), {"allow_degraded_convert": True}, {}) == pin_of(CONFIGS["pymupdf4llm-v1"])
    with pytest.raises(StageError, match="options.structured"):
        guard_pin(acq(), {"structured": {"jats": 1}}, {})
    with pytest.raises(StageError, match="needs a convert config"):
        guard_pin(acq(None), {"structured": {"jats": True}}, {})
    assert guard_pin(acq(None), {"structured": {"jats": False}}, {}) is None


# ---------------------------------------------------------------- the pointer (pure)
def snap(n: int, role: str, *, source: str = "s1", rev: str | None = None, at: dt.datetime = T0,
         status: str | None = "ok", cfg: str | None = PIN, error_class: str | None = None) -> Row:
    return {"snapshot_id": f"r.{n}", "source_id": source, "kind": "raw", "content_role": role,
            "via": "live", "captured_at": at, "as_of": at, "revision": rev,
            "raw": blob(f"d{n}", 1, {"full_text": "application/pdf", "xml": "application/xml"}.get(role, "text/html")),
            "text": blob(f"t{n}", 1, "text/markdown") if status == "ok" or error_class else None, "text_status": status,
            "text_error_class": error_class, "convert_attempts": 1 if cfg else 0, "convert_pin": cfg,
            "quality": None}


def members(*sources: str) -> list[Row]:
    return [{"source_id": s, "is_live": True} for s in sources]


def cur(snaps: list[Row], roles: frozenset[str] | tuple[str, ...], *sources: str) -> tuple[str | None, str]:
    f = point({"snapshots": snaps}, members(*(sources or ("s1",))), PIN, roles)
    return f["current_snapshot_id"], f["current_text_status"]


XML, HTML, BOTH = frozenset({"xml"}), frozenset({"html"}), frozenset({"xml", "html"})


def test_jats_stands_in_for_its_pdf_once_ok() -> None:
    pdf, xml = snap(1, "full_text"), snap(2, "xml", at=T0 - dt.timedelta(days=3))  # captured earlier
    assert cur([pdf, xml], ()) == ("r.1", "ok")  # off: never the text
    assert cur([pdf, xml], XML) == ("r.2", "ok")
    assert cur([pdf, xml], HTML) == ("r.1", "ok")  # only the enabled roles
    pending = snap(2, "xml", status=None, cfg=None)
    assert cur([pdf, pending], XML) == ("r.1", "ok")
    failed = snap(2, "xml", status="failed_permanent", error_class="quality_too_short")
    assert cur([pdf, failed], XML) == ("r.1", "ok")  # the gate failed it: the PDF
    other_pin = snap(2, "xml", cfg="q" * 64)
    assert cur([pdf, other_pin], XML) == ("r.1", "ok")
    # A failed PDF with an ok JATS: the JATS; a JATS alone beats the abstract.
    assert cur([snap(1, "full_text", status="failed_permanent", error_class="parse_error"), xml], XML) == ("r.2", "ok")
    assert cur([snap(3, "abstract"), xml], XML) == ("r.2", "ok")


def test_structured_never_jumps_another_source_or_revision() -> None:
    """Only its own source's PDF of the same revision is replaced: arXiv ``html`` v1 does not beat
    the v2 PDF; a JATS of source s2 does not beat s1's newer PDF; with no partner it ranks as a
    full text by its own capture time."""
    later = T0 + dt.timedelta(days=1)
    v1_pdf, v2_pdf = snap(1, "full_text", rev="v1"), snap(2, "full_text", rev="v2", at=later)
    v1_html, v2_html = snap(3, "html", rev="v1", at=later), snap(4, "html", rev="v2", at=later)
    assert cur([v1_pdf, v2_pdf, v1_html], HTML) == ("r.2", "ok")
    assert cur([v1_pdf, v2_pdf, v1_html, v2_html], HTML) == ("r.4", "ok")
    a_pdf = snap(1, "full_text", source="s1", at=later)
    b_xml = snap(2, "xml", source="s2")
    assert cur([a_pdf, b_xml], XML, "s1", "s2") == ("r.1", "ok")
    newer_alone = snap(2, "xml", source="s2", at=later + dt.timedelta(days=1))
    assert cur([a_pdf, newer_alone], XML, "s1", "s2") == ("r.2", "ok")
    masked = [{"source_id": "s1", "is_live": False}]
    assert point({"snapshots": [b_xml]}, masked, PIN, XML)["current_text_status"] == "no_raw"


def row_of(snaps: list[Row]) -> Row:
    return {"doc_id": "d", "content": {"snapshots": snaps}, "identity": {"members": members("s1")}}


def test_convert_and_quality_scope() -> None:
    """Off: the current candidate only. On: the structured snapshots ahead of it and the candidate
    (the PDF fallback); a structured snapshot failed at the pin is not re-converted, a quality-failed
    one is still judged (a new gate may lift it)."""
    pdf_new, xml_new = snap(1, "full_text", status=None, cfg=None), snap(2, "xml", status=None, cfg=None)
    assert [s["snapshot_id"] for s in to_convert(row_of([pdf_new, xml_new]), PIN, frozenset())] == ["r.1"]
    assert [s["snapshot_id"] for s in to_convert(row_of([pdf_new, xml_new]), PIN, XML)] == ["r.2", "r.1"]
    assert [s["snapshot_id"] for s in to_convert(row_of([snap(1, "full_text"), xml_new]), PIN, XML)] == ["r.2"]
    crashed = snap(2, "xml", status="failed_permanent", error_class="parse_error")
    assert to_convert(row_of([snap(1, "full_text"), crashed]), PIN, XML) == []
    qfail = snap(2, "xml", status="failed_permanent", error_class="quality_too_short")
    assert [s["snapshot_id"] for s in judged_snapshots(row_of([snap(1, "full_text"), qfail]), PIN, XML)] == [
        "r.2", "r.1"]
    assert [s["snapshot_id"] for s in judged_snapshots(row_of([snap(1, "full_text"), qfail]), PIN)] == ["r.1"]


def test_comparison_needs_a_pdf_partner() -> None:
    """A JATS ranked ahead of an abstract (no PDF of that source) is compared with nothing."""
    from workflows.acquire.lanes import LaneEnv
    from workflows.acquire.quality import compare

    q = {"bucket": "ok", "metrics": {"chars": 5000}}
    xml, abstract = {**snap(2, "xml"), "quality": q}, {**snap(3, "abstract"), "quality": q}
    env = LaneEnv(run_id="r", now=T0, blobs=None)
    compare(row_of([xml, abstract]), [xml, abstract], {"r.2", "r.3"}, "r.2", env)
    assert env.counts == {}


def _q(chars: int | None) -> dict[str, Any]:
    return {"version": 1, "bucket": "ok", "flags": [], "fails": [], "gate_digest": "g", "converter": "c",
            "metrics": {"chars": chars}}


def test_structured_text_under_half_its_pdf_falls_back() -> None:
    """B9 (B8's ``sxad2hrqd8d7z``: a truncated LaTeXML page of 25.7k chars against its PDF's 101k):
    a structured text with fewer than 0.5 x its partner PDF's chars is not current; at or above
    half it stands in; unknown lengths change nothing; another source's PDF is no partner. The
    comparison counts it ``structured.<role>.too_short``; ``scope`` sees structured texts that
    need a verdict."""
    from workflows.acquire.lanes import LaneEnv
    from workflows.acquire.point import STRUCTURED_MIN_RATIO, too_short
    from workflows.acquire.quality import compare

    assert STRUCTURED_MIN_RATIO == 0.5
    pdf = {**snap(1, "full_text", rev="v1"), "quality": _q(101_000)}
    short = {**snap(2, "html", rev="v1"), "quality": _q(25_700)}
    half = {**snap(2, "html", rev="v1"), "quality": _q(50_500)}
    unknown = {**snap(2, "html", rev="v1"), "quality": None}
    assert too_short(short, pdf) and not too_short(half, pdf) and not too_short(unknown, pdf)
    assert cur([pdf, short], HTML) == ("r.1", "ok")
    assert cur([pdf, half], HTML) == ("r.2", "ok")
    assert cur([pdf, unknown], HTML) == ("r.2", "ok")
    other = {**snap(3, "xml", source="s2"), "quality": _q(1_000)}  # no partner: ranks by its own time
    assert cur([pdf, other], XML, "s1", "s2")[0] == "r.1"

    class _Texts:  # the comparison reads both texts for its structure markers
        def get(self, digest: str) -> bytes:
            return b"text"

    env = LaneEnv(run_id="r", now=T0, blobs=_Texts())  # type: ignore[arg-type]
    compare(row_of([pdf, short]), [pdf, short], {"r.1", "r.2"}, "r.1", env)
    assert (env.counts["structured.html.too_short"], env.counts["structured.html.fallback"]) == (1, 1)
    env = LaneEnv(run_id="r", now=T0, blobs=_Texts())  # type: ignore[arg-type]
    compare(row_of([pdf, half]), [half, pdf], {"r.1", "r.2"}, "r.2", env)
    assert "structured.html.too_short" not in env.counts and env.counts["structured.html.current"] == 1


def test_scope_quality_clause_sees_structured_texts() -> None:
    """B8's note: ``scope``'s quality clause judged only the current candidate; with the run's
    structured roles a structured text whose verdict is under another gate puts the row in scope."""
    from stack.conversion import quality as qmod
    from workflows.acquire.scope import reasons

    def q(gate: str) -> dict[str, Any]:
        return {**_q(50_000), "version": qmod.CHECKS_VERSION, "gate_digest": gate}

    pdf = {**snap(1, "full_text", rev="v1"), "quality": q("g")}
    html = {**snap(2, "html", rev="v1"), "quality": q("old")}
    row = row_of([pdf, html])
    row.update(state="acquired", inband={"entries": []}, annotations={})
    row["content"].update(roles=[], members_digest="m")
    row["identity"]["members_digest"] = "m"
    assert "quality" not in reasons(row, T0, True, pin=PIN, quality_gate="g")  # off: the PDF only
    assert "quality" in reasons(row, T0, True, pin=PIN, quality_gate="g", structured=HTML)


def test_structure_markers() -> None:
    text = "a $x$ and $$y = 1$$ (1) and $z_1$\n<table><tr><td>1</td></tr></table>\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert structure_markers(text) == {"math": 3, "tables": 2}
    assert structure_markers("plain text, no formula, costs 5 dollars") == {"math": 0, "tables": 0}


# ---------------------------------------------------------------- the LaTeXML path
needs_fetchpdf = pytest.mark.skipif(importlib.util.find_spec("fetchpdf") is None or importlib.util.find_spec("lxml")
                                    is None, reason="fetchpdf/lxml not installed")


@needs_fetchpdf
def test_latexml_path_keeps_maths_and_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACK_CONVERT_NO_DOCLING", "1")
    r = ladder.convert(LATEXML, "text/html", workdir=Path("."), latexml=True)
    assert r.status == "ok" and r.converter.startswith("fetchpdf-md@") and r.converter.endswith("+tierlatexml")
    md = r.markdown
    assert "$\\mathbb{E}[A]$" in md and "$$A_{i}=\\frac{r_{i}-\\mu}{\\sigma}$$ (1)" in md  # inline, display + number
    assert "<td>80.89</td>" in md and "Table 1: Accuracy of the fixture methods." in md  # a figure-wrapped table
    assert "## 2 Method" in md and "tl_set" not in md and "ltx_" not in md
    assert md.count("<table") == 1  # the equation table became a paragraph
    assert structure_markers(md)["math"] >= 47 and structure_markers(md)["tables"] == 1


@needs_fetchpdf
def test_latexml_path_only_with_the_request_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the convert request selects the path; no environment variable does (R6-6)."""
    monkeypatch.setenv("STACK_CONVERT_NO_DOCLING", "1")
    monkeypatch.setenv("STACK_CONVERT_LATEXML", "1")  # the B8 draft's switch: must have no effect
    r = ladder.convert(LATEXML, "text/html", workdir=Path("."))
    assert "latexml" not in r.converter  # the old route (trafilatura or the stdlib floor)


# ================================================================ runner cases
from tests.test_acquire_lanes import (
    DAY,
    NOW,
    aid_of,
    arxiv_row,
    atom,
    by_role,
    checks_ok,
    doi_of,
    go,
    mailto,  # noqa: F401 — the fixture
    oa_go,
    oa_row,
    raws,
    snaps_of,
    store,
    view,
)

HTML_URL = "https://arxiv.org/html/{aid}v{v}"


def html_plan(*, on: bool = True) -> Plan:
    acquire: dict[str, Any] = {"wants": {"arxiv": ["full_text", "html"]}, "convert": {"config": "pymupdf4llm-v1"}}
    options: dict[str, Any] = {"allow_degraded_convert": True}  # any venv: the convert pin guard (R4-1) is lifted
    if on:
        options["structured"] = {"arxiv_html": True}
    return Plan.model_validate({"plan": "test-structured", "schema_version": 2, "acquire": acquire,
                                "stages": [{"stage": "acquire", "options": options}]})


def pdf_v(v: int) -> bytes:
    """A born-digital PDF of revision ``v`` (streams uncompressed: past the lane's 10 KB PDF size floor)."""
    fitz = pytest.importorskip("fitz", reason="pymupdf (fitz) not installed")
    doc = fitz.open()
    for page in range(3):
        doc.new_page().insert_text((72, 72), "\n".join(f"v{v} page {page} line {i}: born-digital text of the paper."
                                                        for i in range(45)), fontsize=9)
    data = doc.tobytes(expand=255)
    doc.close()
    return data


def arxiv_world(fake: FakeHttp, v: int, *, html: bytes | None, html_status: int = 200) -> None:
    atom(fake, {1: v})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v{v}", body=pdf_v(v))
    fake.add(HTML_URL.format(aid=aid_of(1), v=v), status=html_status, body=html or b"",
             headers={"Content-Type": "text/html; charset=utf-8"})


def html_calls(fake: FakeHttp, n: int = 0) -> list[str]:
    return [u for u in fake.urls()[n:] if "/html/" in u]


pytestmark_slow = pytest.mark.slow


@pytestmark_slow
@needs_fetchpdf
def test_arxiv_html_is_the_text_and_the_pdf_the_fallback(tmp_path: Path) -> None:
    """The ``html`` role mints the versioned LaTeXML page (``text/html``, ``revision = 'v1'``); both
    texts convert in the child, both get a verdict, the HTML is current (``current_role = 'html'``),
    and the comparison counts say so; V-content and the other checks pass."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=LATEXML)
    rec = go(root, fake, NOW, plan=html_plan())
    assert rec.counts["arxiv.html.downloads"] == 1 and rec.counts["arxiv.downloads"] == 1
    assert rec.counts["convert.role.html.ok"] == 1 and rec.counts["convert.ok"] == 2
    # the child took the docling-free LaTeXML path (the request's flag, not an environment variable)
    assert [k for k in rec.counts if k.startswith("convert.converter.fetchpdf-md@") and k.endswith("+tierlatexml")]
    row = view(root)[d]
    (h,) = snaps_of(row, "html")
    (p,) = snaps_of(row, "full_text")
    assert (h["raw"]["media_type"], h["revision"], h["url"]) == ("text/html", "v1", HTML_URL.format(aid=aid_of(1), v=1))
    assert h["quality"]["bucket"] in ("ok", "flagged") and p["quality"] is not None
    c = row["content"]
    assert (c["current_snapshot_id"], c["current_role"], c["current_text_status"], c["current_revision"]) == (
        h["snapshot_id"], "html", "ok", "v1")
    role = by_role(row)["html"]
    assert (role["status"], role["lane"], role["validator_url"]) == ("ok", "arxiv", h["url"])
    assert rec.counts["structured.html.compared"] == 1 and rec.counts["structured.html.current"] == 1
    assert rec.counts["structured.html.math.structured"] > rec.counts.get("structured.html.math.base", 0)
    checks_ok(root)


@pytestmark_slow
@needs_fetchpdf
def test_arxiv_html_failing_the_gate_falls_back_to_the_pdf(tmp_path: Path) -> None:
    """An acceptable page (≥ 20 KB of LaTeXML markup) whose text is too short for a paper body:
    the gate fails it (``failed_permanent[quality_too_short]``), the PDF is current."""
    thin = (b'<html><body><article class="ltx_document"><p class="ltx_p">Only a stub.</p>'
            + b"<!-- " + b"x" * ARXIV_HTML_MIN_BYTES + b" --></article></body></html>")
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=thin)
    rec = go(root, fake, NOW, plan=html_plan())
    row = view(root)[d]
    (h,) = snaps_of(row, "html")
    (p,) = snaps_of(row, "full_text")
    assert (h["text_status"], h["text_error_class"], h["quality"]["bucket"]) == (
        "failed_permanent", "quality_too_short", "failed")
    c = row["content"]
    assert (c["current_snapshot_id"], c["current_role"], c["current_text_status"]) == (p["snapshot_id"], "full_text", "ok")
    assert rec.counts["structured.html.fallback"] == 1 and rec.counts["structured.html.bucket.failed"] == 1
    checks_ok(root)


@pytestmark_slow
def test_arxiv_no_html_is_remembered_per_version(tmp_path: Path) -> None:
    """Run 1: arXiv's 404 for v1 → the ``html`` role is ``empty`` (``no_html:http_404``) with the
    v1 URL as ``validator_url``; the PDF is fetched and current. Run 2, past the week (the PDF role
    is re-checked): the HTML is not asked; the role waits until ``last_attempt_at + 180 d``. Run 3,
    v2 is out: a new URL, asked at once. A paper with no HTML for 180 days is asked again."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=b"No HTML for '2610.00001v1'", html_status=404)
    rec = go(root, fake, NOW, plan=html_plan())
    assert rec.counts["arxiv.html.missing"] == 1 and len(html_calls(fake)) == 1
    role = by_role(view(root)[d])["html"]
    v1_url = HTML_URL.format(aid=aid_of(1), v=1)
    assert (role["status"], role["error_class"], role["last_outcome"], role["validator_url"], role["attempts"]) == (
        "empty", None, "no_html:http_404", v1_url, 0)
    assert view(root)[d]["content"]["current_role"] == "full_text"

    n = len(fake.urls())
    rec = go(root, fake, NOW + 8 * DAY, plan=html_plan())
    assert html_calls(fake, n) == [] and rec.counts["arxiv.html.remembered"] == 1
    role = by_role(view(root)[d])["html"]
    assert role["retry_after"] == NOW + 180 * DAY and role["last_attempt_at"] == NOW

    n = len(fake.urls())
    arxiv_world(fake, 2, html=LATEXML)
    rec = go(root, fake, NOW + 16 * DAY, plan=html_plan())
    assert html_calls(fake, n) == [HTML_URL.format(aid=aid_of(1), v=2)]
    row = view(root)[d]
    assert [s["revision"] for s in snaps_of(row, "html")] == ["v2"] and by_role(row)["html"]["status"] == "ok"
    checks_ok(root)


@pytestmark_slow
def test_arxiv_no_html_is_asked_again_after_180_days(tmp_path: Path) -> None:
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=b"", html_status=404)
    go(root, fake, NOW, plan=html_plan())
    n = len(fake.urls())
    rec = go(root, fake, NOW + 179 * DAY, plan=html_plan())
    assert html_calls(fake, n) == [] and rec.counts["arxiv.html.remembered"] == 1
    rec = go(root, fake, NOW + 181 * DAY, plan=html_plan())
    assert html_calls(fake, n) == [HTML_URL.format(aid=aid_of(1), v=1)]
    assert by_role(view(root)[d])["html"]["last_attempt_at"] == NOW + 181 * DAY


@pytestmark_slow
def test_arxiv_html_unacceptable_page_and_option_off(tmp_path: Path) -> None:
    """A 200 below the size floor is ``empty`` (``no_html:too_small``), never a snapshot; with the
    option off a wanted ``html`` role is ``skipped[not_requested]`` and nothing is asked."""
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=b"<html><body>small</body></html>")
    rec = go(root, fake, NOW, plan=html_plan())
    row = view(root)[sid(1)]
    assert rec.counts["arxiv.html.unacceptable.too_small"] == 1 and snaps_of(row, "html") == []
    assert (by_role(row)["html"]["status"], by_role(row)["html"]["last_outcome"]) == ("empty", "no_html:too_small")

    root = store(tmp_path / "off", [arxiv_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=LATEXML)
    go(root, fake, NOW, plan=html_plan(on=False))
    role = by_role(view(root)[sid(1)])["html"]
    assert (role["status"], role["error_class"]) == ("skipped", "not_requested") and html_calls(fake) == []


@pytestmark_slow
def test_arxiv_html_after_a_withdrawn_pdf_is_skipped(tmp_path: Path) -> None:
    """A paper whose current version is a withdrawal: the PDF episode gets arXiv's withdrawal page
    (404) and ends ``skipped[withdrawn]``; the ``html`` role of the same id in the same run answers
    ``skipped[withdrawn]`` without asking its URL. Nothing is minted."""
    from tests.test_acquire_lanes import withdrawn_atom, withdrawn_page

    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    withdrawn_atom(fake, 1)
    pdf = f"https://arxiv.org/pdf/{aid_of(1)}v2"
    for url in (pdf, HTML_URL.format(aid=aid_of(1), v=2)):
        fake.add(url, status=404, body=withdrawn_page(aid_of(1), 2), headers={"Content-Type": "text/html"})
    rec = go(root, fake, NOW, plan=html_plan())
    assert pdf in fake.urls() and html_calls(fake) == []
    assert rec.counts["arxiv.withdrawn"] == 1 and rec.counts["snapshots_minted"] == 0
    roles = by_role(view(root)[sid(1)])
    assert {k: (r["status"], r["error_class"]) for k, r in roles.items()} == {
        "full_text": ("skipped", "withdrawn"), "html": ("skipped", "withdrawn")}
    checks_ok(root)


def keys_html_plan() -> Plan:
    """OpenAlex members want nothing of their own; ``arxiv_html`` on (R6 item 2b cases)."""
    return Plan.model_validate({"plan": "test-structured", "schema_version": 2,
                                "acquire": {"wants": {"openalex": []}, "convert": {"config": "pymupdf4llm-v1"}},
                                "stages": [{"stage": "acquire", "options": {
                                    "allow_degraded_convert": True, "structured": {"arxiv_html": True}}}]})


@pytestmark_slow
def test_no_html_memory_holds_for_an_id_only_in_identity_keys(tmp_path: Path) -> None:
    """The lane's history sees the keys-only ``html`` role (its id from ``identity.keys``): after arXiv's
    404 the "no HTML" memory holds it for 180 days, so a run a week later asks nothing."""
    root = store(tmp_path, [oa_row(1)])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=b"", html_status=404)
    go(root, fake, NOW, plan=keys_html_plan())
    assert len(html_calls(fake)) == 1
    n = len(fake.urls())
    rec = go(root, fake, NOW + 8 * DAY, plan=keys_html_plan())
    assert html_calls(fake, n) == [] and rec.counts["arxiv.html.remembered"] == 1
    assert by_role(view(root)[sid(1)])["html"]["retry_after"] == NOW + 180 * DAY


@pytestmark_slow
@needs_fetchpdf
def test_arxiv_html_for_an_id_only_in_identity_keys(tmp_path: Path) -> None:
    """R6 item 2b through the real arXiv lane: an OpenAlex paper joined by a publisher DOI whose arXiv id
    is only in ``identity.keys`` (``oa_row``: the synthetic row keeps its ``arxiv`` key there). Its
    ``html`` role is on the arXiv lane; the history's Atom sweep includes the id and the lane asks
    ``/html/<id>v1`` once; the LaTeXML text is minted and converted. No ``full_text`` is wanted here."""
    row = oa_row(1)
    assert row["identity"]["keys"] == [{"kind": "arxiv", "value": aid_of(1), "source_id": sid(1)}]
    plan = keys_html_plan()
    root = store(tmp_path, [row])
    fake = FakeHttp()
    arxiv_world(fake, 1, html=LATEXML)
    rec = go(root, fake, NOW, plan=plan)
    assert html_calls(fake) == [HTML_URL.format(aid=aid_of(1), v=1)]
    assert any("export.arxiv.org" in u for u in fake.urls())  # the history's sweep, not a per-role request
    assert rec.counts["carry.arxiv_html_from_keys"] == 1 and rec.counts["convert.role.html.ok"] == 1
    out = view(root)[sid(1)]
    (h,) = snaps_of(out, "html")
    assert (h["revision"], by_role(out)["html"]["lane"], by_role(out)["html"]["status"]) == ("v1", "arxiv", "ok")
    assert out["content"]["current_role"] == "html"


def jats_plan(*, on: bool = True) -> Plan:
    options: dict[str, Any] = {"oa_route": "walk", "allow_degraded_convert": True}
    if on:
        options["structured"] = {"jats": True}
    return Plan.model_validate({"plan": "test-structured", "schema_version": 2,
                                "acquire": {"convert": {"config": "pymupdf4llm-v1"},
                                            "wants": {"openalex": ["full_text", "xml"]}},
                                "stages": [{"stage": "acquire", "options": options}]})


@pytestmark_slow
@needs_fetchpdf
@pytest.mark.usefixtures("mailto")
@pytest.mark.parametrize("thin", [False, True])
def test_jats_ranks_above_the_pdf_and_falls_back(tmp_path: Path, thin: bool) -> None:
    """One OA retrieval gives the JATS (role ``xml``) and the PDF. With ``structured.jats`` both
    convert (fetchpdf's JATS converter for the XML) and both are judged; the JATS is current. A JATS
    whose body is too short fails the gate and the PDF is current."""
    from tests.test_acquire_convert import pdf_bytes

    jats = JATS if not thin else JATS.split(b"<sec id=\"s1\">")[0] + b"<sec><p>Too short.</p></sec></body></article>"
    d = sid(1)
    root = store(tmp_path, [oa_row(1)])
    fp = FakeFetchpdf({doi_of(1): [(jats, "https://europepmc.test/jats/1", "europepmc"),
                                   (pdf_bytes(), "https://journal.test/1.pdf", "unpaywall")]})
    rec = oa_go(root, fp, NOW, jats_plan())
    row = view(root)[d]
    (x,) = snaps_of(row, "xml")
    (p,) = snaps_of(row, "full_text")
    assert x["quality"] is not None and p["quality"] is not None and rec.counts["structured.xml.compared"] == 1
    c = row["content"]
    if not thin:
        assert (x["text_status"], c["current_snapshot_id"], c["current_role"], c["current_text_status"]) == (
            "ok", x["snapshot_id"], "xml", "ok")
        assert rec.counts["convert.role.xml.ok"] == 1 and rec.counts["structured.xml.current"] == 1
        assert rec.counts["structured.xml.tables.structured"] == 1
    else:
        assert (x["text_status"], x["text_error_class"]) == ("failed_permanent", "quality_too_short")
        assert (c["current_snapshot_id"], c["current_role"]) == (p["snapshot_id"], "full_text")
        assert rec.counts["structured.xml.fallback"] == 1
    checks_ok(root)


@pytestmark_slow
@pytest.mark.usefixtures("mailto")
def test_jats_option_off_keeps_the_pdf_and_its_pin(tmp_path: Path) -> None:
    from tests.test_acquire_convert import pdf_bytes

    root = store(tmp_path, [oa_row(1)])
    fp = FakeFetchpdf({doi_of(1): [(JATS, "https://europepmc.test/jats/1", "europepmc"),
                                   (pdf_bytes(), "https://journal.test/1.pdf", "unpaywall")]})
    rec = oa_go(root, fp, NOW, jats_plan(on=False))
    row = view(root)[sid(1)]
    (x,) = snaps_of(row, "xml")
    assert (x["text_status"], x["convert_attempts"]) == (None, 0) and row["content"]["current_role"] == "full_text"
    assert row["content"]["convert_pin"] == pin_of(CONFIGS["pymupdf4llm-v1"])
    assert not any(k.startswith("structured.") for k in rec.counts)
    assert len(raws(row)) == 2
