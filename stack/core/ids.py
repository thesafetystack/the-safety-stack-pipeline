"""Row-id minting and validation — 20-technical §1, 30-stack-module §4.

Format: ``<type letter> + 12 chars`` from the lowercase Crockford base32 alphabet
(``0123456789abcdefghjkmnpqrstvwxyz``, no i/l/o/u) = 60 random bits from a CSPRNG. The letter
table and alphabet live in ``stack.core.idkinds`` (a checked mapping); this module only mints
and validates.
"""

from __future__ import annotations

import secrets

from stack.core.idkinds import ID_ALPHABET, ID_RE, kind_letter

_BODY_LEN = 12


def mint(kind: str) -> str:
    """Mint a fresh row id: the type letter for ``kind`` + 12 CSPRNG base32 chars.

    ``kind`` may be a kind *name* (e.g. ``"source"``) or a type *letter* (e.g. ``"s"``); both
    resolve through :func:`stack.core.idkinds.kind_letter`.

    There is intentionally **NO retry / collision handling here**. Per the §1 collision policy,
    id uniqueness is enforced by the DB primary key; the caller's insert loop catches the PK
    violation and calls :func:`mint` again (the birthday bound at 1e7 rows makes retries
    effectively never). This function is a pure CSPRNG generator.
    """
    letter = kind_letter(kind)
    body = "".join(secrets.choice(ID_ALPHABET) for _ in range(_BODY_LEN))
    return letter + body


def is_valid(id: str, kind: str | None = None) -> bool:
    """Whether ``id`` is a well-formed row id (§1 full-shape regex).

    When ``kind`` is given (a kind name or a type letter), also require the leading letter to
    match that kind. Returns ``False`` for any shape mismatch rather than raising.

    Uses ``fullmatch`` (not ``match``): the anchored §1 pattern's trailing ``$`` would let
    ``match`` accept a valid id followed by a trailing newline; ``fullmatch`` requires the whole
    string to be the id.
    """
    if ID_RE.fullmatch(id) is None:
        return False
    if kind is not None:
        return id[0] == kind_letter(kind)
    return True
