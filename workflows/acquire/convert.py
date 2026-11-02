"""Substage ``convert`` (§3.1, §5.5, §7.9; D9, D10, D39): raw → text at the plan's pin, no network.
ADAPTed from v3's ``convert.docling`` (``workflows/convert/docling/main.py``, retired): the
RLIMIT_DATA-capped child per item, the attempt-before-invoke charge, the attempt cap, the routing;
the ladder itself is ``stack/conversion/{ladder,worker}.py`` (v3 COPY).

**Seam** (pinned by T3.1; the stops added by T3.5-f): ``run_convert(rows, env[, converted], *,
max_rows, max_minutes, started) -> complete`` over the in-scope working rows (full ``documents``
row dicts, changed in place) and the run's :class:`~workflows.acquire.lanes.LaneEnv`; ``quality``
and ``point`` run after it and read only the snapshot fields. ``converted`` (optional; round 3, Q,
replacing T3.5's ``quality`` verdict map) receives a :class:`~workflows.acquire.quality.Converted`
(converter stamp, PDF page count, truncation signal) for every snapshot converted ``ok`` in this
run, keyed ``(doc_id, snapshot_id)``; the ``quality`` substage measures those texts with it
(the ladder's own v3 ``text_quality`` verdict is no longer recorded). A charged attempt clears the
snapshot's ``quality``. Without a convert config (``env.acquire`` or its ``convert`` absent:
library callers) nothing is converted.

**Convert configs** (:data:`CONFIGS`; the plan names one in ``acquire.convert.config``):

==================  ==========  ========  ==========================================  ============
config              PDF entry   heap cap  PDF ladder                                  HTML tool
==================  ==========  ========  ==========================================  ============
``pymupdf4llm-v1``  tier 2      16 GB     pymupdf4llm → pymupdf raw text (dev, D39)   trafilatura
``docling-v2``      tier 1      48 GB     docling → pymupdf4llm → pymupdf raw text    trafilatura
==================  ==========  ========  ==========================================  ============

Tier entry 2 disables docling in the child for every kind (``STACK_CONVERT_NO_DOCLING``), so a
``pymupdf4llm-v1`` text never depends on docling. HTML/XML take the markup path (fetchpdf for
publisher markup from the ``oa`` lane, docling's backend under ``docling-v2``, then the light
path), plain text passes through, under any config, as in v3. The light path is ``trafilatura``'s
main-content extraction (the ``convert`` group; it drops site menus and footers), else the stdlib
floor ``html-min``, which keeps every navigation menu; under ``pymupdf4llm-v1`` it converts every
blog and forum page. The heap caps are T3.5b's measurement: under 16 GB docling ``bad_alloc``s in
preprocess and the ladder silently falls to tier 2; 48 GB converts.

**The pin** (:func:`pin_of` over :func:`pin_document`): ``sha256`` over the canonical JSON (sorted
keys, no whitespace) of ``{"config": name, "tier": entry, "mem_gb": cap, "tools": {dist: version |
"absent"}}`` with the config's tool distributions (``pymupdf4llm-v1``: ``pymupdf4llm``,
``pymupdf``, ``trafilatura``; ``docling-v2``: also ``docling``, ``"absent"`` too when
``STACK_CONVERT_NO_DOCLING`` disables it). ``trafilatura`` is in both because the light path runs
under both: installing it changes the texts of HTML pages, so it must change the pin. Versions come
from ``importlib.metadata`` in the parent, which runs the child with the same interpreter
(``sys.executable``), so they are the child's. A version bump, a cap change or installing a tool
changes the pin, and every current candidate re-converts, PDFs included (D9: the change clears the
fields and resets ``convert_attempts``; X24 allows the reset on a changed config). An unknown config
name is refused (:class:`~stack.stage_runner.StageError`).

**Structured text** (round 5, T-RUN B8; the acquire line's ``options.structured: {jats: bool,
arxiv_html: bool}``, :func:`workflows.acquire.point.structured_of`). Off (absent, or both false): the
pin document, the scope and the child are exactly as above. On: the pin document gains
``"structured": {"jats": …, "arxiv_html": …}`` and the tools :data:`STRUCTURED_TOOLS` (``fetchpdf``,
its version with the git commit it was installed from, and ``lxml``, which :func:`stack.conversion.
ladder.rewrite_mathml` needs), so the guard refuses a venv without them like any other tool; the
scope is :func:`workflows.acquire.point.needed` (the enabled structured snapshots ranked ahead of the
current candidate, and that candidate: the PDF fallback is converted too, so the quality gate can
compare the two and the pointer can fall back at once); and every child runs with
``latexml: true`` in its request (no environment switch), which sends a LaTeXML page (the arXiv ``html`` role) to
the docling-free :func:`stack.conversion.ladder.latexml_to_text`. JATS (``xml``) takes the markup
path as before: fetchpdf's JATS converter, then docling's backend (``docling-v2``), then the light
path. Counts per structured role: ``convert.role.<role>.<status>``.

**The pin guard** (:func:`guard_pin`, called by ``acquire.run`` before any substage, ``--only
point`` included; R4-1): a pin computed in a venv that lacks a tool of the config differs from the
stored one, so ``point`` would hide every converted text (``convert_pending``) and ``convert``
would re-convert everything at degraded tiers under the config's name. The run therefore refuses
(:class:`~stack.stage_runner.EnvironmentRefused`: the run ends ``failed[environment]``, naming
the config, the absent tools, the computed pin and the remedy: ``uv sync --group convert-pdf``
for ``pymupdf4llm-v1``, ``--group convert`` for ``docling-v2``) while any tool of the config is
``absent``, unless the stage line sets ``options.allow_degraded_convert:
true`` (a warning, ``counts["convert.degraded_allowed"] = 1``). When the plan sets
``acquire.convert.expect_pin`` (a hex prefix) and the computed pin does not start with it, the run
refuses too, printing the pin document; ``allow_degraded_convert`` does not lift that refusal
(change or drop ``expect_pin``). Library callers (:func:`run_convert` with a ready
:class:`~workflows.acquire.lanes.LaneEnv`) are not guarded.

**Scope** (one snapshot per row; with ``options.structured``, :func:`workflows.acquire.point.needed`,
Structured text above): the pointer's current candidate,
:func:`workflows.acquire.point.candidates` ``[0]`` (§5.5: only its text is ever consulted; older
occurrences, masked members and never-text roles never are). Every other snapshot's conversion
fields are left as they are, so a pin change re-converts current candidates only. The candidate
is converted when it is not an in-band entry carrying its collector's text
(``convert_pin`` NULL with ``text`` set, D10). Converted under another config, it
is reset first (``counts["convert.reset"]``). At the pin it converts when it has no text
and its status is NULL or ``failed_transient``; ``ok``, ``failed_permanent`` and ``quarantined``
are final at the pin, with one exception (D661, :func:`garbled_retry`): a PDF whose text
the quality gate failed ``quality_garbled`` (its verdict's ``converter`` a layout tier: a font
without a Unicode map comes out as U+FFFD per glyph, e.g. arXiv 1908.09203's monospace font,
27 per 1,000 chars) is converted **once more by the raw-text tier alone** (the child's request
``pdf_raw``, :func:`stack.conversion.ladder.convert_pdf` ``raw_only``; charged like any attempt,
``counts["convert.garbled_retry"]``), which keeps the prose and renders the unmapped glyphs as the
font's own codes (the gate's ``glyph_lines``/``binary`` checks then flag them). Its verdict is made
from the ``pymupdf@…+tier3`` text (``quality.tier = 3``), so the rule does not fire again; the pin is unchanged (the first
conversion of any PDF is the ladder's as before). A transient failure of the second conversion
leaves no ``quality_garbled`` mark: the next attempt is the ladder's, whose verdict re-arms the
rule (bounded by the cap only for transients). A PDF when none of the config's PDF tools is importable is left untouched
(not charged; ``counts["convert.no_tool"]``); a raw blob missing from the store likewise
(``counts["convert.blob_missing"]``).

**Stops** (§3.3, as in ``fetch``; D392 (e2e-fix), defaults D483): ``max_rows`` caps the rows whose
candidate is converted (charged) in this run (``acquire`` passes ``options.convert_max_rows``; absent:
no cap), ``max_minutes`` the wall clock from ``started`` (``acquire`` passes
``options.convert_max_minutes``, absent: ``budget.max_minutes``, and no ``started``: the clock starts
with convert, after the fetch). Both are checked before a row that needs converting; the
substage then stops, the remaining candidates stay as they are (``convert_pending``, not charged)
and :func:`run_convert` returns ``False`` (the run ends ``ok`` with ``scope_complete = false``;
``counts["convert.stopped.limit|budget"]``). A drain (D570, D571; ``drain``) stops it the same
way (``convert.stopped.drain``); the children in flight get the drain's bounded wait, and one still
running then is abandoned: its snapshot is put back as it was before the charge (``convert.abandoned``),
so the next run converts it.

**Parallelism** (``options.convert_workers``, an int ≥ 1, default 1): up to that many children
run at once. Choosing, the stop checks and the charge stay in the calling thread in the order of
``rows`` (acquire's scope order, ``options.order``, D391), exactly as with one worker; a pool
thread only runs :func:`convert_one` (it waits on its child), and each outcome is applied to its
snapshot in the calling thread, in completion order. The stop checks run once a worker is free, so
with one worker they see the same clock as the sequential loop. A stop ends submission; items
already in flight finish and are applied. Each child carries its own heap cap, so
``convert_workers`` × the config's cap bounds the children's heap.

**Threads** (``options.convert_threads``, an int ≥ 1, default :data:`DEFAULT_THREADS` = 2; T3.x-f4):
each child caps its native thread pools (OpenMP/BLAS environment, onnxruntime sessions; see
``stack/conversion/worker.py``) at that number before importing a converter, so
``convert_workers`` × ``convert_threads`` bounds the conversion threads (T3.6: an uncapped
pymupdf4llm child ran ~67 threads, three of them loaded 22 cores to ~80). Not in the pin: the
same PDF converted at 1, 2 and uncapped threads gave identical markdown (measured on one paper).

**Per snapshot**: the attempt is charged on the row first (``convert_attempts += 1``,
``convert_pin := pin``), then the ladder runs in a fresh RLIMIT_DATA-capped child
(:func:`convert_one`, wall-clock ``options.convert_timeout_s``, default 300 s). Outcomes, §2.6:

=======================================  =====================================================
child result                             ``text_status[text_error_class]``
=======================================  =====================================================
markdown                                 ``ok``: text → ``blobs/``, ``text`` (its size, D690)
``ok`` with an empty body                ``failed_permanent[too_short]``
ladder ``failed``                        ``failed_permanent[too_short]`` (all PDF tiers
                                         near-empty) or ``[parse_error]`` (the ladder raised)
markdown that does not encode as UTF-8   ``failed_permanent[parse_error]``,
                                         ``counts["convert.encode_error"]`` (D586)
no result (the child died)               ``failed_transient[crashed]``
a result that does not parse             ``failed_transient[crashed]`` (D586)
wall-clock timeout                       ``failed_transient[timeout]``
transient at ``convert_attempts == 3``   ``quarantined[cap_reached]``
=======================================  =====================================================

``failed_permanent`` is not retried at the same pin (the ladder is deterministic per bytes and
pin); ``failed_transient`` is retried by the next run that has the row in scope.
A converter's surrogate code points (they arrive as JSON ``\\udXXX`` escapes) are repaired in the
parent before the text is stored, :func:`~stack.core.hashes.utf8_safe` (a pair becomes its
character, a lone one U+FFFD; D586), so the encode row above is a guard, not an expected outcome.

**What the charge guarantees in this store.** The charged attempt and the outcome land in the
same row of the run's part file. Every child invocation of a run that publishes is charged: the
count is on the row before the child starts, so a child that dies, hangs or writes no result still
leaves its attempt (and a poison input reaches the cap in three runs). A parent that dies
publishes nothing (§7.9): neither the attempt nor any outcome lands, and the snapshot reads as
before. The heavy work never runs in the parent, so a poison input cannot kill it repeatedly.

**Attribution**: the converter stamp of the tier actually used (``<tool>@<version>+tier<k>``) is
counted per run as ``counts["convert.converter.<stamp>"]`` (a silent tier fallback is visible
there; ``SNAPSHOT`` has no field for it), with ``convert.ok``, ``convert.failed_permanent``,
``convert.failed_transient``, ``convert.quarantined``, ``convert.seconds`` (the sum of the items'
seconds), ``convert.wall_seconds`` (the substage's wall clock from its first invocation to its last
outcome) and ``convert.entry_tool_absent`` (the config's entry tier is not importable).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, MutableMapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path
from typing import Any, NamedTuple

from stack.conversion import ladder
from stack.core.hashes import utf8_safe
from stack.schemas.structs import text_blob
from stack.schemas.vocab import ERROR_CLASSES, NEVER_TEXT_ROLES
from stack.stage_runner import NO_DRAIN, DrainCheck, DrainPool, EnvironmentRefused, StageError
from stack.store.blobs import BlobNotFound
from workflows.acquire.lanes import LaneEnv
from workflows.acquire.point import (
    Structured,
    candidates,
    needed,
    structured_of,
    text_of,
)
from workflows.acquire.quality import Converted

log = logging.getLogger(__name__)

#: Conversion attempts per snapshot and pin (§3.1); the third transient failure quarantines.
CAP = 3
#: Wall-clock cap per item (v3); a hang is the item's ``failed_transient[timeout]``.
DEFAULT_TIMEOUT_S = 300.0
#: Native threads per conversion child (module docstring, Threads).
DEFAULT_THREADS = 2
_WORKER = Path(__file__).resolve().parents[2] / "stack" / "conversion" / "worker.py"
_FINAL = frozenset({"ok", "failed_permanent", "quarantined"})
#: The importable module of each tool distribution.
_MODULE = {"docling": "docling", "pymupdf4llm": "pymupdf4llm", "pymupdf": "fitz", "trafilatura": "trafilatura",
           "fetchpdf": "fetchpdf", "lxml": "lxml"}
#: The tools a config with ``options.structured`` adds to its pin (module docstring, Structured text).
STRUCTURED_TOOLS: tuple[str, ...] = ("fetchpdf", "lxml")

Row = dict[str, Any]


class ConvertConfig(NamedTuple):
    """A named convert config: PDF tier entry, the child's heap cap, the tools in its pin."""

    name: str
    tier: int
    mem_gb: float
    tools: tuple[str, ...]


CONFIGS: dict[str, ConvertConfig] = {c.name: c for c in (
    ConvertConfig("pymupdf4llm-v1", 2, 16.0, ("pymupdf4llm", "pymupdf", "trafilatura")),
    ConvertConfig("docling-v2", 1, 48.0, ("docling", "pymupdf4llm", "pymupdf", "trafilatura")),
)}


def config_named(name: str) -> ConvertConfig:
    cfg = CONFIGS.get(name)
    if cfg is None:
        raise StageError(f"acquire.convert.config {name!r} is not a convert config ({sorted(CONFIGS)})")
    return cfg


def _usable(tool: str) -> bool:
    if tool == "docling":
        return ladder.docling_available()  # honours STACK_CONVERT_NO_DOCLING
    return importlib.util.find_spec(_MODULE[tool]) is not None


def tool_versions(cfg: ConvertConfig, structured: Structured | None = None) -> dict[str, str]:
    """``{distribution: version | 'absent'}`` of the config's tools as the child will see them
    (with ``structured`` on, :data:`STRUCTURED_TOOLS` too)."""
    out: dict[str, str] = {}
    tools = cfg.tools + (STRUCTURED_TOOLS if structured is not None and structured.on else ())
    for tool in tools:
        try:
            out[tool] = _version(tool) if _usable(tool) else "absent"
        except importlib.metadata.PackageNotFoundError:
            out[tool] = "absent"
    return out


def _version(tool: str) -> str:
    """The distribution's version. A structured-text tool installed from a VCS URL (``fetchpdf``: a
    git commit in ``pyproject.toml``, whose version string never changes) is
    ``<version>+<commit[:12]>``; the configs' own tools stay the bare version (their pins unchanged)."""
    version = importlib.metadata.version(tool)
    if tool not in STRUCTURED_TOOLS:
        return version
    try:
        direct = json.loads(importlib.metadata.distribution(tool).read_text("direct_url.json") or "null")
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError, OSError):
        direct = None
    commit = ((direct or {}).get("vcs_info") or {}).get("commit_id") if isinstance(direct, dict) else None
    return f"{version}+{commit[:12]}" if commit else version


def pin_document(cfg: ConvertConfig, structured: Structured | None = None) -> dict[str, Any]:
    """The document :func:`pin_of` hashes (module docstring, The pin; ``structured`` on adds its
    flags and tools, off leaves the document as it was)."""
    if structured is None or not structured.on:  # off: the document (and the call) exactly as before
        return {"config": cfg.name, "tier": cfg.tier, "mem_gb": cfg.mem_gb, "tools": tool_versions(cfg)}
    return {"config": cfg.name, "tier": cfg.tier, "mem_gb": cfg.mem_gb, "tools": tool_versions(cfg, structured),
            "structured": structured._asdict()}


def pin_of(cfg: ConvertConfig, structured: Structured | None = None) -> str:
    """The convert pin of ``cfg`` (with ``structured``) in this environment (module docstring)."""
    return _pin_hash(pin_document(cfg, structured))


def _pin_hash(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def convert_pin(acquire: Any, structured: Structured | None = None) -> str | None:
    """The plan's convert pin (``acquire`` is the plan's ``AcquireConfig``; ``structured``, the
    acquire line's :func:`~workflows.acquire.point.structured_of`); ``None`` without a convert config."""
    cfg = getattr(acquire, "convert", None)
    return None if cfg is None else pin_of(config_named(cfg.config), structured)


#: The ``pyproject.toml`` dependency group that installs each config's tools.
SYNC_GROUP = {"pymupdf4llm-v1": "convert-pdf", "docling-v2": "convert"}


def guard_pin(acquire: Any, options: Mapping[str, Any], counts: MutableMapping[str, Any]) -> str | None:
    """The plan's convert pin (:func:`convert_pin`), or an
    :class:`~stack.stage_runner.EnvironmentRefused` (the run ends ``failed[environment]``) when
    this environment cannot produce the texts it names (module docstring, The pin guard).
    ``options``: the acquire stage line's; ``counts``: the run's."""
    try:
        structured = structured_of(options)
    except ValueError as exc:
        raise StageError(f"acquire: {exc}") from exc
    conv = getattr(acquire, "convert", None)
    if conv is None:
        if structured.on:
            raise StageError("acquire: options.structured needs a convert config (acquire.convert.config)")
        return None
    cfg = config_named(conv.config)
    doc = pin_document(cfg, structured)
    pin = _pin_hash(doc)
    allow = options.get("allow_degraded_convert", False)
    if not isinstance(allow, bool):
        raise StageError(f"acquire: options.allow_degraded_convert must be true or false, got {allow!r}")
    absent = sorted(t for t, v in doc["tools"].items() if v == "absent")
    if absent and allow:
        log.warning("acquire: convert config %s runs DEGRADED without %s (pin %s; options.allow_degraded_convert)",
                    cfg.name, absent, pin)
        counts["convert.degraded_allowed"] = 1
    elif absent:
        hint = (" (STACK_CONVERT_NO_DOCLING is set: unset it)"
                if "docling" in absent and ladder._env_flag("STACK_CONVERT_NO_DOCLING") else "")
        raise EnvironmentRefused(
            f"acquire: convert config {cfg.name!r} needs {absent}, absent in this environment{hint}; its pin "
            f"here would be {pin}, so every text converted at the stored pin would read convert_pending and "
            f"convert would re-convert at degraded tiers. Run from a venv with `uv sync --group "
            f"{SYNC_GROUP.get(cfg.name, 'convert')}`, or set options.allow_degraded_convert: true on the acquire line")
    expect = getattr(conv, "expect_pin", None)
    if expect is not None and not pin.startswith(expect):
        raise EnvironmentRefused(
            f"acquire: the convert pin here is {pin}, not acquire.convert.expect_pin {expect}…; pin document "
            f"{json.dumps(doc, sort_keys=True)}. A tool version, cap or config change re-converts every current "
            f"text: restore the expected environment, or update expect_pin if the change is intended")
    return pin


def _pdf_tools(cfg: ConvertConfig) -> list[str]:
    """The config's PDF tiers that are importable (docling only when the entry is tier 1)."""
    tiers = ("docling", "pymupdf4llm", "pymupdf") if cfg.tier <= 1 else ("pymupdf4llm", "pymupdf")
    return [t for t in tiers if _usable(t)]


# ---------------------------------------------------------------- the child
class ConvertOutcome(NamedTuple):
    """One item's result as the parent sees it. ``status`` ∈ ``ok``, ``failed`` (the ladder's
    clean failure), ``crashed`` (no result: the child died), ``timeout``. ``pages``: a PDF's page
    count; ``truncated``: the ladder's truncation signal (docling page errors); both ``None`` when
    the ladder gave none."""

    status: str
    markdown: str = ""
    converter: str = ""
    error: str | None = None
    error_class: str | None = None
    pages: int | None = None
    truncated: bool | None = None


def convert_one(raw: bytes, media_type: str | None, *, mem_gb: float, timeout: float, tier: int = 1,
                origin: str | None = None, threads: int = DEFAULT_THREADS, latexml: bool = False,
                pdf_raw: bool = False) -> ConvertOutcome:
    """Convert one item in a fresh RLIMIT_DATA-capped child (``stack/conversion/worker.py``) whose
    thread pools are capped at ``threads``; ``latexml`` goes into the child's request (module docstring,
    Structured text), ``pdf_raw`` too (Scope: a garbled PDF's second conversion). A missing result file (the child
    died) is ``crashed``, a wall-clock overrun ``timeout``; either is attributed to this item only, and
    so is a result file that does not parse (a child killed mid-write: ``crashed``, D586). The
    markdown and error pass through :func:`~stack.core.hashes.utf8_safe` (D586): a converter's
    surrogates arrive as JSON ``\\udXXX`` escapes and would not encode as UTF-8."""
    with tempfile.TemporaryDirectory(prefix="convert-") as td:
        tmp = Path(td)
        raw_path, out_path, req_path = tmp / "raw.bin", tmp / "result.json", tmp / "request.json"
        raw_path.write_bytes(raw)
        req_path.write_text(json.dumps({"raw_path": str(raw_path), "media_type": media_type,
                                        "mem_gb": mem_gb, "out_path": str(out_path), "origin": origin,
                                        "tier": tier, "threads": threads, "latexml": latexml,
                                        "pdf_raw": pdf_raw}))
        try:
            proc = subprocess.run([sys.executable, str(_WORKER), str(req_path)], timeout=timeout,
                                  capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            return ConvertOutcome("timeout", error=f"timeout after {timeout:.0f}s")
        if not out_path.exists():
            tail = proc.stderr.decode("utf-8", "replace")[-500:] if proc.stderr else ""
            return ConvertOutcome("crashed", error=f"worker exit {proc.returncode}; stderr: {tail}")
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError: a partial or corrupt result
            return ConvertOutcome("crashed", error=f"worker exit {proc.returncode}; result unreadable: {exc}")
    pages, truncated = data.get("pages"), data.get("truncated")
    error = data.get("error")
    return ConvertOutcome(status=str(data.get("status") or "failed"),
                          markdown=utf8_safe(str(data.get("markdown") or "")),
                          converter=str(data.get("converter") or ""),
                          error=utf8_safe(str(error)) if error is not None else None,
                          error_class=data.get("error_class"), pages=int(pages) if pages else None,
                          truncated=bool(truncated) if truncated is not None else None)


def settle(out: ConvertOutcome, attempts: int) -> tuple[str, str | None]:
    """``(text_status, text_error_class)`` of a non-``ok`` outcome after ``attempts`` charged
    attempts (module docstring table)."""
    if out.status == "ok":  # a result without a body
        return "failed_permanent", "too_short"
    if out.status == "failed":
        cls = out.error_class if out.error_class in ERROR_CLASSES["failed_permanent"] else "parse_error"
        return "failed_permanent", cls
    if attempts >= CAP:
        return "quarantined", "cap_reached"
    return "failed_transient", "timeout" if out.status == "timeout" else "crashed"


# ---------------------------------------------------------------- the substage
def _reset(snap: Row) -> None:
    snap.update(text=None, text_status=None, text_error_class=None, convert_attempts=0,
                convert_pin=None, quality=None)


def _pending(snap: Row, pin: str, structured: frozenset[str] = frozenset()) -> bool:
    """``True`` when ``snap`` needs converting at ``pin`` (module docstring, Scope; ``structured``:
    the enabled structured roles, otherwise never text); pure (a snapshot of another config is
    reset by the caller once it is charged)."""
    if snap["kind"] not in ("raw", "inband") or (snap["content_role"] in NEVER_TEXT_ROLES
                                                  and snap["content_role"] not in structured):
        return False
    if snap["convert_pin"] is None:
        if snap["kind"] == "inband" and snap.get("text"):
            return False  # the collector's text (D10)
    elif snap["convert_pin"] != pin:
        return True
    if snap.get("text") and snap.get("text_status") == "ok":
        return False
    if garbled_retry(snap, pin):
        return True
    return snap.get("text_status") not in _FINAL


#: The quality failure a second conversion by the raw-text tier may cure (module docstring, Scope).
GARBLED = "quality_garbled"


def garbled_retry(snap: Mapping[str, Any], pin: str | None) -> bool:
    """``snap`` is a PDF converted at ``pin`` whose ``quality_garbled`` verdict was made from a layout
    tier's text (its verdict's ``quality.tier`` is not :data:`~stack.conversion.ladder.RAW_TIER`, D690):
    :func:`run_convert` converts it once more by the raw-text tier (module docstring, Scope). Pure."""
    if pin is None or snap.get("kind") != "raw" or snap.get("convert_pin") != pin:
        return False
    if (snap.get("text_status"), snap.get("text_error_class")) != ("failed_permanent", GARBLED):
        return False
    if ladder.classify(snap["raw"]["media_type"], b"") != "pdf":
        return False
    tier = (snap.get("quality") or {}).get("tier")
    return tier is not None and tier != ladder.RAW_TIER


def current_candidate(row: Row) -> Row | None:
    """The snapshot the pointer would choose for ``row`` (§5.5), or ``None``."""
    cands = candidates(row["content"], row["identity"]["members"])
    return cands[0] if cands else None


def to_convert(row: Row, pin: str, structured: frozenset[str]) -> list[Row]:
    """The snapshots of ``row`` this substage would convert at ``pin`` (module docstring, Scope;
    without ``structured`` roles at most the current candidate)."""
    if not structured:
        cur = current_candidate(row)
        return [cur] if cur is not None and _pending(cur, pin) else []
    keep = lambda s: text_of(s, pin)[1] in ("ok", "convert_pending")  # not failed at the pin
    return [s for s in needed(row["content"], row["identity"]["members"], structured, keep)
            if _pending(s, pin, structured)]


def awaits_text(row: Row, pin: str, structured: frozenset[str] = frozenset()) -> bool:
    """``True`` when a snapshot :func:`to_convert` would convert at ``pin`` has no converted text
    yet: never converted (``text_status`` NULL, e.g. an in-band body a stop left
    ``convert_pending``) or ``failed_transient``, under any config. A text converted under an older
    pin does not count (D403: :mod:`workflows.acquire.scope`'s ``convert`` clause). A garbled PDF
    awaiting its raw-text conversion counts too (:func:`garbled_retry`)."""
    return any((not s.get("text") and s.get("text_status") in (None, "failed_transient"))
               or garbled_retry(s, pin) for s in to_convert(row, pin, structured))


def _workers(options: Mapping[str, Any]) -> int:
    """``options.convert_workers`` (module docstring, Parallelism); anything but an int ≥ 1 is refused."""
    return _positive_int(options, "convert_workers", 1)


def _threads(options: Mapping[str, Any]) -> int:
    """``options.convert_threads`` (module docstring, Threads); anything but an int ≥ 1 is refused."""
    return _positive_int(options, "convert_threads", DEFAULT_THREADS)


def _positive_int(options: Mapping[str, Any], key: str, default: int) -> int:
    n = options.get(key, default)
    if n is None:
        return default
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise StageError(f"convert: options.{key} must be an int >= 1, got {n!r}")
    return n


class _Item(NamedTuple):
    """An invoked item awaiting its outcome: where to apply it and the charged attempt."""

    row: Row
    snap: Row
    attempts: int
    before: Row  # the snapshot before the charge: put back when a drain abandons the item


def _timed(raw: bytes, media_type: str | None, **kw: Any) -> tuple[ConvertOutcome, float]:
    """:func:`convert_one` and its seconds (run in a pool thread; resolved at call time so the
    module attribute is the one invoked)."""
    t = time.monotonic()
    out = convert_one(raw, media_type, **kw)
    return out, time.monotonic() - t


def run_convert(rows: list[Row], env: LaneEnv, converted: MutableMapping[tuple[str, str], Converted] | None = None,
                *, max_rows: int | None = None, max_minutes: float | None = None,
                started: float | None = None, drain: DrainCheck = NO_DRAIN) -> bool:
    """Convert each row's current candidate (in place, chosen and charged in the order of ``rows``,
    the caller's scope order, D391) at
    ``env.convert_pin``, up to ``options.convert_workers`` children at once. ``started`` is the
    stage's ``time.monotonic()`` start (default: now). Returns ``False`` when a stop
    (``max_rows``, ``max_minutes``, a ``drain``) left a candidate unconverted, else ``True``."""
    plan_cfg = getattr(env.acquire, "convert", None)
    if plan_cfg is None:
        return True
    cfg = config_named(plan_cfg.config)
    pin = env.convert_pin
    structured = structured_of(env.options)
    roles = structured.roles
    if pin != pin_of(cfg, structured):
        raise StageError(f"convert: env.convert_pin {pin!r} is not the pin of {cfg.name!r} here")
    child_kw: dict[str, Any] = {"latexml": True} if structured.on else {}
    timeout = float(env.options.get("convert_timeout_s") or DEFAULT_TIMEOUT_S)
    workers = _workers(env.options)
    threads = _threads(env.options)
    entry = "docling" if cfg.tier <= 1 else "pymupdf4llm"
    if not _usable(entry):
        env.counts["convert.entry_tool_absent"] = 1
    pdf_tools = _pdf_tools(cfg)
    t0 = time.monotonic() if started is None else started
    seconds = 0.0
    wall_from: float | None = None
    converted_rows = 0
    stop: str | None = None
    inflight: dict[Future[tuple[ConvertOutcome, float]], _Item] = {}

    def apply(fut: Future[tuple[ConvertOutcome, float]]) -> None:
        nonlocal seconds
        item = inflight.pop(fut)
        out, secs = fut.result()  # a raise in convert_one fails the stage, as in the sequential loop
        seconds += secs
        row, snap, attempts, _before = item
        body: bytes | None = None
        if out.status == "ok" and out.markdown.strip():
            try:
                body = out.markdown.encode("utf-8")
            except UnicodeEncodeError as exc:  # D586: this row's text, not the run (convert_one repairs it)
                env.count("convert.encode_error")
                out = out._replace(status="failed", error=f"converted text does not encode as UTF-8: {exc}",
                                   error_class="parse_error")
        if body is not None:
            snap.update(text=text_blob(env.blobs.put(body), len(body), snap["raw"]), text_status="ok",
                        text_error_class=None)
            env.count("convert.ok")
            if snap["content_role"] in roles:
                env.count(f"convert.role.{snap['content_role']}.ok")
            env.count(f"convert.converter.{out.converter or 'unknown'}")
            if converted is not None:
                converted[(row["doc_id"], snap["snapshot_id"])] = Converted(out.converter or None, out.pages,
                                                                            out.truncated)
            return
        status, cls = settle(out, attempts)
        snap.update(text=None, text_status=status, text_error_class=cls)
        env.count(f"convert.{status}")
        if snap["content_role"] in roles:
            env.count(f"convert.role.{snap['content_role']}.{status}")
        log.warning("convert %s/%s attempt %d: %s[%s]: %s", row["doc_id"], snap["snapshot_id"], attempts,
                    status, cls, (out.error or "")[:500])

    def await_inflight(until: int) -> None:
        """Apply outcomes until at most ``until`` items are in flight; under a drain, return early
        (``until`` > 0: a free worker no longer matters), and past its deadline abandon the rest."""
        while len(inflight) > until:
            if drain.stopping():
                if until > 0:
                    return
                if drain.expired():
                    abandon()
                    return
            done, _ = wait(list(inflight), timeout=drain.remaining(1.0), return_when=FIRST_COMPLETED)
            for fut in sorted(done, key=lambda f: inflight[f].row["doc_id"]):
                apply(fut)

    def abandon() -> None:
        """D571: the items still in flight are not attempted: their snapshots as before the charge."""
        env.count("convert.abandoned", len(inflight))
        for fut, item in list(inflight.items()):
            fut.cancel()
            item.snap.clear()
            item.snap.update(item.before)
        inflight.clear()
        pool.abandoned = True

    with DrainPool(max_workers=workers, thread_name_prefix="convert") as pool:
        for row in rows:
            snaps = to_convert(row, pin, roles)
            if not snaps:
                continue
            await_inflight(pool.workers - 1)  # a worker is free before the stops are checked (D786)
            if max_rows is not None and converted_rows >= max_rows:
                stop = "limit"
            elif max_minutes is not None and time.monotonic() - t0 >= max_minutes * 60:
                stop = "budget"
            elif drain.stopping():
                stop = "drain"
            if stop is not None:
                break
            charged = False
            for snap in snaps:
                try:
                    raw = env.blobs.get(snap["raw"]["sha256"])
                except BlobNotFound:
                    env.count("convert.blob_missing")
                    log.warning("convert %s/%s: raw blob %s missing", row["doc_id"], snap["snapshot_id"],
                                snap["raw"]["sha256"])
                    continue
                if ladder.classify(snap["raw"]["media_type"], raw) == "pdf" and not pdf_tools:
                    env.count("convert.no_tool")
                    continue
                before = dict(snap)
                raw_route = garbled_retry(snap, pin)  # read before the charge clears the verdict
                if raw_route:
                    env.count("convert.garbled_retry")
                if snap["convert_pin"] not in (None, pin):
                    _reset(snap)
                    env.count("convert.reset")
                charged = True
                attempts = int(snap["convert_attempts"] or 0) + 1
                snap.update(convert_attempts=attempts, convert_pin=pin, quality=None)  # charged first
                lane = next((r.get("lane") for r in row["content"]["roles"] or []
                             if (r.get("source_id"), r["content_role"]) == (snap["source_id"], snap["content_role"])),
                            None)
                if wall_from is None:
                    wall_from = time.monotonic()
                await_inflight(pool.workers - 1)  # one child per free worker, also between a row's snapshots
                if drain.stopping():  # D570: no new child; this snapshot as it was
                    snap.clear()
                    snap.update(before)
                    stop = "drain"
                    break
                fut = pool.submit(_timed, raw, snap["raw"]["media_type"], mem_gb=cfg.mem_gb, timeout=timeout,
                                  tier=cfg.tier, origin=f"fetch.{lane}" if lane else None, threads=threads,
                                  pdf_raw=raw_route, **child_kw)
                inflight[fut] = _Item(row, snap, attempts, before)
            converted_rows += charged
            if stop is not None:
                break
        await_inflight(0)  # a stop ends submission; items in flight finish and are applied (a drain: bounded)
    if seconds:
        env.count("convert.seconds", round(seconds))
    if wall_from is not None:
        env.count("convert.wall_seconds", round(time.monotonic() - wall_from))
    if stop is not None:
        env.count(f"convert.stopped.{stop}")
    return stop is None


__all__ = ["CAP", "CONFIGS", "GARBLED", "STRUCTURED_TOOLS", "ConvertConfig", "ConvertOutcome", "config_named", "convert_one",
           "convert_pin", "current_candidate", "garbled_retry", "guard_pin", "pin_document", "pin_of",
           "run_convert", "settle",
           "to_convert", "tool_versions"]
