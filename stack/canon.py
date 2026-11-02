"""``stack.canon`` — the config-pure entity canonicalizers (10-design §9.3).

``canon_org`` / ``canon_model`` map a raw surface string onto a ``registry.yaml`` slug by
ORDERED FIRST-MATCH over the alias regexes, exactly as ``registry.yaml``'s own header states:
"Aliases are ORDERED first-match regexes (strata-style); canon_model/canon_org walk them
top-to-bottom and the first match wins". The walk is over the FLATTENED (entry, alias) list in
FILE ORDER, so specificity is expressed by entry ordering — ``microsoft-research`` must appear
above ``microsoft`` in the file, and a test asserts it.

Referentially transparent: pure functions of the pinned config, safe to re-run on any registry
change. That is what lets the affiliation pipeline store raw surfaces and resolve orgs on
any later read, instead of baking an org id into an LLM payload (see
``workflows/annotate/affiliation``).

Three surfaces, because two genuinely different questions hide behind "canonicalize an org":

* :meth:`Canon.canon_org` — "this string names ONE entity; which?" The §9.3 protocol contract
  (``spec/types/interfaces.py``): first match wins, one :class:`CanonResult`.
* :meth:`Canon.orgs_in` — "this free text MENTIONS which tracked orgs?" (v1's
  ``corpus.match_orgs``). A dual affiliation — ``"MIT ; Anthropic"`` — is one affiliation
  string naming two orgs, so a scalar answer would silently drop one. Overlapping matches
  resolve to the earlier (more specific) entry, so ``"Microsoft Research"`` yields
  ``microsoft-research`` alone, never both it and ``microsoft``.
* :meth:`Canon.org_for_domain` — email-domain corroboration via ``registry.orgs[].domains``
  (v1's coarse third tier: ``alice@anthropic.com`` → ``anthropic``).

Unmatched is a first-class answer, not a failure: ``CanonResult(slug=None, matched=False)``.
§9.3 says an unmatched surface may raise a ``proposal``, but this module never decides that —
per-document proposals would flood on every university. The frequency count that makes the
decision meaningful belonged to v3's ``aggregate.org_attribution`` (retired at T1.8); v4 has none yet.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from stack.config import ModelEntry, OrgEntry, RegistryConfig, StackConfig


@dataclass(frozen=True)
class CanonResult:
    """Output of a canonicalizer (§9.3): the matched registry slug, or ``None`` when the raw
    string matched no ``registry.yaml`` entry.

    Mirrors the ``spec/types/interfaces.py`` dataclass of the same name. ``rule`` carries the
    alias pattern that fired, so a surprising match is traceable to the line that caused it
    without re-running the walk.
    """

    slug: str | None
    matched: bool
    rule: str | None = None


# The unmatched answer is identical every time; one shared instance keeps identity checks cheap
# and makes `is _UNMATCHED` a legal (if unused) shortcut.
_UNMATCHED = CanonResult(slug=None, matched=False, rule=None)

_WS = re.compile(r"\s+")


def _norm(raw: str) -> str:
    """NFKC + whitespace collapse, applied to every surface before the alias walk.

    Aliases are written against clean single-spaced text, but PDF-derived surfaces routinely
    carry NBSPs, thin spaces, doubled spaces, and line breaks — ``"Microsoft Research"``
    must not fall through ``\\bmicrosoft research\\b`` onto the bare ``microsoft`` entry, which
    is a silently *wrong* answer, not a miss. NFKC also folds ligatures and superscripts, the
    other conversion artefacts that break literal alias patterns.
    """
    return _WS.sub(" ", unicodedata.normalize("NFKC", raw))


@dataclass(frozen=True)
class _Alias:
    """One compiled (entry, alias) pair in file order — the unit the first-match walk steps over."""

    slug: str
    pattern: str
    regex: re.Pattern[str]


class CanonError(ValueError):
    """A ``registry.yaml`` alias failed to compile. Raised at construction — fail loud and early
    (30-stack-module §3), never mid-sweep on the one document whose surface hit the bad rule."""


def _compile(entries: Iterable[tuple[str, Sequence[str]]], kind: str) -> list[_Alias]:
    """Flatten ``(slug, aliases)`` into the ordered alias table, preserving FILE ORDER.

    File order IS the precedence rule (§9.3), so this must not sort, dedupe by slug, or reorder
    by pattern length — a caller wanting ``microsoft-research`` to beat ``microsoft`` expresses
    that by entry position, which is reviewable in the config diff.
    """
    table: list[_Alias] = []
    for slug, aliases in entries:
        for pattern in aliases:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise CanonError(
                    f"registry.yaml {kind} '{slug}': alias {pattern!r} is not a valid regex: {exc}"
                ) from exc
            table.append(_Alias(slug=slug, pattern=pattern, regex=regex))
    return table


class Canon:
    """The §9.3 canonicalizer service, built once per run from the pinned config.

    Implements the ``Canonicalizer`` protocol (``canon_model`` / ``canon_org``) and adds the two
    surfaces the affiliation pipeline needs (:meth:`orgs_in`, :meth:`org_for_domain`). Holds only
    compiled regexes derived from ``registry.yaml`` — no connection, no I/O, no mutable state —
    so it is safe to share and trivial to construct in a test.
    """

    def __init__(self, registry: RegistryConfig) -> None:
        self._orgs = _compile(((o.slug, o.aliases) for o in registry.orgs), "org")
        self._models = _compile(((m.slug, m.aliases) for m in registry.models), "model")
        self._org_kind = {o.slug: o.kind for o in registry.orgs}
        # Longest-suffix-wins domain map: 'research.anthropic.com' must resolve through
        # 'anthropic.com'. Sorted longest-first so the first suffix hit is the most specific.
        domains: list[tuple[str, str]] = []
        for org in registry.orgs:
            for domain in org.domains:
                domains.append((domain.strip().lower().lstrip("."), org.slug))
        self._domains = sorted(domains, key=lambda pair: len(pair[0]), reverse=True)

    @classmethod
    def from_config(cls, config: StackConfig) -> Canon:
        """Build from a loaded :class:`~stack.config.StackConfig` (the runner's path)."""
        return cls(config.registry)

    # ── the §9.3 protocol surface ──────────────────────────────────────────────────────────

    def canon_org(self, raw: str) -> CanonResult:
        """Canonicalize a raw org surface to a registry slug (ordered first match)."""
        return self._first_match(raw, self._orgs)

    def canon_model(self, raw: str) -> CanonResult:
        """Canonicalize a raw model surface to a registry slug (ordered first match)."""
        return self._first_match(raw, self._models)

    @staticmethod
    def _first_match(raw: str, table: Sequence[_Alias]) -> CanonResult:
        if not raw:
            return _UNMATCHED
        raw = _norm(raw)
        for alias in table:
            if alias.regex.search(raw):
                return CanonResult(slug=alias.slug, matched=True, rule=alias.pattern)
        return _UNMATCHED

    # ── affiliation-facing surfaces ────────────────────────────────────────────────────────

    def orgs_in(self, raw: str) -> list[CanonResult]:
        """Every tracked org MENTIONED in ``raw``, in first-occurrence order (v1 ``match_orgs``).

        One affiliation field legitimately names several orgs — ``"MIT ; Anthropic"`` is a dual
        affiliation, and returning only the first would silently drop half of it.

        Overlap resolution keeps first-match precedence honest across a scan: an alias whose
        match OVERLAPS a span already claimed by an earlier (more specific) entry is skipped, so
        ``"Microsoft Research"`` yields ``microsoft-research`` alone rather than both it and the
        bare ``microsoft`` sitting inside the same span. Non-overlapping matches all survive,
        which is what makes the dual-affiliation case work.
        """
        if not raw:
            return []
        raw = _norm(raw)
        claimed: list[tuple[int, int]] = []
        found: dict[str, CanonResult] = {}
        for alias in self._orgs:
            for match in alias.regex.finditer(raw):
                start, end = match.span()
                if any(start < c_end and c_start < end for c_start, c_end in claimed):
                    continue  # a more specific entry already owns this text
                claimed.append((start, end))
                found.setdefault(
                    alias.slug, CanonResult(slug=alias.slug, matched=True, rule=alias.pattern)
                )
        return list(found.values())

    def org_for_domain(self, domain: str) -> CanonResult:
        """Resolve an email domain to an org via ``registry.orgs[].domains`` (v1's coarse tier).

        Matches the domain itself or any subdomain of it. Only DISTINCTIVE domains belong in the
        config — v1's lesson, preserved in ``registry.yaml``'s comments: broad shared domains
        (google.com, *.edu) are deliberately absent, so this tier stays a corroboration signal
        rather than a source of misattribution.
        """
        d = (domain or "").strip().lower().lstrip("@").rstrip(".")
        if not d:
            return _UNMATCHED
        for registered, slug in self._domains:
            if d == registered or d.endswith("." + registered):
                return CanonResult(slug=slug, matched=True, rule=f"domain:{registered}")
        return _UNMATCHED

    def org_kind(self, slug: str) -> str | None:
        """The ``org_kind`` of a registry slug (``lab``/``company``/``nonprofit``/``institute``),
        or None for an unknown slug. Lets a consumer group by kind without re-reading config."""
        return self._org_kind.get(slug)

    @property
    def org_slugs(self) -> list[str]:
        """Registry org slugs in file order — the menu, for reporting and tests."""
        seen: dict[str, None] = {}
        for alias in self._orgs:
            seen.setdefault(alias.slug, None)
        return list(seen)


def email_domain(email: str) -> str:
    """The domain part of an email address, lowercased ('' when there is no '@').

    The no-'@' guard matters: ``rpartition`` returns the WHOLE string as its tail when the
    separator is absent, so an author with an empty/garbage email field would otherwise be
    looked up as if the garbage were a domain.
    """
    local, sep, domain = (email or "").strip().rpartition("@")
    if not sep or not local:
        return ""
    return domain.strip().lower().rstrip(".")
