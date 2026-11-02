"""``stack.anchor`` — quote→span anchoring (the ``Anchorer`` service, 20-technical §6).

Input: a model-emitted ``quote`` and the snapshot's stored Markdown ``text``. Output an
:class:`~spec.types.interfaces.Anchor` — ``{span, method, score, occurrences}`` — locating the
quote, or ``None`` on rejection. Offsets are ``[start, end)`` half-open in **Unicode code
points** into the exact text named by the snapshot's ``text.sha256`` (Python ``str`` indexing
*is* the code-point frame, so slicing is direct; §6 / payloads ``Grounding``).

Contract (authoritative): v3's ``spec/20-technical.md`` §6 (the anchoring recipe) and
``spec/types/interfaces.py`` (the ``Anchorer`` protocol + ``Anchor`` dataclass). ``Anchor`` and
the method-literal type are MIRRORED locally (below) so ``stack`` stays import-safe outside repo
root — ``spec/`` is not packaged, and 30-stack-module keeps ``stack`` self-contained (as v3's
``read.py`` did). The interface types are imported only under ``TYPE_CHECKING``, where the
``_conforms`` assertion at the bottom proves this module's API satisfies the ``Anchorer``
protocol field-for-field. Thresholds come from ``configs/policies.yaml`` ``anchoring:`` via
:meth:`Anchorer.from_config`; nothing here hardcodes an acceptance score.

The §6 contract, restated as the code obeys it:

1. **Named tiers in strength order; the first with ≥1 hit names ``method``** (§6.1):
   ``exact`` (substring on raw), ``ws`` (search on an NFC + zero-width-stripped +
   whitespace-collapsed projection, offsets mapped back; a whitespace run between two Han/kana
   characters collapses to nothing), ``fold`` (ws + punctuation folding —
   curly quotes → straight, dashes → ``-``, ligatures expanded, ``…`` → ``...``; **case
   preserved**), ``fuzzy`` (a category-weighted-edit alignment over the folded projection).
   ``exact``/``ws``/``fold`` score ``1.0``.
2. **Weighted score** at ``fuzzy`` (§6.2): whitespace/punctuation edits ~free, case cheap,
   letter edits moderate; normalized to ``[0, 1]``. The candidate-window construction and the
   exact cost constants are implementation-free — the constants below are one valid choice.
3. **The HARD digit-run rule** (§6.3, normative): the ordered sequence of maximal ``[0-9]+``
   runs of the quote must equal that of the matched raw span **exactly**, else reject
   regardless of score ("84%" never anchors to "48%"; "GPT-4" never to "GPT-5"). This is the
   invariant that protects numeric evidence; it is also the stored-span re-verification check.
4. **Accept iff ``score ≥ anchor_min_score``** (policies.yaml; default 0.98, per-workflow
   overridable). Record ``method``, ``score``, ``occurrences``.

Quote length cap: 600 code points (longer ⇒ reject, reason ``quote_too_long``). Multiple hits
at the winning tier ⇒ ``occurrences = n``. The service is pure and deterministic: identical
``(quote, text)`` (at a fixed ``min_score``) always yields identical output, no randomness.

Caveats callers should know:

* **Fold-tier spans can be a superset of the quote.** Folding expands some code points (a single
  ``ﬁ`` ligature → ``fi``, ``…`` → ``...``), so a ``fold`` (or ``fuzzy``) span sliced out of the
  raw text may read as MORE than the emitted quote (it covers the un-expanded source glyph). Do
  not assert ``text[span] == quote`` on non-``exact`` hits; the guarantee is the digit-run rule
  (§6.3), not character equality.
* **Fuzzy recall on very long text is seeding-bounded** — see :func:`_seed_estimates` (top few
  longest tokens; whole-text fallback only under ``_FULLSCAN_MAX_CP``).
* **``anchor_detail``/:class:`AnchorOutcome` are NOT in the ``Anchorer`` protocol.** The protocol
  is ``anchor() -> Anchor | None``; the typed rejection reason is an extra afforded by the
  concrete :class:`Anchorer`, which void-recording callers (§6.1) use directly. (The integrator
  raises adding it to the protocol as a separate spec amendment.)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    # Type-check against the authoritative contract types WITHOUT importing ``spec`` at runtime,
    # so ``stack`` stays import-safe outside repo root (``spec/`` is not packaged; 30-stack-module
    # keeps ``stack`` self-contained, as v3's ``read.py`` did). At type-check time these names alias
    # the interface dataclass/Literal — so ``Anchor`` is verified field-for-field and the
    # ``Anchorer``-protocol conformance assert (bottom of file) is checked against the real
    # contract. At runtime the ``else`` branch below supplies structurally identical mirrors.
    from spec.types.interfaces import Anchor, AnchorMethod
    from spec.types.interfaces import Anchorer as _AnchorerProto
    from stack.config import StackConfig
else:
    AnchorMethod = Literal["exact", "ws", "fold", "fuzzy"]

    @dataclass(frozen=True)
    class Anchor:
        """Runtime mirror of ``spec.types.interfaces.Anchor`` (field-for-field; offsets are
        ``[start, end)`` in Unicode code points). Local so ``stack`` needs no runtime ``spec``
        import; type-checking uses the interface class itself (see the ``TYPE_CHECKING`` block)."""

        span: tuple[int, int]
        method: AnchorMethod
        score: float
        occurrences: int

# ─────────────────────────────────────────────────────────────────────────────
# Constants (§6)
# ─────────────────────────────────────────────────────────────────────────────

MAX_QUOTE_CP: Final = 600
"""Hard quote length cap in code points (§6): longer ⇒ ``quote_too_long``."""

DEFAULT_MIN_SCORE: Final = 0.98
"""Mirror of ``policies.yaml`` ``anchoring.anchor_min_score`` (§6). The live value is read
from config via :meth:`Anchorer.from_config`; this default is only a bare-constructor fallback."""

# Category-weighted edit costs for the fuzzy tier (§6.2, "implementation-free"). Letter/digit
# edits are the threshold driver; case is cheap; whitespace/punctuation are ~free. Digits are
# priced as letters — the hard digit-run rule (§6.3) is the real numeric gate, so a dedicated
# digit cost would be redundant (the spec drops that mandate explicitly).
_LETTER_COST: Final = 1.0
_CASE_COST: Final = 0.10
_PUNCT_COST: Final = 0.05
_WS_COST: Final = 0.05

# Fuzzy candidate-window seeding bounds (implementation-free; kept modest for determinism/speed).
_MAX_SEED_TOKENS: Final = 3
_MAX_SEED_HITS: Final = 32
_MAX_WINDOWS: Final = 24
_FULLSCAN_MAX_CP: Final = 4_000  # whole-text (unbanded) fuzzy fallback only for short texts
_BAND_FLOOR: Final = 8  # minimum diagonal-band radius in cp (slack over the edit budget)
_INF: Final = float("inf")

RejectReason = Literal[
    "quote_too_long",
    "empty_quote",
    "empty_text",
    "below_threshold",
    "digit_run_mismatch",
    "no_match",
]
"""Why an anchor was rejected (surfaced via :meth:`Anchorer.anchor_detail`). ``anchor()`` itself
returns ``None`` per the protocol; the void recipe (§6.1) records the reason in ``flags.voided``."""

# Zero-width code points stripped by the ws/fold projections (matches the content_key recipe §3:
# ZWSP..ZWJ, word joiner, BOM/ZWNBSP).
_ZERO_WIDTH: Final = frozenset("​‌‍⁠﻿")

# ASCII whitespace (the fast-path whitespace test in ``_project``).
_ASCII_WS: Final = frozenset(" \t\n\r\f\v")

# Punctuation fold table (§6.1 N2 example — informative, not normative; a superset is legal).
# Case is NOT folded here (fold tier preserves case; case is evidence). Curly/typographic
# quotes → straight, the dash family → "-", ellipsis → "...", the common ligatures expanded.
_FOLD: Final[dict[str, str]] = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",  # single quotes/prime
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',  # double quotes/prime
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",  # hyphen/dashes
    "―": "-", "−": "-",                                                # bar / minus
    "…": "...",                                                            # ellipsis
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",  # ligatures
}

# §6.3 literally says `[0-9]+`, but that leaves a hole for non-ASCII numerals (Arabic-Indic
# ٩٣, Devanagari ९३, fullwidth ９３): the number would be invisible to the guard and could anchor
# to an altered span. We deliberately STRENGTHEN it to Unicode decimal digits (category Nd, `\d`),
# comparing runs as EXACT CHARACTER sequences — no numeric-value normalization, so ٩٣ and 93 are
# NOT equal (we only close the hole where ٩٣ would have anchored to a span reading ٩٢). ASCII
# behavior is unchanged. (Integrator raises the §6.3 wording clarification separately.)
_DIGIT_RUN: Final = re.compile(r"\d+")


# ─────────────────────────────────────────────────────────────────────────────
# Result envelope
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnchorOutcome:
    """The full anchoring outcome: an :class:`Anchor` on success, else ``anchor=None`` plus a
    typed ``reason``. ``Anchorer.anchor`` returns just ``.anchor`` (the protocol shape); callers
    that record the §6.1 void recipe read ``.reason`` for ``flags.voided``."""

    anchor: Anchor | None
    reason: RejectReason | None


# ─────────────────────────────────────────────────────────────────────────────
# Projection (ws / fold), with a per-char map back to raw code-point offsets
# ─────────────────────────────────────────────────────────────────────────────


#: Scripts written without spaces between words (Han ideographs, kana, CJK and fullwidth punctuation; not Hangul,
#: which spaces its words). A whitespace run between two such characters is dropped by the ws/fold projections:
#: PDF converters insert a space at a line wrap inside a Chinese sentence ("及 时处置" for "及时处置", the TC260
#: miss of the Chinese-coverage review's round 2), and a model quoting the sentence writes none.
_NO_SPACE_SCRIPT: Final = re.compile(
    "[\u2e80-\u2fdf\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\u3100-\u312f\u31a0-\u31ff\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\U00020000-\U0003134f]")


def _no_space_script(ch: str) -> bool:
    return ch > "\u2e7f" and _NO_SPACE_SCRIPT.match(ch) is not None


def _project(text: str, *, fold: bool) -> tuple[str, list[int]]:
    """Build the ws (``fold=False``) or fold (``fold=True``) projection of ``text`` and an index
    map ``out[k] -> raw code-point index`` (§6.1).

    Per raw char, in order: drop zero-width; collapse each whitespace run to a single space, or to
    nothing between two characters of a script written without spaces (:data:`_NO_SPACE_SCRIPT`:
    a converter's line-wrap space inside a Chinese sentence); else NFC-normalize the char and (fold
    tier) apply the punctuation fold table. NFC is applied
    per code point so every projected char keeps a single raw source index — this is a partial
    NFC (it never recomposes a base+combining-mark *sequence*), which is sufficient for the §6
    contract (singleton/compatibility normalization + ligature/quote/dash folding); a
    decomposed-vs-precomposed *sequence* difference degrades to the fuzzy tier instead.

    ASCII fast-path: ASCII code points are NFC-stable and never zero-width, so they skip the
    per-char ``unicodedata.normalize`` call — this keeps whole-document projection linear and
    cheap on the common (mostly-ASCII) case (the fuzzy-latency budget, §6.2).
    """
    out: list[str] = []
    imap: list[int] = []
    pending = -1  # raw index of a whitespace run not yet emitted (-1: none); leading/trailing runs never are
    prev = ""  # the last emitted char (to tell whether a pending run sits between two no-space-script chars)

    def flush(ch: str) -> None:
        nonlocal pending
        if pending >= 0 and out and not (_no_space_script(prev) and _no_space_script(ch)):
            out.append(" ")
            imap.append(pending)
        pending = -1

    for i, ch in enumerate(text):
        if ch <= "\x7f":  # ASCII fast-path: NFC-stable, not zero-width
            if ch in _ASCII_WS:
                if pending < 0:
                    pending = i
                continue
            flush(ch)
            folded = _FOLD.get(ch) if fold else None
            if folded is not None:
                for fc in folded:
                    out.append(fc)
                    imap.append(i)
            else:
                out.append(ch)
                imap.append(i)
            prev = out[-1]
            continue
        if ch in _ZERO_WIDTH:
            continue
        if ch.isspace():
            if pending < 0:
                pending = i
            continue
        flush(ch)
        for c in unicodedata.normalize("NFC", ch):
            folded = _FOLD.get(c) if fold else None
            if folded is not None:
                for fc in folded:
                    out.append(fc)
                    imap.append(i)
            else:
                out.append(c)
                imap.append(i)
        prev = ch
    return "".join(out), imap


def _count_occurrences(hay: str, needle: str) -> int:
    """Count distinct start positions of ``needle`` in ``hay`` (overlapping-aware)."""
    if not needle:
        return 0
    n = 0
    start = 0
    while True:
        at = hay.find(needle, start)
        if at < 0:
            return n
        n += 1
        start = at + 1


def _digit_runs(s: str) -> list[str]:
    """The ordered sequence of maximal Unicode-decimal-digit runs (§6.3; see ``_DIGIT_RUN``).

    Format/zero-width characters (Unicode category ``Cf`` — ZWSP/ZWNJ/ZWJ/word-joiner/BOM, the
    same family ``content_key`` normalization strips) are removed FIRST, so an invisible char
    inside a number (``1``+ZWSP+``2``) does not split the run and misreport ``digit_run_mismatch``.
    Real whitespace inside a number still splits it (conservative — a genuine ``1 2`` ≠ ``12``)."""
    if any(unicodedata.category(c) == "Cf" for c in s):
        s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    return _DIGIT_RUN.findall(s)


def _locate(
    proj_text: str, imap: list[int], proj_quote: str
) -> tuple[tuple[int, int], int] | None:
    """A projected exact-substring tier (ws or fold): find ``proj_quote`` in ``proj_text`` and map
    the hit back to a raw ``[start, end)`` span, returning ``(span, occurrences)`` or ``None``."""
    if not proj_quote:
        return None
    i = proj_text.find(proj_quote)
    if i < 0:
        return None
    span = (imap[i], imap[i + len(proj_quote) - 1] + 1)
    return span, _count_occurrences(proj_text, proj_quote)


# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy tier — category-weighted approximate-substring alignment (§6.2)
# ─────────────────────────────────────────────────────────────────────────────


# Per-char feature arrays let the hot DP loop avoid per-cell method calls (the §6.2
# fuzzy-latency budget). Class codes: 0 = whitespace, 1 = alphanumeric, 2 = punctuation.
_Feats = tuple[list[int], list[str], list[bool], list[float]]


def _features(s: str) -> _Feats:
    """Per-char ``(class, casefold, is_alpha, indel_cost)`` arrays for ``s`` (fuzzy DP)."""
    cls: list[int] = []
    cf: list[str] = []
    alpha: list[bool] = []
    indel: list[float] = []
    for ch in s:
        a = ch.isalpha()
        alpha.append(a)
        cf.append(ch.casefold() if a else ch)
        if ch.isspace():
            cls.append(0)
            indel.append(_WS_COST)
        elif ch.isalnum():
            cls.append(1)
            indel.append(_LETTER_COST)
        else:
            cls.append(2)
            indel.append(_PUNCT_COST)
    return cls, cf, alpha, indel


def _approx_substring(
    pattern: str, window: str, pf: _Feats, wf: _Feats, diag: int, band: int, budget: float
) -> tuple[float, int, int] | None:
    """Best category-weighted alignment of the whole ``pattern`` against a *substring* of
    ``window`` (free start/end), restricted to a diagonal band and pruned by a cost budget.

    Free-start/free-end weighted-edit DP (``dp[0][j]=0`` in-band ⇒ begin anywhere; answer at
    ``min_j dp[n][j]``; ``start`` tracks the alignment's origin). Two speed levers (§6.2 fix 2):

    * **Diagonal band** — ``pattern[0]`` is expected at window column ``diag`` (seed geometry), so
      only cells with ``|j - (i + diag)| <= band`` are computed: O(n·band) not O(n·m). ``band`` is
      sized from the edit budget; a genuinely near-verbatim match (the only kind that can clear the
      threshold) stays on the diagonal. (Pass ``band >= m`` to disable — the unbanded reference.)
    * **Cost early-out** — a whole row's minimum cost is non-decreasing across rows, so once it
      exceeds ``budget`` no ≤budget completion can exist and the window bails immediately (a huge
      win on long text, where most seed windows are false positives). (Pass ``budget = inf`` off.)

    Returns ``(cost, local_start, local_end)`` in window coordinates, or ``None``."""
    n, m = len(pattern), len(window)
    if n == 0 or m == 0:
        return None
    p_cls, p_cf, p_alpha, p_indel = pf
    w_cls, w_cf, w_alpha, w_indel = wf
    prev_cost = [_INF] * (m + 1)
    prev_start = [0] * (m + 1)
    lo, hi = max(0, diag - band), min(m, diag + band)
    for j in range(lo, hi + 1):  # row 0: free start anywhere in the band
        prev_cost[j] = 0.0
        prev_start[j] = j
    for i in range(1, n + 1):
        del_p = p_indel[i - 1]
        pc = pattern[i - 1]
        pcls = p_cls[i - 1]
        pcf = p_cf[i - 1]
        pal = p_alpha[i - 1]
        lo, hi = max(0, i + diag - band), min(m, i + diag + band)
        cur_cost = [_INF] * (m + 1)
        cur_start = [0] * (m + 1)
        row_min = _INF
        j = lo
        if j == 0:  # pattern char deleted, no text consumed
            c0 = prev_cost[0] + del_p
            cur_cost[0] = c0
            cur_start[0] = prev_start[0]
            row_min = c0
            j = 1
        while j <= hi:
            wc = window[j - 1]
            if pc == wc:
                sc = 0.0
            elif pal and w_alpha[j - 1] and pcf == w_cf[j - 1]:
                sc = _CASE_COST  # case-only difference — cheap
            elif pcls != 1 and w_cls[j - 1] != 1:  # both whitespace/punctuation — ~free
                sc = _PUNCT_COST if (pcls == 2 or w_cls[j - 1] == 2) else _WS_COST
            else:
                sc = _LETTER_COST
            best = prev_cost[j - 1] + sc  # substitute/match
            s = prev_start[j - 1]
            dc = prev_cost[j] + del_p  # delete pattern char
            if dc < best:
                best = dc
                s = prev_start[j]
            ins = cur_cost[j - 1] + w_indel[j - 1]  # insert (extra) text char
            if ins < best:
                best = ins
                s = cur_start[j - 1]
            cur_cost[j] = best
            cur_start[j] = s
            if best < row_min:
                row_min = best
            j += 1
        if row_min > budget:
            return None  # early-out — no ≤budget alignment can complete from here
        prev_cost, prev_start = cur_cost, cur_start
    lo, hi = max(0, n + diag - band), min(m, n + diag + band)
    best_c = _INF
    best_j = -1
    for j in range(lo, hi + 1):
        if prev_cost[j] < best_c:
            best_c = prev_cost[j]
            best_j = j
    if best_j < 0 or best_c == _INF:
        return None
    start, end = prev_start[best_j], best_j
    if end <= start:
        return None
    return best_c, start, end


def _seed_estimates(fquote: str, ftext: str) -> list[int]:
    """Estimated match-start offsets in ``ftext`` for the fuzzy tier: for each distinctive quote
    token found in ``ftext``, ``hit_pos - token_offset_in_quote`` places the quote's start on the
    diagonal so the DP band can be tight. Deterministic; deduped, sorted, capped.

    Recall limitation (documented): only the top ``_MAX_SEED_TOKENS`` longest tokens, each to
    ``_MAX_SEED_HITS`` hits, seed the search — a quote whose only intact tokens are shorter than
    those, on text above ``_FULLSCAN_MAX_CP`` (so the whole-text fallback is off), may not be
    found. Acceptable at the strict threshold (a near-verbatim quote keeps long intact tokens)."""
    tokens = sorted({t for t in fquote.split(" ") if len(t) >= 2}, key=lambda t: (-len(t), t))
    ests: list[int] = []
    for seed in tokens[:_MAX_SEED_TOKENS]:
        off = fquote.find(seed)
        start = 0
        hits = 0
        while hits < _MAX_SEED_HITS:
            at = ftext.find(seed, start)
            if at < 0:
                break
            ests.append(at - off)
            start = at + 1
            hits += 1
    return sorted(set(ests))[:_MAX_WINDOWS]


# ─────────────────────────────────────────────────────────────────────────────
# The service
# ─────────────────────────────────────────────────────────────────────────────


class Anchorer:
    """Weighted-cost quote anchorer (20-technical §6 / ``interfaces.Anchorer``).

    Construct with an explicit ``min_score`` (the §6 acceptance threshold) or, preferably, via
    :meth:`from_config` which reads ``policies.yaml`` ``anchoring:`` and applies any per-workflow
    override. A single instance carries one threshold, so :meth:`anchor` matches the protocol's
    ``(quote, text)`` signature (the workflow's threshold is baked in at construction)."""

    def __init__(self, min_score: float = DEFAULT_MIN_SCORE) -> None:
        self.min_score = min_score
        # Fuzzy DP banding + early-out (the §6.2 latency levers). Only turned off by the
        # equivalence test, which asserts the banded result matches a full unbanded DP.
        self._banded = True

    @classmethod
    def from_config(cls, config: StackConfig, workflow: str | None = None) -> Anchorer:
        """Build an anchorer whose threshold is ``policies.yaml`` ``anchoring.anchor_min_score``,
        overridden by ``anchoring.per_workflow[workflow]`` when ``workflow`` is registered there
        (§6; the live ``policies.yaml`` registers none)."""
        anchoring = config.policy_file.anchoring
        score = anchoring.anchor_min_score
        if workflow is not None:
            score = anchoring.per_workflow.get(workflow, score)
        return cls(min_score=score)

    # -- protocol surface ----------------------------------------------------

    def anchor(self, quote: str, text: str) -> Anchor | None:
        """Locate ``quote`` in ``text`` by the exact→ws→fold→fuzzy tiers with the hard digit-run
        rule; return the :class:`Anchor` or ``None`` on rejection (§6). See :meth:`anchor_detail`
        for the typed rejection reason."""
        return self.anchor_detail(quote, text).anchor

    def anchor_detail(self, quote: str, text: str) -> AnchorOutcome:
        """As :meth:`anchor`, but returns the full :class:`AnchorOutcome` (anchor + typed
        reason) so a caller can record the §6.1 void recipe (``flags.voided``)."""
        # Degenerate / cap guards (§6). "Blank" = no anchorable char (only whitespace and/or
        # zero-width code points); note str.strip() does NOT remove zero-width, so check both.
        if len(quote) > MAX_QUOTE_CP:
            return AnchorOutcome(None, "quote_too_long")
        if not text:
            return AnchorOutcome(None, "empty_text")
        if not any((not c.isspace()) and (c not in _ZERO_WIDTH) for c in quote):
            return AnchorOutcome(None, "empty_quote")

        # Tier: exact (substring on raw).
        at = text.find(quote)
        if at >= 0:
            return self._finish(text, (at, at + len(quote)), "exact", 1.0,
                                _count_occurrences(text, quote), quote)

        # Tier: ws (NFC + zero-width strip + whitespace collapse), then fold (+ punctuation fold).
        # The fold projection is built once here and reused by the fuzzy tier (one pass, not two).
        ws_text, ws_map = _project(text, fold=False)
        ws_quote, _ = _project(quote, fold=False)
        ws_hit = _locate(ws_text, ws_map, ws_quote)
        if ws_hit is not None:
            return self._finish(text, ws_hit[0], "ws", 1.0, ws_hit[1], quote)
        fold_text, fold_map = _project(text, fold=True)
        fold_quote, _ = _project(quote, fold=True)
        fold_hit = _locate(fold_text, fold_map, fold_quote)
        if fold_hit is not None:
            return self._finish(text, fold_hit[0], "fold", 1.0, fold_hit[1], quote)

        # Tier: fuzzy (category-weighted alignment over the folded projection, reused).
        return self._fuzzy(quote, text, fold_text, fold_map, fold_quote)

    def _finish(
        self,
        text: str,
        span: tuple[int, int],
        method: AnchorMethod,
        score: float,
        occurrences: int,
        quote: str,
    ) -> AnchorOutcome:
        """Apply the hard digit-run rule (§6.3, also the stored-span re-verification) to the raw
        span, then emit the anchor. For exact/ws/fold the digits are preserved by construction,
        so this is a safety net; for fuzzy it is the numeric gate (applied there directly)."""
        if _digit_runs(quote) != _digit_runs(text[span[0]:span[1]]):
            return AnchorOutcome(None, "digit_run_mismatch")
        return AnchorOutcome(Anchor(span, method, round(score, 4), occurrences), None)

    def _fuzzy(
        self, quote: str, text: str, ftext: str, fmap: list[int], fquote: str
    ) -> AnchorOutcome:
        """The fuzzy tier: a banded, budget-pruned weighted-edit alignment over the (reused) fold
        projection, restricted to seed-anchored windows (§6.2). ``ftext``/``fmap``/``fquote`` are
        the fold projection computed once by the caller."""
        if not fquote or not ftext:
            return AnchorOutcome(None, "no_match")
        q_digits = _digit_runs(quote)
        pf = _features(fquote)
        denom = max(1.0, sum(pf[3]))  # sum of pattern indel costs (the score normalizer)
        qlen = len(fquote)
        # Edit budget an accepted match may spend, and the diagonal band it can wander within:
        # score >= min_score ⟺ cost <= (1-min_score)*denom. `band` covers seed-offset error +
        # near-verbatim drift (~1 cp per edit-unit); the equivalence test guards this heuristic.
        budget = (1.0 - self.min_score) * denom
        band = 2 * (int(budget) + _BAND_FLOOR)
        dp_budget = budget if self._banded else _INF

        # (score, raw_span, digit_ok) for the best alignment in each candidate window.
        cands: list[tuple[float, tuple[int, int], bool]] = []
        seen: set[tuple[int, int]] = set()

        def _consider(w0: int, window: str, diag: int, dp_band: int) -> None:
            res = _approx_substring(fquote, window, pf, _features(window), diag, dp_band, dp_budget)
            if res is None:
                return
            cost, ls, le = res
            f0, f1 = w0 + ls, w0 + le
            if f1 <= f0:
                return
            raw_span = (fmap[f0], fmap[f1 - 1] + 1)
            if raw_span in seen:
                return
            seen.add(raw_span)
            score = max(0.0, 1.0 - cost / denom)
            matched = text[raw_span[0]:raw_span[1]]
            cands.append((score, raw_span, _digit_runs(matched) == q_digits))

        ests = _seed_estimates(fquote, ftext)
        if ests:
            for est in ests:
                w0 = max(0, est - band)
                w1 = min(len(ftext), est + qlen + band)
                win = ftext[w0:w1]
                # Banded around the seed diagonal; the equivalence mode widens the band to the
                # whole window (a full unbanded DP) while keeping the same window + windows.
                _consider(w0, win, est - w0, band if self._banded else len(win))
        elif len(ftext) <= _FULLSCAN_MAX_CP:
            # No distinctive token matched (rare for a near-verbatim quote): scan the whole short
            # text UNBANDED (no diagonal to anchor on), so recall does not depend on seeding.
            _consider(0, ftext, 0, len(ftext))

        # Keep candidates that clear the threshold AND satisfy the hard digit-run rule (§6.3-4).
        valid = [(s, sp) for (s, sp, ok) in cands if s >= self.min_score and ok]
        if valid:
            clusters = _merge_spans(valid)  # de-overlap so occurrences counts distinct regions
            best_score, best_span = max(clusters, key=lambda c: (c[0], -c[1][0]))
            return AnchorOutcome(
                Anchor(best_span, "fuzzy", round(best_score, 4), len(clusters)), None
            )
        # No acceptable hit — classify why (digit rule is reported even though it "wins" over
        # score). With banding+early-out on, a purely below-threshold candidate is pruned before
        # scoring, so such a case surfaces as ``no_match`` rather than ``below_threshold``.
        if any(s >= self.min_score and not ok for (s, _sp, ok) in cands):
            return AnchorOutcome(None, "digit_run_mismatch")
        if cands:
            return AnchorOutcome(None, "below_threshold")
        return AnchorOutcome(None, "no_match")


def _merge_spans(hits: list[tuple[float, tuple[int, int]]]) -> list[tuple[float, tuple[int, int]]]:
    """Merge overlapping ``(score, span)`` hits into distinct regions (occurrence clusters),
    keeping each region's best-scoring representative. Deterministic (sorted by start)."""
    # Each cluster: (best_score, best_span, lo, hi) — hi is the merged right edge.
    clusters: list[tuple[float, tuple[int, int], int, int]] = []
    for score, span in sorted(hits, key=lambda h: (h[1][0], h[1][1])):
        if clusters:
            b_score, b_span, lo, hi = clusters[-1]
            if span[0] < hi:  # overlaps the current cluster → merge, keep the better rep
                new_hi = max(hi, span[1])
                if score > b_score:
                    clusters[-1] = (score, span, lo, new_hi)
                else:
                    clusters[-1] = (b_score, b_span, lo, new_hi)
                continue
        clusters.append((score, span, span[0], span[1]))
    return [(c[0], c[1]) for c in clusters]


# ─────────────────────────────────────────────────────────────────────────────
# Protocol conformance (mypy-checked): stack.anchor.Anchorer satisfies interfaces.Anchorer,
# and anchor() returns the interface's Anchor dataclass (reused above, not re-declared).
# ─────────────────────────────────────────────────────────────────────────────
if TYPE_CHECKING:
    _conforms: _AnchorerProto = Anchorer(min_score=DEFAULT_MIN_SCORE)
