"""Round 3, Q: LLM-free text quality checks (``stack/conversion/quality.py``) and the ``quality``
substage of ``acquire`` (``workflows/acquire/quality.py``).

Real cases are snippets of the dev store's texts frozen in ``tests/fixtures/quality/`` (provenance
in ``cases.json``). The store holds no stub, challenge, paywall, mojibake, binary or link-menu text
(every text blob of ``v4/store-dev`` and ``v4/store-sr-scratch`` was scanned, and every raw HTML
blob through the stdlib converter), so those cases are synthetic and marked so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from stack.checks.verify import run_checks
from stack.conversion import quality as q
from stack.schemas.structs import QUALITY, QUALITY_METRICS, SNAPSHOT
from stack.schemas.vocab import ERROR_CLASSES, TEXT_QUALITY_BUCKETS
from stack.stage_runner import StageError, run_stage
from stack.store.blobs import BlobStore, open_blobs
from tests.fakes.lanes import TrapTransport
from tests.synth.records import sdir
from tests.synth.rows import blob
from tests.synth.world import write_on_head
from tests.test_acquire_convert import (
    BODY,
    NOW,
    TRAPS,
    env_for,
    pending_row,
    plan,
    snap_of,
    view,
)
from workflows.acquire import lanes
from workflows.acquire.convert import run_convert
from workflows.acquire.point import point
from workflows.acquire.quality import (
    Converted,
    current_quality,
    gate_of,
    needs_verdict,
    run_quality,
)
from workflows.acquire.scope import reasons

FIX = Path(__file__).parent / "fixtures" / "quality"
CASES: dict[str, dict[str, Any]] = json.loads((FIX / "cases.json").read_text(encoding="utf-8"))
Row = dict[str, Any]


def fixture(name: str) -> tuple[str, q.Facts]:
    c = CASES[name]
    return ((FIX / f"{name}.md").read_text(encoding="utf-8"),
            q.Facts(role=c["role"], kind=q.kind_of(c["media_type"]), raw_bytes=c.get("raw_bytes")))


# ---------------------------------------------------------------- schema and vocabulary
def test_metrics_struct_matches_the_module() -> None:
    assert tuple(f.name for f in QUALITY_METRICS) == q.METRIC_FIELDS
    assert SNAPSHOT[-1].name == "quality" and SNAPSHOT[-1].nullable and SNAPSHOT[-1].type == QUALITY
    assert set(q.FAIL_CLASSES.values()) <= ERROR_CLASSES["failed_permanent"]
    assert q.BUCKETS == TEXT_QUALITY_BUCKETS
    assert set(q.FAIL_CLASSES) == {c.name for c in q.CHECKS if c.can_fail}
    names = {c.name for c in q.CHECKS}
    assert {k.split(".")[0] for k in q.THRESHOLDS} <= names  # every threshold belongs to a check
    metrics = q.measure(BODY, q.Facts("page", "html", raw_bytes=10_000, pages=None))
    assert tuple(metrics) == q.METRIC_FIELDS
    pa.array([metrics], type=QUALITY_METRICS)  # the measured dict fits the struct


# ---------------------------------------------------------------- real texts from the dev store
REAL: dict[str, tuple[str, set[str], set[str], str | None]] = {
    # case: (bucket, flags that must fire, flags that must not, error class)
    "arxiv-abs-landing": ("failed", {"abstract_only", "too_short"}, set(), "quality_abstract_only"),
    "sci-reports-article": ("ok", set(), {"references_only", "abstract_only"}, None),
    "sci-reports-references": ("failed", {"references_only"}, set(), "quality_references_only"),
    "sci-reports-running-headers": ("flagged", {"repeated_lines"}, set(), None),
    "indonesian-paper": ("flagged", {"non_english"}, set(), None),
    "japanese-abstract": ("flagged", {"non_latin"}, {"non_english"}, None),
    "latex-residue-page": ("flagged", {"latex_residue"}, set(), None),
    "toc-dot-leaders": ("flagged", set(), {"glyph_runs", "references_only"}, None),
    "figure-text": ("flagged", {"figure_text"}, {"references_only"}, None),
    "openai-short-post": ("ok", set(), {"too_short", "low_text_ratio"}, None),
    "openai-abstract-page": ("flagged", {"low_text_ratio"}, {"too_short"}, None),
    "replacement-chars-pdf": ("failed", {"replacement"}, set(), "quality_garbled"),
}


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_snippets(name: str) -> None:
    text, facts = fixture(name)
    bucket, must, must_not, cls = REAL[name]
    _, v = q.assess(text, facts)
    assert v.bucket == bucket, (v.flags, v.fails)
    assert must <= set(v.flags), v.flags
    assert not must_not & set(v.flags), v.flags
    assert v.error_class == cls


def test_dot_leaders_are_not_glyph_runs() -> None:
    """The ToC excerpt has runs of single glyphs (``. . . .``) that the raw pattern matches; only
    runs with 5+ letters or digits count."""
    text, facts = fixture("toc-dot-leaders")
    assert q._GLYPH_RUN.findall(text)
    assert q.measure(text, facts)["glyph_runs_per_1k"] == 0
    garbled = "x i = 1 2 3 4 5 6 7 8 9 " * 3 + text
    assert q.measure(garbled, facts)["glyph_runs_per_1k"] > 0


def test_running_headers_counted_per_line() -> None:
    text, facts = fixture("sci-reports-running-headers")
    m = q.measure(text, facts)
    assert m["repeated_line_ratio"] > q.THRESHOLDS["repeated_lines.flag"]
    one_page = "\n".join(line for line in text.splitlines() if "Scientific Reports" not in line
                         and "Article in Press" not in line and "doi.org" not in line)
    assert q.measure(one_page, facts)["repeated_line_ratio"] < q.THRESHOLDS["repeated_lines.flag"]


def test_abstract_only_by_the_metadata_abstract() -> None:
    """A paper body that is its abstract plus a little: below 2.5 times the abstract fails, below
    5 times flags; a real body (the Scientific Reports excerpt) is far above."""
    article, facts = fixture("sci-reports-article")
    abstract = article[article.find("#### **Abstract**") + 17:][:1600]
    body = "Title line\n\n" + abstract + "\n\nKeywords: models, reasoning, meta-analysis."
    v = q.judge(q.measure(body, facts._replace(abstract=abstract)), "full_text")
    assert (v.bucket, v.error_class) == ("failed", "quality_abstract_only")
    m = q.measure(article, facts._replace(abstract=abstract))
    assert m["abstract_ratio"] > 5 and "abstract_only" not in q.judge(m, "full_text").flags


# ---------------------------------------------------------------- synthetic cases (none in the store)
SYNTH_STUBS: dict[str, str] = {
    "challenge": "Just a moment...\nChecking if the site connection is secure\nexample.org needs to review the "
                 "security of your connection before proceeding.\nPerformance & security by Cloudflare",
    "access_denied": "# Access Denied\n\nYou don't have permission to access this resource on this server.",
    "javascript": "You need to enable JavaScript to run this app.",
    "paywall": "# A study of agents\n\nThe first paragraph of the story.\n\nSubscribe to continue reading. "
               "Already a subscriber? Sign in to read the full article.",
    "not_found": "# Page not found\n\nThe page you are looking for could not be found. Return home.",
    "cookie_wall": "We use cookies to improve your experience. Accept all cookies or manage cookie settings.",
    "server_error": "500 Internal Server Error\n\nSomething went wrong. Please try again later.",
}


@pytest.mark.parametrize("cat", sorted(SYNTH_STUBS))
def test_stub_pages_fail_when_short(cat: str) -> None:
    """Synthetic: a stub/error page fails ``quality_stub`` in every role; the same phrase in a long
    real body (the Scientific Reports excerpt, 11.5k chars) neither fails nor flags as a stub."""
    stub = SYNTH_STUBS[cat]
    for role in ("page", "post", "full_text"):
        m, v = q.assess(stub, q.Facts(role, "html"))
        assert cat in m["stub_hits"], m["stub_hits"]
        assert (v.bucket, v.error_class) == ("failed", "quality_stub"), (role, v)
    article, facts = fixture("sci-reports-article")
    m, v = q.assess(article + "\n\n" + stub, facts)
    assert cat in m["stub_hits"] and "stub" not in v.flags


def test_mojibake_binary_markup_synthetic() -> None:
    """Synthetic: UTF-8 read as cp1252, a binary dump, a link menu."""
    good = BODY * 6
    moji = good.replace("genuine", "genuÃ¯ne").replace("text", "textâ€™s")
    m, v = q.assess(moji, q.Facts("page", "html"))
    assert m["mojibake_per_1k"] > q.THRESHOLDS["mojibake.fail"] and v.error_class == "quality_garbled"
    binary = "".join(chr(c) for c in range(1, 9)) * 300 + good
    assert q.assess(binary, q.Facts("page", "html"))[1].error_class == "quality_garbled"
    menu = "\n".join(f"- [Section {i}](https://example.org/a/very/long/path/to/section-{i}?ref=nav)"
                     for i in range(80)) + "\n\n" + BODY
    m, v = q.assess(menu, q.Facts("page", "html"))
    assert m["link_line_ratio"] > 0.9 and v.error_class == "quality_markup" and "link_lines" in v.flags
    assert q.assess(good, q.Facts("page", "html"))[1].bucket == "ok"


def test_pdf_length_against_pages_and_truncation() -> None:
    """Synthetic: 10 pages yielding 1,500 chars (a scan without OCR) fail; 10 pages of 5,000 chars
    pass; the converter's truncation signal flags."""
    text = (BODY * 3)[:1500]
    assert q.assess(text, q.Facts("page", "pdf", pages=10))[1].error_class == "quality_too_short"
    long = BODY * 30
    m, v = q.assess(long, q.Facts("full_text", "pdf", pages=10))
    assert m["chars_per_page"] > 5000 and v.bucket == "ok"
    assert "truncated" in q.assess(long, q.Facts("full_text", "pdf", pages=10, truncated=True))[1].flags


# ---------------------------------------------------------------- the gate
def test_gate_overrides_and_digest() -> None:
    text, facts = fixture("arxiv-abs-landing")
    metrics = q.measure(text, facts)
    assert q.judge(metrics, "full_text").bucket == "failed"
    flag_only = q.gate_from_options({"quality": {"fail": False}})
    v = q.judge(metrics, "full_text", flag_only)
    assert (v.bucket, v.fails, v.error_class) == ("flagged", ["abstract_only"], None)
    off = q.gate_from_options({"quality": {"thresholds": {"abstract_only.fail": None, "abstract_only.flag": None,
                                                          "too_short.full_text.flag": None}}})
    assert q.judge(metrics, "full_text", off).bucket == "ok"
    digests = {q.DEFAULT_GATE.digest(), flag_only.digest(), off.digest(), q.gate_from_options({}).digest()}
    assert len(digests) == 3  # the empty options are the default gate
    for bad in ({"quality": {"thresholds": {"no_such.fail": 1}}}, {"quality": {"fail": "yes"}},
                {"quality": {"thresholds": {"stub.fail": "1"}}}, {"quality": {"gate": {}}}, {"quality": []}):
        with pytest.raises(ValueError):
            q.gate_from_options(bad)
        with pytest.raises(StageError):
            gate_of(bad)


# ---------------------------------------------------------------- the substage on rows
def inband_row(blobs: BlobStore, i: int, text: str, role: str = "post", media: str = "text/markdown") -> Row:
    """An acquired row whose current text is an in-band collector text (no conversion)."""
    row = pending_row(blobs, i, b"", media)
    (snap,) = row["content"]["snapshots"]
    body = blob(blobs.put(text.encode()), len(text.encode()), media)
    snap.update(kind="inband", via="inband", content_role=role, raw=body, text=dict(body), text_status="ok",
                convert_pin=None, quality=None)
    for r in row["content"]["roles"]:
        r.update(content_role=role, lane="inband")
    return row


def repoint(row: Row, pin: str | None) -> dict[str, Any]:
    fields = point(row["content"], row["identity"]["members"], pin)
    return fields | {"current_text_quality": current_quality(row, fields["current_snapshot_id"],
                                                             fields["current_text_status"])}


def test_failed_inband_text_is_skipped_by_the_pointer_then_lifted(tmp_path: Path) -> None:
    """An in-band stub fails: ``failed_permanent[quality_stub]``, text digest kept, pointer
    ``convert_failed``. A flag-only gate re-judges from the stored metrics (the text blob is
    gone: nothing is read) and restores ``ok``; the default gate fails it again."""
    e = env_for(tmp_path)
    row = inband_row(e.blobs, 1, SYNTH_STUBS["challenge"])
    gate = gate_of({})
    assert needs_verdict(row, e.convert_pin, gate.digest())
    run_quality([row], e, gate)
    s = snap_of(row)
    assert (s["text_status"], s["text_error_class"], s["quality"]["bucket"]) == (
        "failed_permanent", "quality_stub", "failed")
    assert s["text"] and s["quality"]["converter"] == "inband" and s["quality"]["fails"] == ["stub", "too_short"]
    assert s["quality"]["tier"] is None  # D690: a collector's text has no ladder tier
    f = repoint(row, e.convert_pin)
    assert (f["current_text_status"], f["current_text_sha256"], f["current_text_quality"]) == (
        "convert_failed", None, "failed")
    assert not needs_verdict(row, e.convert_pin, gate.digest())
    assert e.counts == {"quality.measured": 1, "quality.failed": 1, "quality.fail.stub": 1, "quality.fail.too_short": 1}

    (tmp_path / "blobs" / s["text"]["sha256"]).unlink()
    lenient = gate_of({"quality": {"fail": False}})
    assert needs_verdict(row, e.convert_pin, lenient.digest())
    run_quality([row], e, lenient)
    s = snap_of(row)
    assert (s["text_status"], s["text_error_class"], s["quality"]["bucket"]) == ("ok", None, "flagged")
    assert s["quality"]["fails"] == ["stub", "too_short"] and e.counts["quality.rejudged"] == 1
    f = repoint(row, e.convert_pin)
    assert (f["current_text_status"], f["current_text_quality"]) == ("ok", "flagged")
    run_quality([row], e, gate)
    assert (snap_of(row)["text_status"], e.counts["quality.rejudged"]) == ("failed_permanent", 2)


def test_checks_version_remeasures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env_for(tmp_path)
    row = inband_row(e.blobs, 1, BODY * 4)
    gate = gate_of({})
    run_quality([row], e, gate)
    assert snap_of(row)["quality"]["bucket"] == "ok" and e.counts["quality.measured"] == 1
    run_quality([row], e, gate)
    assert e.counts["quality.measured"] == 1  # same version, same gate: nothing to do
    monkeypatch.setattr(q, "CHECKS_VERSION", q.CHECKS_VERSION + 1)
    assert needs_verdict(row, e.convert_pin, gate.digest())
    run_quality([row], e, gate)
    assert e.counts["quality.measured"] == 2 and snap_of(row)["quality"]["version"] == q.CHECKS_VERSION


def test_scope_clause_and_older_snapshots(tmp_path: Path) -> None:
    """``reasons`` names ``quality`` only with a gate digest and only for a current text without a
    verdict; a text of another pin is convert's, not judged."""
    e = env_for(tmp_path)
    row = inband_row(e.blobs, 1, BODY * 4)
    row["annotations"]["triage"] = None  # not fetchable: only the quality clause can put it in scope
    digest = gate_of({}).digest()
    assert "quality" not in reasons(row, NOW, True)
    assert "quality" in reasons(row, NOW, True, pin=e.convert_pin, quality_gate=digest)
    run_quality([row], e, gate_of({}))
    assert "quality" not in reasons(row, NOW, True, pin=e.convert_pin, quality_gate=digest)
    other = pending_row(e.blobs, 2, BODY.encode(), "text/plain")
    snap_of(other).update(text=blob(e.blobs.put(b"x"), 1, "text/plain"), text_status="ok",
                          convert_attempts=1, convert_pin="another-pin")
    assert not needs_verdict(other, e.convert_pin, digest)


def test_conversion_facts_reach_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``convert`` hands the stamp, page count and truncation signal to ``quality``; a charged
    attempt clears an earlier verdict."""
    from workflows.acquire import convert as convert_mod
    from workflows.acquire.convert import ConvertOutcome

    monkeypatch.setattr(convert_mod, "convert_one", lambda raw, media_type, **kw: ConvertOutcome(
        "ok", markdown=BODY * 10, converter="fake@1+tier2", pages=100, truncated=True))
    e = env_for(tmp_path)
    row = pending_row(e.blobs, 1, b"%PDF-1.7 not really", "application/pdf")
    snap_of(row)["quality"] = {"stale": True}
    converted: dict[tuple[str, str], Converted] = {}
    run_convert([row], e, converted)
    assert snap_of(row)["quality"] is None and converted[(row["doc_id"], snap_of(row)["snapshot_id"])] == Converted(
        "fake@1+tier2", 100, True)
    run_quality([row], e, gate_of({}), converted)
    qual = snap_of(row)["quality"]
    assert (qual["converter"], qual["metrics"]["pages"], qual["metrics"]["truncated"]) == ("fake@1+tier2", 100, True)
    assert qual["tier"] == "2"  # D690: the stamp's tier, a field
    assert qual["bucket"] == "failed" and qual["fails"] == ["sparse_pages"]  # 18,600 chars over 100 pages
    assert "truncated" in qual["flags"]
    assert snap_of(row)["text_error_class"] == "quality_too_short"


# ---------------------------------------------------------------- through the runner
@pytest.mark.slow
def test_runner_converts_judges_points_and_rejudges(tmp_path: Path) -> None:
    """``--only convert`` (trap lanes, no network): a raw challenge page converts in the child,
    the gate fails it, the pointer reads ``convert_failed``; the store passes verify's content
    checks; a second run writes nothing; a plan with a flag-only gate re-judges it to ``ok``."""
    root = tmp_path / "store"
    blobs = open_blobs(root)
    page = (f"<html><head><title>Just a moment...</title></head><body><p>{SYNTH_STUBS['challenge']}</p>"
            "</body></html>").encode()
    rows = [pending_row(blobs, 1, page, "text/html"), pending_row(blobs, 2, (BODY * 6).encode(), "text/plain")]
    for r in rows:  # the seeded rows' pointer agrees with their unconverted snapshot (V-content)
        r["inband"]["entries"] = []  # no in-band abstract: ``inband`` would mint it (D690: no blob is read)
        r["content"].update(point(r["content"], r["identity"]["members"], None))
    write_on_head(root, "documents", "documents", rows, stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband", "content"])
    wants = {"arxiv": ["full_text"]}

    def go(**options: Any) -> Any:
        with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
            rec = run_stage(root, plan("pymupdf4llm-v1", wants, **options), "acquire",
                            [sdir("documents")], series="main", only=["convert"])
        assert rec.status == "ok", rec.error
        return rec

    rec = go()
    assert rec.counts["convert.ok"] == 2 and rec.counts["quality.failed"] == 1 and rec.counts["quality.ok"] == 1
    v = view(root)
    bad, good = v[rows[0]["doc_id"]], v[rows[1]["doc_id"]]
    sb = snap_of(bad)
    assert (sb["text_status"], sb["text_error_class"], sb["quality"]["bucket"]) == (
        "failed_permanent", "quality_stub", "failed")
    assert sb["quality"]["converter"].split("@")[0] in ("html-min", "trafilatura", "fetchpdf-md")
    assert (bad["content"]["current_text_status"], bad["content"]["current_text_quality"]) == (
        "convert_failed", "failed")
    assert (good["content"]["current_text_status"], good["content"]["current_text_quality"]) == ("ok", "ok")
    by = {r.check: r for r in run_checks(root, names=["V-content", "status_pairing", "attempts_monotone",
                                                       "block_ownership", "V-chain-linear"])}
    assert {n: r.status for n, r in by.items()} == dict.fromkeys(by, "ok"), {n: r.items for n, r in by.items()}

    rec = go()
    assert rec.outputs == {} and "quality.measured" not in rec.counts

    rec = go(quality={"fail": False})
    assert rec.counts == rec.counts | {"quality.rejudged": 2, "quality.flagged": 1, "quality.ok": 1}
    bad = view(root)[rows[0]["doc_id"]]
    assert (bad["content"]["current_text_status"], bad["content"]["current_text_quality"]) == ("ok", "flagged")
    assert snap_of(bad)["convert_attempts"] == 1  # re-judged, never re-converted

    with pytest.raises(AssertionError, match="unknown"):
        go(quality={"thresholds": {"nope.fail": 1}})


# ---------------------------------------------------------------- Article in Press (round 4, P)
AIP_URL = "https://www.nature.com/articles/s41598-026-72982-1_reference.pdf"


@pytest.mark.parametrize("name", sorted(CASES))
def test_article_in_press_detection_on_the_real_snippets(name: str) -> None:
    """The three Scientific Reports snippets (``sbkp3dcezxtvn``, D206) carry the banner or its running
    head in their first 5,000 chars; no other real text does (the dev store's 839 text blobs: only the
    two Article-in-Press PDFs, 2026-10-12). The text rule is for paper bodies only."""
    text, facts = fixture(name)
    assert q.in_press(text, facts.role) is name.startswith("sci-reports-")
    assert not q.in_press(text, "page")


def test_article_in_press_url_and_phrases() -> None:
    assert q.in_press_url(AIP_URL) and q.in_press_url("http://nature.com/articles/x_reference.pdf?download=1")
    assert not q.in_press_url("https://www.nature.com/articles/s41598-026-72982-1.pdf")
    assert not q.in_press_url("https://example.org/articles/x_reference.pdf") and not q.in_press_url(None)
    assert q.version_of_record_url(AIP_URL) == "https://www.nature.com/articles/s41598-026-72982-1.pdf"
    assert q.version_of_record_url("https://journal.test/1.pdf") is None
    assert q.in_press(None, "page", [None, AIP_URL])  # the URL rule holds for any role, without the text
    body = BODY * 4
    assert not q.in_press(body, "full_text")
    assert q.in_press("Journal Pre-proof\n" + body, "full_text")
    assert q.in_press("ARTICLE IN PRESS\n" + body, "full_text")
    assert not q.in_press("Smith, J. Results on oversight (article in press, 2026).\n" + body, "full_text")
    assert not q.in_press(body + "\nArticle in Press", "full_text")  # past the first page


def test_article_in_press_flag_never_fails_and_survives_a_rejudge(tmp_path: Path) -> None:
    """The flag makes an ``ok`` text ``flagged`` and never fails one; a re-judge from stored metrics
    (text blob gone) keeps it. An in-band text has no fetch role: nothing is made due."""
    text, _ = fixture("sci-reports-article")
    v = q.with_in_press(q.Verdict("ok", [], [], None))
    assert (v.bucket, v.flags) == ("flagged", ["article_in_press"])
    assert q.with_in_press(q.Verdict("failed", ["stub"], ["stub"], "quality_stub")).bucket == "failed"
    e = env_for(tmp_path)
    row = inband_row(e.blobs, 1, text * 3, role="full_text")
    run_quality([row], e, gate_of({}))
    s = snap_of(row)
    assert "article_in_press" in s["quality"]["flags"] and s["quality"]["bucket"] == "flagged"
    assert e.counts["quality.flag.article_in_press"] == 1 and "refresh.article_in_press" not in e.counts
    assert all(r["retry_after"] is None for r in row["content"]["roles"])
    (tmp_path / "blobs" / s["text"]["sha256"]).unlink()
    run_quality([row], e, gate_of({"quality": {"fail": False}}))
    assert e.counts["quality.rejudged"] == 1 and "article_in_press" in snap_of(row)["quality"]["flags"]
