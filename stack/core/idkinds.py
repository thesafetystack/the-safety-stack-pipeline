"""Row-id kind letters and the id shape (20-technical §1).

Every row id is ``<type letter> + 12`` lowercase Crockford base32 chars (60 random bits).
``ID_KINDS`` maps each domain table's kind name to its one-letter prefix; ``ID_RE`` is the
full-id regex (mirrors the DDL ``id_ok`` check). A module-level ``_check()`` runs at import
time and fails fast if the table, the regex, the retired set, and the alphabet ever disagree.
"""

from __future__ import annotations

import re
import string
from typing import Mapping

# Kind name -> type letter (20-technical §1). Order matches the spec's enumeration.
ID_KINDS: Mapping[str, str] = {
    "source": "s",
    "snapshot": "n",
    "metadata": "m",
    "annotation": "a",
    "aggregate": "g",
    "mask": "k",
    "review": "v",
    "run": "r",
    "sweep": "w",
    "llm_call": "c",
    "coverage": "o",
    "fetch_log": "f",
}

# Letters retired with their subsystem and NEVER reused ('l' was the removed links table).
RETIRED_LETTERS = frozenset({"l"})

# Lowercase Crockford base32 (no i/l/o/u).
ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

# Full id: one type letter + 12 body chars from the alphabet.
ID_RE = re.compile(r"^[snmagkvrwcof][0-9abcdefghjkmnpqrstvwxyz]{12}$")

_LETTER_SET = frozenset(ID_KINDS.values())


def _leading_class() -> frozenset[str]:
    """The set of type letters the regex's leading character class accepts."""
    m = re.match(r"\^\[([^\]]+)\]", ID_RE.pattern)
    assert m is not None, "ID_RE must start with a literal leading character class"
    return frozenset(m.group(1))


# Type letters are drawn from lowercase ASCII a-z. NOTE (spec finding): they are a positional
# PREFIX, NOT members of the Crockford base32 BODY alphabet — 'o' (coverage) is a valid type
# letter even though ID_ALPHABET excludes i/l/o/u. So the "drawn from the alphabet" check below
# means the Latin lowercase alphabet, not ID_ALPHABET; asserting the latter would (correctly)
# fail on 'o' and make this module unimportable against the spec's own §1 letter table.
_LOWERCASE = frozenset(string.ascii_lowercase)


def _check() -> None:
    """Assert table/regex/retired/alphabet consistency (run at import)."""
    letters = list(ID_KINDS.values())
    # (1) letters unique
    assert len(letters) == len(_LETTER_SET), f"duplicate id kind letters: {letters}"
    lead = _leading_class()
    for kind, letter in ID_KINDS.items():
        # (2) every letter appears in the regex's leading class
        assert letter in lead, f"letter {letter!r} for {kind!r} missing from ID_RE leading class"
        # (4) every letter is a lowercase ASCII letter (see _LOWERCASE note above)
        assert letter in _LOWERCASE, f"letter {letter!r} for {kind!r} is not lowercase a-z"
    # (3) no retired letter is in use
    assert not (_LETTER_SET & RETIRED_LETTERS), (
        f"retired letters reused: {sorted(_LETTER_SET & RETIRED_LETTERS)}"
    )


def kind_letter(kind: str) -> str:
    """Resolve ``kind`` to its type letter.

    Accepts either a kind name (``"snapshot"`` -> ``"n"``) or an already-valid single letter
    (``"n"`` -> ``"n"``). Raises ``KeyError`` for an unknown kind name and ``ValueError`` for
    a one-character string that is not a valid type letter.
    """
    if kind in ID_KINDS:
        return ID_KINDS[kind]
    if len(kind) == 1:
        if kind in _LETTER_SET:
            return kind
        raise ValueError(f"unknown id type letter {kind!r}")
    raise KeyError(kind)


_check()
