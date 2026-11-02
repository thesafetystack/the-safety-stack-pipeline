"""Content-hashing recipes — byte-exact per 20-technical §3.

Every recipe here is normative: two independent implementers MUST reproduce identical
hashes (C-S1, D-S14). All digests are sha256, lowercase hex.

Determinism rules (20-technical §3, verbatim):

* **Canonical JSON = RFC 8785 (JCS)** — sorted keys, no insignificant whitespace, the JCS
  number canonicalization, UTF-8, minimal string escaping. ``jcs()`` below is a minimal JCS
  implementation sufficient for the schema / params / window dicts these recipes hash.
* **Free-text fields are JSON-string-encoded on their own line** (``json.dumps(s)`` — quotes +
  ``\\n`` / ``\\"`` / ``\\uXXXX`` escapes) so embedded newlines cannot merge or split lines.
* **``\\n`` is a separator, not a terminator** (no trailing newline). An **empty list**
  serializes as the literal two-character string ``[]`` on its line (distinct from an absent
  optional field's empty string).

Recipe version tags (each recipe opens with a literal tag so recipes can evolve): ``in1``,
``rp1``. v3's ``ck1`` content key, ``agg1``/``stats1``/``ss1`` aggregate digests and config stamp
had no v4 caller and were removed.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from hashlib import sha256

# ─────────────────────────────────────────────────────────────────────────────
# RFC 8785 (JCS) — minimal canonical JSON
# ─────────────────────────────────────────────────────────────────────────────


def _canon_float(value: float) -> str:
    """ECMAScript ``Number::toString`` rendering of a finite float (the JCS number rule).

    * NaN / ±Inf are rejected (``ValueError``) — JCS has no representation for them.
    * ``-0.0`` renders as ``"0"``.
    * an integral value with ``abs < 1e21`` renders as an integer (``1.0`` -> ``"1"``); ES uses
      positional notation up to 1e21, so this matches ``str(int(value))``.
    * otherwise ``repr(value)`` (Python's shortest round-tripping form) with its exponent
      normalized to ES form: Python ``"1e-07"`` -> JCS ``"1e-7"`` (leading zeros stripped from
      the exponent digits, sign kept); Python and JCS already agree on ``"1e+21"``.
    """
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"non-finite float has no JCS representation: {value!r}")
    if value == 0.0:  # also normalizes -0.0 -> "0"
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    s = repr(value)
    if "e" in s:
        mant, _, exp = s.partition("e")
        sign = exp[0]  # Python float repr always carries an explicit exponent sign
        digits = exp[1:].lstrip("0") or "0"
        return f"{mant}e{sign}{digits}"
    return s


def jcs(value: object) -> str:
    """A minimal RFC 8785 (JCS) canonical-JSON serializer.

    Sufficient for the schema / params / window dicts the §3 recipes hash. Rules:

    * dict keys sorted by UTF-16 code units (sort key ``k.encode("utf-16-be")``); non-str keys
      are rejected (JSON objects key on strings).
    * no whitespace anywhere.
    * strings serialized exactly like ``json.dumps(s, ensure_ascii=False)`` — Python's escaping
      of control chars as ``\\uXXXX`` plus the short forms ``\\b \\t \\n \\f \\r`` matches JCS.
    * numbers: ``bool`` before ``int`` (``True`` -> ``true``); ints verbatim; floats via
      :func:`_canon_float`.
    * ``bool`` / ``None`` -> ``true`` / ``false`` / ``null``; lists recurse.
    * anything that is not str / int / float / bool / None / dict / list -> ``TypeError``
      (a non-``dict`` ``Mapping`` or a ``tuple`` is out of scope by the literal §3 enumeration).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):  # MUST precede int — bool is a subclass of int
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _canon_float(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        keys: list[str] = []
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"JCS object keys must be str, got {type(k).__name__}")
            keys.append(k)
        keys.sort(key=lambda kk: kk.encode("utf-16-be"))
        body = ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + jcs(value[k]) for k in keys
        )
        return "{" + body + "}"
    if isinstance(value, list):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    raise TypeError(f"value not JCS-serializable: {type(value).__name__}")


# ─────────────────────────────────────────────────────────────────────────────
# Raw / text digests
# ─────────────────────────────────────────────────────────────────────────────


def raw_digest(data: bytes) -> str:
    """``sha256(raw bytes)`` — the raw-fetch-bytes digest (§3)."""
    return sha256(data).hexdigest()


def text_digest(text: str) -> str:
    """``sha256(UTF-8 of the exact stored Markdown)`` (§3)."""
    return sha256(text.encode("utf-8")).hexdigest()


_SURROGATE = re.compile("[\ud800-\udfff]")


def utf8_safe(text: str) -> str:
    """``text`` made encodable as UTF-8 (D586): surrogate code points paired up into the characters
    they encode, and every lone one replaced by U+FFFD. A Python ``str`` can hold surrogates (a
    converter's output, a JSON ``\\ud83d`` escape without its partner) and UTF-8 refuses them, so
    text entering the text model from outside passes through here before it is stored or hashed. A
    text without surrogates is returned unchanged (the same object), so its digest is unchanged."""
    if _SURROGATE.search(text) is None:
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


# ─────────────────────────────────────────────────────────────────────────────
# LLM cache / prompt hashes
# ─────────────────────────────────────────────────────────────────────────────


def input_digest(user_msg: str) -> str:
    """``sha256("in1\\n" + user-message text)`` — the LLM cache input digest (§3)."""
    return sha256(("in1\n" + user_msg).encode("utf-8")).hexdigest()


def rendered_hash(
    rendered: str,
    schema: Mapping[str, object],
    params: Mapping[str, object],
) -> str:
    """``rp1`` — the rendered-prompt cache hash (§3):

    ``sha256("rp1\\n" + json.dumps(rendered) + "\\n" + jcs(schema) + "\\n" + jcs(params))``

    The rendered prompt is JSON-string-encoded (``json.dumps`` on the STRING -> a quoted,
    escaped literal) because it contains newlines; ``schema`` (output schema) and ``params``
    (sampling params ``{model, max_tokens, temperature, …}``) are canonical JSON (JCS).
    """
    payload = (
        "rp1\n"
        + json.dumps(rendered, ensure_ascii=False)
        + "\n"
        + jcs(dict(schema))
        + "\n"
        + jcs(dict(params))
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def template_hash(
    template: str,
    schema: Mapping[str, object],
    params: Mapping[str, object],
) -> str:
    """``rp1`` over the **template** form — the prompt-drift lineage hash (§3, D-M8).

    Shares the ``rp1`` tag and recipe with :func:`rendered_hash`; the only difference is the
    caller passes the unrendered template (versionless ``{{ref:<kind>}}`` placeholders intact)
    instead of the rendered prefix. Delegating keeps the two byte-identical by construction.
    """
    return rendered_hash(template, schema, params)
