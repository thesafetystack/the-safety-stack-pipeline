"""Row-id minting and validation — 20-technical §1, 30-stack-module §4.

Covers the id shape (type letter + 12 Crockford-base32 body chars, no i/l/o/u), the fact
that the type-letter namespace is SEPARATE from the body alphabet (``o`` is a legal letter
but not a legal body char), ``mint`` accepting both kind names and letters, ``is_valid``'s
``fullmatch`` semantics, the ``ID_KINDS`` letter table, and ``RETIRED_LETTERS``.
"""

from __future__ import annotations

import re

import pytest

from stack.core.ids import is_valid, mint
from stack.core.idkinds import ID_ALPHABET, ID_KINDS, ID_RE, RETIRED_LETTERS

# 20-technical §1 letter table, verbatim (the cross-implementer contract).
_EXPECTED_KINDS = {
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

# The §1 full-id regex, re-derived here so the test is independent of ID_RE's source text.
_SHAPE = re.compile(r"^[snmagkvrwcof][0-9abcdefghjkmnpqrstvwxyz]{12}$")


# ─────────────────────────── the letter table ──────────────────────────────


def test_id_kinds_table_matches_spec_exactly() -> None:
    """ID_KINDS is exactly the 12-kind §1 letter table (all kinds, all letters)."""
    assert dict(ID_KINDS) == _EXPECTED_KINDS
    assert len(ID_KINDS) == 12
    # Letters are unique (no two kinds share a prefix).
    assert len(set(ID_KINDS.values())) == 12


def test_retired_letters() -> None:
    """'l' (the removed links table) is retired and no live kind reuses it (§1)."""
    assert "l" in RETIRED_LETTERS
    assert not (set(ID_KINDS.values()) & RETIRED_LETTERS)


def test_alphabet_excludes_ilou() -> None:
    """The body alphabet is lowercase Crockford base32 — no i/l/o/u (§1)."""
    assert ID_ALPHABET == "0123456789abcdefghjkmnpqrstvwxyz"
    for c in "ilou":
        assert c not in ID_ALPHABET


# ─────────────────────────── mint ──────────────────────────────────────────


@pytest.mark.parametrize("kind", list(ID_KINDS))
def test_mint_shape_by_name(kind: str) -> None:
    """mint(kind name) → type letter + 12 body chars, all from the alphabet (§1)."""
    rid = mint(kind)
    assert len(rid) == 13
    assert _SHAPE.fullmatch(rid) is not None
    assert rid[0] == ID_KINDS[kind]
    assert all(c in ID_ALPHABET for c in rid[1:])


@pytest.mark.parametrize("kind,letter", list(ID_KINDS.items()))
def test_mint_accepts_names_and_letters(kind: str, letter: str) -> None:
    """mint resolves both a kind name and a bare type letter (§4/kind_letter)."""
    assert mint(kind)[0] == letter
    assert mint(letter)[0] == letter


def test_mint_distinctness() -> None:
    """Many mints are distinct (60 random bits; birthday collisions ~never at this n)."""
    ids = {mint("annotation") for _ in range(5000)}
    assert len(ids) == 5000


def test_mint_body_never_contains_excluded_chars() -> None:
    """A minted body never contains i/l/o/u (the CSPRNG draws only from ID_ALPHABET)."""
    for _ in range(2000):
        body = mint("coverage")[1:]
        assert not (set(body) & set("ilou"))


# ─────────────────────────── is_valid ──────────────────────────────────────


@pytest.mark.parametrize("letter", list(ID_KINDS.values()))
def test_is_valid_accepts_every_kind_letter(letter: str) -> None:
    """Every type letter — INCLUDING 'o' (coverage) — leads a valid id (§1 namespace
    separation: 'o' is a legal letter though not a legal body char)."""
    assert is_valid(letter + "0" * 12) is True


def test_is_valid_accepts_o_letter_but_rejects_o_in_body() -> None:
    """'o' is a legal leading letter but an illegal body character (§1)."""
    assert is_valid("o" + "0" * 12) is True
    assert is_valid("a" + "o" + "0" * 11) is False


@pytest.mark.parametrize("bad", ["i", "l", "o", "u"])
def test_is_valid_rejects_excluded_body_chars(bad: str) -> None:
    """i/l/o/u in the BODY are rejected — they are not in the Crockford alphabet (§1)."""
    assert is_valid("a" + bad + "0" * 11) is False


@pytest.mark.parametrize("rid", ["a" + "0" * 11, "a" + "0" * 13, "a", "a0", "", "0" * 13])
def test_is_valid_rejects_wrong_length(rid: str) -> None:
    """Anything but letter + exactly 12 body chars is invalid (§1)."""
    assert is_valid(rid) is False


@pytest.mark.parametrize("rid", ["A" + "0" * 12, "a" + "B" + "0" * 11, "S" + "0" * 12])
def test_is_valid_rejects_uppercase(rid: str) -> None:
    """Uppercase in the letter or the body is invalid (alphabet is lowercase; §1)."""
    assert is_valid(rid) is False


@pytest.mark.parametrize("letter", ["x", "b", "d", "e", "h", "j", "p", "q", "t", "y", "z", "1"])
def test_is_valid_rejects_unknown_leading_letter(letter: str) -> None:
    """A leading char outside the §1 letter set is invalid (even valid body chars like 'b')."""
    assert is_valid(letter + "0" * 12) is False


def test_is_valid_rejects_trailing_newline() -> None:
    """A trailing newline is rejected — is_valid uses fullmatch, not match (§4 docstring)."""
    assert is_valid("a" + "0" * 12 + "\n") is False


def test_is_valid_kind_letter_check() -> None:
    """is_valid(id, kind) additionally pins the leading letter to that kind (§4)."""
    sid = "s" + "0" * 12
    assert is_valid(sid, "source") is True
    assert is_valid(sid, "s") is True
    assert is_valid(sid, "snapshot") is False  # letter s != n
    assert is_valid(sid, "n") is False
    assert is_valid("n" + "0" * 12, "source") is False
    # A malformed id fails the kind check too (shape checked first).
    assert is_valid("s" + "0" * 11, "source") is False
