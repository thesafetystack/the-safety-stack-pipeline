"""Content-hashing recipes — 20-technical §3, byte-exact cross-implementer contract.

The GOLDEN digests below were produced by the implementation and reviewed; they are the
contract two independent implementers must reproduce (C-S1, D-S14). Any change here is a
change to the wire format. Plus behavioural checks: the JCS number/key rules.
"""

from __future__ import annotations

import pytest

from stack.core import hashes as h

# ─────────────────────────── golden digests ────────────────────────────────


def test_raw_digest_golden() -> None:
    """raw_digest = sha256(raw bytes) (§3)."""
    assert h.raw_digest(b"stack v3") == (
        "f2cb7f204e89eea4e4e6689fe0e9c1843bc9868b3c9a848f9ee390c497728fd3"
    )


def test_text_digest_golden() -> None:
    """text_digest = sha256(UTF-8 of the exact stored Markdown) (§3)."""
    assert h.text_digest("Hello\nstack") == (
        "2021f0b7a948dd20c642e7b71f1b8413dfbbafdaf689deaa31db93b3d9bd8f3a"
    )


@pytest.mark.parametrize("text", ["Hello\nstack", "", "Příliš žluťoučký kůň \U0001d400 \U0001f600 \ufffd",
                                  "x" * 100_000])
def test_utf8_safe_keeps_text_without_surrogates(text: str) -> None:
    """D586: a text without surrogates (non-BMP characters and U+FFFD included) is returned
    unchanged, the same object, so its text_digest is the one it had before D586."""
    assert h.utf8_safe(text) is text
    assert h.text_digest(h.utf8_safe("Hello\nstack")) == (
        "2021f0b7a948dd20c642e7b71f1b8413dfbbafdaf689deaa31db93b3d9bd8f3a"
    )


def test_utf8_safe_pairs_and_replaces_surrogates() -> None:
    """D586: a surrogate pair held as two code points becomes its character, a lone surrogate
    (high, low, or a high before a complete pair) U+FFFD; the result encodes as UTF-8."""
    hi, lo = chr(0xD835), chr(0xDC00)
    with pytest.raises(UnicodeEncodeError):
        (hi + lo).encode("utf-8")
    cases = {hi + lo: "\U0001d400", f"a{hi}b": "a\ufffdb", f"a{lo}b": "a\ufffdb", f"{hi}{hi}{lo}": "\ufffd\U0001d400",
             f"{lo}{hi}": "\ufffd\ufffd", f"end{hi}": "end\ufffd"}
    for text, want in cases.items():
        assert h.utf8_safe(text) == want, ascii(text)
        h.text_digest(h.utf8_safe(text))  # encodes


def test_input_digest_golden() -> None:
    """input_digest = sha256('in1\\n' + user-message text) (§3)."""
    assert h.input_digest("What techniques does the doc describe?") == (
        "5372b7621c3b8327fef5019129997964cbc9b0f1dc1ba52940d85ae1a7fa0017"
    )


# rp1 args reused by rendered_hash / template_hash.
_RP_PROMPT = "SYSTEM\nline two"
_RP_SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}}
_RP_PARAMS = {"model": "openai/gpt-6-luna", "max_tokens": 512, "temperature": 1.0}


def test_rendered_hash_golden() -> None:
    """rendered_hash = sha256('rp1\\n' + json.dumps(rendered) + '\\n' + jcs(schema) + '\\n' +
    jcs(params)); temperature 1.0 canonicalizes to '1' (§3)."""
    assert h.rendered_hash(_RP_PROMPT, _RP_SCHEMA, _RP_PARAMS) == (
        "65c48f959a3a792197fac839da0d307de5874cc0b090bfb229eb2079337fbfcc"
    )


def test_template_hash_equals_rendered_over_template_form() -> None:
    """template_hash shares the rp1 recipe — identical output on the same arguments (§3, D-M8)."""
    assert h.template_hash(_RP_PROMPT, _RP_SCHEMA, _RP_PARAMS) == h.rendered_hash(
        _RP_PROMPT, _RP_SCHEMA, _RP_PARAMS
    )


# ─────────────────────────── JCS (RFC 8785) ────────────────────────────────


def test_jcs_object_key_sort_and_number_canon() -> None:
    """JCS sorts keys and canonicalizes numbers: 1.0 -> '1', 0.5 stays (§3)."""
    assert h.jcs({"b": 1, "a": [1.0, 0.5, "x"]}) == '{"a":[1,0.5,"x"],"b":1}'


def test_jcs_key_sort() -> None:
    """JCS object keys are sorted (§3)."""
    assert h.jcs({"z": 1, "a": 2, "m": 3}) == '{"a":2,"m":3,"z":1}'


@pytest.mark.parametrize(
    "value,expected",
    [
        (1.0, "1"),  # integral float -> integer form
        (-0.0, "0"),  # negative zero normalizes to '0'
        (1e-07, "1e-7"),  # exponent leading zeros stripped
        (1e21, "1e+21"),  # >= 1e21 keeps exponent form
    ],
)
def test_jcs_float_canonicalization(value: float, expected: str) -> None:
    """JCS number canonicalization (ECMAScript Number::toString) (§3)."""
    assert h.jcs(value) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_jcs_rejects_non_finite(bad: float) -> None:
    """JCS has no representation for NaN/±Inf -> ValueError (§3)."""
    with pytest.raises(ValueError):
        h.jcs(bad)


@pytest.mark.parametrize("bad", [object(), {1, 2}, (1, 2)])
def test_jcs_rejects_non_json_types(bad: object) -> None:
    """Non-JSON scalars (object, set, tuple) -> TypeError (§3)."""
    with pytest.raises(TypeError):
        h.jcs(bad)


def test_jcs_rejects_non_str_keys() -> None:
    """JSON objects key on strings; a non-str key -> TypeError (§3)."""
    with pytest.raises(TypeError):
        h.jcs({1: "x"})


def test_text_digest_is_byte_sensitive() -> None:
    """text_digest hashes the exact bytes: edge whitespace changes it (§3)."""
    assert h.text_digest("hello") != h.text_digest("   hello   ")
