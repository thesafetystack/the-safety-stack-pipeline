"""Tests for ``stack.anchor`` — the quote→span Anchorer (20-technical §6).

Each test pins one clause of the §6 anchoring contract (cited inline). Coverage: the tier
ladder (exact→ws→fold→fuzzy) and the ``method`` each names; code-point (not byte/UTF-16)
offsets, verified on non-BMP text; the hard digit-run rule (§6.3, the numeric-evidence
invariant); the strict ``anchor_min_score`` threshold and its per-workflow override read from
``policies.yaml``; the 600-cp quote cap; occurrence counting; degenerate inputs; and
determinism. Property-style sweeps sit alongside the golden cases.

These tests are DB-free (only the ``config`` fixture from conftest, which just loads configs/)."""

from __future__ import annotations

import dataclasses

import pytest

from spec.types.interfaces import Anchorer as AnchorerProto
from stack.anchor import DEFAULT_MIN_SCORE, MAX_QUOTE_CP, Anchor, Anchorer, _digit_runs

A = Anchorer()  # default threshold (0.98)


# ─────────────────────────────────────────────────────────────────────────────
# §6 contract conformance — the API is an interfaces.Anchorer returning an Anchor
# ─────────────────────────────────────────────────────────────────────────────


def test_default_threshold_is_the_spec_default() -> None:
    assert DEFAULT_MIN_SCORE == 0.98
    assert Anchorer().min_score == 0.98


def test_satisfies_anchorer_protocol_at_runtime() -> None:
    # interfaces.Anchorer is @runtime_checkable; the concrete service must satisfy it (§6/§9.2).
    assert isinstance(A, AnchorerProto)


def test_anchor_returns_the_anchor_contract_shape() -> None:
    # anchor() returns stack.anchor.Anchor, the field-for-field runtime mirror of the interface
    # dataclass (offsets code-point [start, end), method/score/occurrences per §6).
    a = A.anchor("improved by 10", "the model improved by 10 points")
    assert isinstance(a, Anchor)
    assert isinstance(a.span, tuple) and len(a.span) == 2
    assert a.method in ("exact", "ws", "fold", "fuzzy")
    assert isinstance(a.score, float) and isinstance(a.occurrences, int)


# ─────────────────────────────────────────────────────────────────────────────
# Tier ladder (§6.1) — exact → ws → fold → fuzzy; first hit names `method`, score 1
# ─────────────────────────────────────────────────────────────────────────────


def test_exact_tier() -> None:
    text = "the model improved by 10 points"
    a = A.anchor("improved by 10", text)
    assert a is not None
    assert a.method == "exact"
    assert a.score == 1.0
    assert a.span == (10, 24)
    assert text[a.span[0]:a.span[1]] == "improved by 10"


def test_ws_tier_nbsp() -> None:
    # A non-breaking space in the text vs an ordinary space in the quote → ws tier (§6.1).
    text = "say hello world now"
    a = A.anchor("hello world", text)
    assert a is not None
    assert a.method == "ws"
    assert a.score == 1.0
    assert text[a.span[0]:a.span[1]] == "hello world"


def test_ws_tier_newline_and_run_collapse() -> None:
    # Newlines + a multi-space run collapse to single spaces in the ws projection (§6.1).
    text = "say hello\n\n   world now"
    a = A.anchor("hello world", text)
    assert a is not None
    assert a.method == "ws"
    assert text[a.span[0]:a.span[1]] == "hello\n\n   world"


def test_ws_tier_zero_width_stripped() -> None:
    # A zero-width space inside the text is stripped by the projection (§3 zero-width set).
    text = "the al​ignment work"
    a = A.anchor("alignment", text)
    assert a is not None
    assert a.method == "ws"
    assert text[a.span[0]:a.span[1]] == "al​ignment"


def test_fold_tier_curly_quotes() -> None:
    text = "he said “quick” loudly"
    a = A.anchor('"quick"', text)
    assert a is not None
    assert a.method == "fold"
    assert text[a.span[0]:a.span[1]] == "“quick”"


def test_fold_tier_dashes_and_ellipsis() -> None:
    text = "the range 3–5 and so on… done"
    a = A.anchor("3-5 and so on...", text)
    assert a is not None
    assert a.method == "fold"


def test_fold_tier_ligature_expanded() -> None:
    # A single ligature code point (ﬃ) expands to "ffi" in the fold projection; the span still
    # covers exactly that one raw code point (§6.1). Also confirms offsets are code points.
    text = "very eﬃcient code"
    a = A.anchor("efficient", text)
    assert a is not None
    assert a.method == "fold"
    assert text[a.span[0]:a.span[1]] == "eﬃcient"


def test_fold_preserves_case_not_a_ws_hit() -> None:
    # Case is evidence: the fold tier does NOT casefold (§6.1 "case preserved"), so a case-only
    # difference is not a fold hit — it degrades to fuzzy (cheap, so still accepted here).
    text = "the Alignment Problem"
    a = A.anchor("alignment problem", text)
    assert a is not None
    assert a.method == "fuzzy"  # not ws/fold — those are case-sensitive


# ─────────────────────────────────────────────────────────────────────────────
# Code-point offsets on non-BMP text (§6 — Unicode code points, not bytes/UTF-16)
# ─────────────────────────────────────────────────────────────────────────────


def test_offsets_are_code_points_emoji() -> None:
    # Two astral-plane emoji (1 code point each in Python) precede the match; a byte- or
    # UTF-16-based offset would be larger. The span must be code-point 3.
    text = "\U0001f600\U0001f600 improved by 10 today"
    a = A.anchor("improved by 10", text)
    assert a is not None
    assert a.span[0] == 3
    assert text[a.span[0]:a.span[1]] == "improved by 10"


def test_offsets_are_code_points_cjk() -> None:
    # CJK chars are 3 bytes each in UTF-8 but 1 code point; span[0] must be 3, not 7.
    text = "你好 improved model"
    a = A.anchor("improved", text)
    assert a is not None
    assert a.span[0] == 3
    assert text[a.span[0]:a.span[1]] == "improved"


def test_ws_tier_drops_converter_spaces_inside_chinese() -> None:
    # The Chinese-coverage review, round 2: pymupdf put a line-wrap space inside "及时处置" (TC260-003); the model quoted it without.
    text = "对生成内容应当及 时处置，并保存记录。"
    a = A.anchor("应当及时处置", text)
    assert a is not None and a.method == "ws" and a.score == 1.0
    assert text[a.span[0]:a.span[1]] == "应当及 时处置"
    b = A.anchor("应当及 时处置", "对生成内容应当及时处置。")  # and the other way round
    assert b is not None and b.method == "ws"
    # kana too; a space between a Latin and a Han character, and Korean word spaces, still count
    kana = A.anchor("安全性の評価", "モデルの安全性の 評価を行う")
    assert kana is not None and kana.method == "ws"
    for quote, hay in (("AI安全", "大型 AI 安全"), ("인공지능 안전", "인공지능안전")):
        got = A.anchor(quote, hay)
        assert got is None or got.method not in ("exact", "ws")


def test_fuzzy_span_slices_back_on_non_bmp_text() -> None:
    # A genuine one-letter typo (lazy→lasy) forces the FUZZY tier, and the astral emoji before the
    # match forces the fold-projection index map (fmap) to translate a banded-DP span across a
    # non-BMP code point. span[0] must be 2 (emoji=1 cp, space=1 cp), and the slice must recover
    # the TEXT (not the misspelled quote).
    matched = "the quick brown fox jumped over the lazy dog and ran away quickly"
    text = "\U0001f9e0 " + matched
    a = A.anchor(matched.replace("lazy", "lasy"), text)
    assert a is not None
    assert a.method == "fuzzy"
    assert a.span[0] == 2
    assert text[a.span[0]:a.span[1]] == matched


# ─────────────────────────────────────────────────────────────────────────────
# Hard digit-run rule (§6.3) — protects numeric evidence, REGARDLESS of score
# ─────────────────────────────────────────────────────────────────────────────


def test_digit_run_rule_spec_literal_examples() -> None:
    # The spec's own examples: "84%" never anchors to "48%"; "GPT-4" never to "GPT-5" (§6.3).
    assert A.anchor("84%", "the score was 48% overall") is None
    assert A.anchor("GPT-4", "we evaluated GPT-5 on this") is None


def test_digit_run_violation_rejected_even_above_threshold() -> None:
    # THE invariant: a fuzzy alignment whose similarity clears the threshold is still rejected
    # when it alters a digit run (§6.3). One digit differs (23→21) inside a long quote.
    text = "the alignment tax measurement improved substantially to 23 percent overall in tests"
    dquote = text.replace(" 23 ", " 21 ")
    out = A.anchor_detail(dquote, text)
    assert out.anchor is None
    assert out.reason == "digit_run_mismatch"

    # Control: the SAME string with one LETTER changed instead of the digit anchors as fuzzy
    # with a high score — proving the digit case above was rejected on the digit rule, not the
    # threshold (identical edit magnitude, identical would-be score).
    lquote = text.replace("overall", "overoll")
    ctrl = A.anchor(lquote, text)
    assert ctrl is not None
    assert ctrl.method == "fuzzy"
    assert ctrl.score >= 0.98


def test_digit_run_rule_rejects_dropped_number() -> None:
    # Dropping a number entirely also changes the digit-run sequence → reject (§6.3).
    text = "accuracy reached 92 on the held-out evaluation set after tuning steps"
    quote = text.replace("92 ", "")  # quote omits the number
    assert A.anchor(quote, text) is None


def test_accepted_anchor_digit_runs_match_slice() -> None:
    # Stored-span re-verification (§6.3): the accepted span's digit runs equal the quote's.
    text = "the model improved by 10.5 points and 3 more"
    a = A.anchor("improved by 10.5 points and 3", text)
    assert a is not None
    assert _digit_runs("improved by 10.5 points and 3") == _digit_runs(text[a.span[0]:a.span[1]])


@pytest.mark.parametrize(
    "orig,alt,label",
    [
        ("٩٣", "٩٢", "arabic-indic"),
        ("९३", "९२", "devanagari"),
        ("９３", "９２", "fullwidth"),
    ],
)
def test_digit_run_rule_covers_unicode_numerals(orig: str, alt: str, label: str) -> None:
    # Non-ASCII decimal digits (category Nd) are guarded too (§6.3 strengthened from `[0-9]+` to
    # `\d+`): a near-verbatim quote whose numeral is altered (٩٣→٩٢, ९३→९२, ９３→９２) must NOT
    # anchor, even though its similarity clears the threshold. Runs compare as exact character
    # sequences (no numeric-value normalization).
    text = f"the controlled adversarial evaluation measured accuracy improving to {orig} overall"
    assert A.anchor(text, text) is not None  # the numeral itself anchors fine (exact)
    out = A.anchor_detail(text.replace(orig, alt), text)
    assert out.anchor is None, label
    assert out.reason == "digit_run_mismatch", label


def test_zero_width_inside_number_not_digit_mismatch() -> None:
    # Fix 3: a zero-width char inside a number must not split its digit run and misfire the digit
    # rule. Text has "1<ZWSP>2"; the quote "12" anchors at the ws tier with matching digit runs.
    text = "the reported value 1​2 percent overall"  # ZWSP between 1 and 2
    a = A.anchor("value 12 percent", text)
    assert a is not None
    assert a.method == "ws"
    assert _digit_runs("12") == _digit_runs(text[a.span[0]:a.span[1]])


# ─────────────────────────────────────────────────────────────────────────────
# Strict threshold (§6.4) — score >= anchor_min_score, with per-workflow override
# ─────────────────────────────────────────────────────────────────────────────


def test_fuzzy_near_miss_above_threshold_accepts() -> None:
    text = "the quick brown fox jumped over the lazy dog and ran away quickly"
    quote = text.replace("lazy", "lasy")  # one letter off in a long quote → score >= 0.98
    a = A.anchor(quote, text)
    assert a is not None
    assert a.method == "fuzzy"
    assert a.score >= 0.98


def test_fuzzy_near_miss_below_threshold_rejects() -> None:
    text = "the quick brown fox jumped"
    quote = text.replace("brown", "brawn")  # one letter off in a short quote → below 0.98
    out = A.anchor_detail(quote, text)
    assert out.anchor is None
    # The fuzzy budget early-out (§6.2) prunes a purely below-threshold window before it is
    # scored, so this surfaces as no_match; the contract is only that it is rejected.
    assert out.reason in ("below_threshold", "no_match")


def test_per_workflow_threshold_flips_the_decision() -> None:
    # The SAME near-miss that fails at 0.98 passes at 0.95 (a looser per-workflow bar, §6.4).
    text = "the quick brown fox jumped"
    quote = text.replace("brown", "brawn")
    loose = Anchorer(min_score=0.95)
    a = loose.anchor(quote, text)
    assert a is not None
    assert a.method == "fuzzy"
    assert 0.95 <= a.score < 0.98


def test_from_config_reads_threshold_and_override(config: object) -> None:
    # Thresholds come from policies.yaml, not hardcoded (§6.4): the default and each registered
    # per_workflow override. The live file registers none (policies 4.0.0), so an override is
    # set on a copy of the loaded config to show it is applied.
    anchoring = config.policy_file.anchoring  # type: ignore[attr-defined]
    assert Anchorer.from_config(config).min_score == anchoring.anchor_min_score  # type: ignore[arg-type]
    for wf, score in anchoring.per_workflow.items():  # an override that differs, so it shows
        assert score != anchoring.anchor_min_score, wf
        assert Anchorer.from_config(config, wf).min_score == score  # type: ignore[arg-type]
    looser = anchoring.anchor_min_score - 0.03
    policies = config.policies_cfg.model_copy(  # type: ignore[attr-defined]
        update={"anchoring": anchoring.model_copy(update={"per_workflow": {"annotate.screen": looser}})})
    with_override = dataclasses.replace(config, policies_cfg=policies)  # type: ignore[type-var]
    assert Anchorer.from_config(with_override, "annotate.screen").min_score == looser
    # An unregistered workflow falls back to the default.
    assert "annotate.claim" not in anchoring.per_workflow
    assert Anchorer.from_config(config, "annotate.claim").min_score == anchoring.anchor_min_score  # type: ignore[arg-type]
    assert Anchorer.from_config(with_override, "annotate.claim").min_score == anchoring.anchor_min_score


# ─────────────────────────────────────────────────────────────────────────────
# Quote length cap (§6) — 600 code points
# ─────────────────────────────────────────────────────────────────────────────


def test_quote_cap_601_rejected() -> None:
    quote = "a" * (MAX_QUOTE_CP + 1)
    text = "x" + quote + "x"
    out = A.anchor_detail(quote, text)
    assert out.anchor is None
    assert out.reason == "quote_too_long"


def test_quote_cap_600_allowed() -> None:
    quote = "a" * MAX_QUOTE_CP
    text = "x" + quote + "x"
    a = A.anchor(quote, text)
    assert a is not None
    assert a.method == "exact"


def test_quote_cap_counts_code_points_not_bytes() -> None:
    # 600 astral code points = 2400 UTF-8 bytes but is exactly at the cap → allowed.
    quote = "\U0001f600" * MAX_QUOTE_CP
    a = A.anchor(quote, "z" + quote + "z")
    assert a is not None


# ─────────────────────────────────────────────────────────────────────────────
# Occurrences (§6) — count all matches at the winning tier, first (lowest offset) chosen
# ─────────────────────────────────────────────────────────────────────────────


def test_occurrences_counted_and_first_chosen_exact() -> None:
    a = A.anchor("cat", "cat cat cat")
    assert a is not None
    assert a.occurrences == 3
    assert a.span == (0, 3)  # first (lowest offset)


def test_occurrences_at_winning_tier_only() -> None:
    # One exact hit plus a whitespace-variant elsewhere: the winning tier is exact, so its count
    # (1) is what is reported — the ws variant is not counted (§6, "at the winning tier").
    text = "alpha beta and alpha beta again"
    a = A.anchor("alpha beta", text)
    assert a is not None
    assert a.method == "exact"
    assert a.occurrences == 1
    assert a.span == (0, 10)


def test_occurrences_ws_tier() -> None:
    text = "alpha beta then alpha beta"  # two ws-variant hits, no exact hit
    a = A.anchor("alpha beta", text)
    assert a is not None
    assert a.method == "ws"
    assert a.occurrences == 2


# ─────────────────────────────────────────────────────────────────────────────
# Degenerate / empty inputs
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("quote", ["", "   ", "\n\t ", "​"])
def test_empty_or_whitespace_quote_rejected(quote: str) -> None:
    out = A.anchor_detail(quote, "some real text here")
    assert out.anchor is None
    assert out.reason == "empty_quote"


def test_empty_text_rejected() -> None:
    out = A.anchor_detail("anything", "")
    assert out.anchor is None
    assert out.reason == "empty_text"


def test_absent_quote_returns_none() -> None:
    assert A.anchor("a phrase that simply is not present", "totally unrelated content") is None


# ─────────────────────────────────────────────────────────────────────────────
# Determinism (§6 pin) — same inputs → same output, no randomness
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "quote,text",
    [
        ("improved by 10", "the model improved by 10 points"),
        ("hello world", "say hello world now"),
        ("the quick brown fox", "\U0001f9e0 the quick brown fox and more"),
        (
            "the quick brown fox jumped over the lazy dog and ran away quickly".replace("lazy", "lasy"),
            "the quick brown fox jumped over the lazy dog and ran away quickly",
        ),
    ],
)
def test_deterministic(quote: str, text: str) -> None:
    first = A.anchor_detail(quote, text)
    for _ in range(3):
        assert A.anchor_detail(quote, text) == first


# ─────────────────────────────────────────────────────────────────────────────
# Property-style sweeps
# ─────────────────────────────────────────────────────────────────────────────


_SWEEP_TEXTS = [
    "The alignment tax measured 12.5% across 3 runs on GPT-4 and Claude models.",
    "\U0001f600 emoji 你好 CJK and a café with résumé words here.",
    "numbers 007 and 42 and 100 appear, with punctuation: commas, and — dashes — too.",
]


def test_property_exact_slices_roundtrip() -> None:
    # For any substring of a text, the exact tier recovers a span that slices back to it, with
    # method 'exact', score 1.0, and digit runs preserved (deterministic enumeration, §6).
    for text in _SWEEP_TEXTS:
        n = len(text)
        for start in range(0, n, 3):
            for length in (1, 4, 9, 17):
                end = min(n, start + length)
                quote = text[start:end]
                if not quote.strip() or len(quote) > MAX_QUOTE_CP:
                    continue
                a = A.anchor(quote, text)
                assert a is not None, (start, end, repr(quote))
                assert a.method == "exact"
                assert a.score == 1.0
                assert text[a.span[0]:a.span[1]] == quote
                # The chosen span is the first occurrence.
                assert a.span[0] == text.find(quote)
                # Stored-span re-verification invariant (§6.3).
                assert _digit_runs(quote) == _digit_runs(text[a.span[0]:a.span[1]])


def test_property_digit_flip_always_rejected() -> None:
    # Flipping any single digit of an otherwise-verbatim quote must never anchor (§6.3),
    # whatever the resulting similarity — swept over each digit position.
    for text in _SWEEP_TEXTS:
        for i, ch in enumerate(text):
            if not ch.isdigit():
                continue
            other = "8" if ch != "8" else "3"
            quote = text[:i] + other + text[i + 1:]
            # Keep the quote within the cap and non-trivial.
            if len(quote) > MAX_QUOTE_CP:
                continue
            assert A.anchor(quote, text) is None, (i, ch)


def test_property_never_raises_and_offsets_in_range() -> None:
    # The service never raises and any returned span is a valid code-point range (§6).
    weird = ["", "   ", "​‌", "a", "abc " * 500, "\U0001f600" * 700, "GPT-4 vs GPT-5"]
    for q in weird:
        for t in _SWEEP_TEXTS + ["", "\U0001f600"]:
            out = A.anchor_detail(q, t)
            if out.anchor is not None:
                s, e = out.anchor.span
                assert 0 <= s <= e <= len(t)
                assert out.anchor.occurrences >= 1
                assert 0.0 <= out.anchor.score <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy latency (§6.2 fix: banded DP + tight windows + cost early-out) & equivalence
# ─────────────────────────────────────────────────────────────────────────────


def _large_fuzzy_case() -> tuple[str, str]:
    """A ~580-cp near-verbatim quote embedded once in a ~500k-cp text. The filler repeats a long
    quote token ("evaluations") so seeding yields many false-positive windows — stressing the DP,
    not just the projection."""
    needle = (
        "the alignment research programme measured substantial and consistent improvements across "
        "a wide battery of controlled adversarial robustness evaluations conducted by several "
        "independent laboratories over many months using carefully preregistered protocols and "
        "transparent reproducible open source tooling throughout the entire multiyear study period "
        "with detailed appendices documenting the methodology datasets and statistical choices made "
        "alongside a thorough discussion of limitations threats to validity and future replications"
    )
    assert 500 <= len(needle) <= MAX_QUOTE_CP
    filler = "routine evaluations of lorem ipsum dolor sit amet consectetur adipiscing elit sed "
    body = filler * 6000
    text = body[:250_000] + " " + needle + " " + body[:250_000]
    quote = needle.replace("substantial", "substantiol")  # one-letter typo
    return quote, text


def test_fuzzy_latency_on_500k_text() -> None:
    # A 600-cp-class quote on a 500k-cp text anchors (§6.2 fix 2). No wall-clock bound: it flaked on
    # a loaded host; the banded DP's cost shows in the suite's durations instead.
    quote, text = _large_fuzzy_case()
    assert len(text) >= 500_000
    a = A.anchor(quote, text)
    assert a is not None
    assert a.method == "fuzzy"
    assert a.score >= 0.98


# The fuzzy (quote, text) pairs the suite exercises — the "existing fuzzy corpus" the banded DP
# must reproduce EXACTLY versus a full unbanded DP (fix 2 correctness guardrail).
_LONG = "the alignment tax measurement improved substantially to 23 percent overall in tests"
_FUZZY_CORPUS: list[tuple[str, str]] = [
    ("the alignment problem", "the Alignment Problem"),  # case-only (fallback path)
    ("the quick brown fox jumped over the lazy dog and ran away quickly".replace("lazy", "lasy"),
     "the quick brown fox jumped over the lazy dog and ran away quickly"),  # accept
    ("the quick brown fox jumped".replace("brown", "brawn"),
     "the quick brown fox jumped"),  # below threshold
    (_LONG.replace(" 23 ", " 21 "), _LONG),  # digit-run violation (rejected above threshold)
    (_LONG.replace("overall", "overoll"), _LONG),  # one-letter accept control
    ("the controlled adversarial evaluation measured accuracy improving to ٩٢ overall",
     "the controlled adversarial evaluation measured accuracy improving to ٩٣ overall"),  # Nd digit
]


def test_banded_matches_unbanded_on_fuzzy_corpus() -> None:
    # Banding + early-out must not change the accept/reject/span/score outcome vs a full unbanded
    # DP over the same windows (fix 2 guardrail), including the 500k adversarial case.
    banded = Anchorer()
    unbanded = Anchorer()
    unbanded._banded = False
    for q, t in _FUZZY_CORPUS + [_large_fuzzy_case()]:
        assert banded.anchor_detail(q, t).anchor == unbanded.anchor_detail(q, t).anchor, q[:48]
