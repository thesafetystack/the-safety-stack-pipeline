"""The pointer rule (§5.5 with D8-D10): one pure function from a ``content`` block and the
document's members to the ``current_*`` fields. Substage ``point`` (pure, always runs, D32).

**Candidates**: ``snapshots[]`` with ``kind IN ('raw', 'inband')`` and a pointer-rank role
(:data:`~stack.schemas.vocab.POINTER_RANK`; ``xml``/``archive`` are never text) whose
``source_id`` is a **live** member (``is_live``: masked and split-off members are not candidates;
withdrawn members are). **Order**: role rank, ``via <> 'wayback'`` first, ``as_of`` DESC (NULL
last), ``captured_at`` DESC, ``snapshot_id``. The first is current; then **its** text only:

=========================================  ======================  ==================
current entry                              ``current_text_sha256``  ``current_text_status``
=========================================  ======================  ==================
none                                       NULL                    ``no_raw``
``inband`` with ``text`` (collector        its ``text.sha256``     ``ok``
text, or converted at the pin)
``inband`` collector text the quality      NULL                    ``convert_failed``
gate failed (``failed_permanent``)
``raw``/``inband`` converted ``ok`` at pin  its ``text.sha256``     ``ok``
conversion at the pin ``failed_*`` or      NULL                    ``convert_failed``
``quarantined``
not converted at the pin (none yet, or     NULL                    ``convert_pending``
another config, or ``not_attempted``)
=========================================  ======================  ==================

The pin is the plan's convert config digest (``content.convert_pin`` records the pin a block
was pointed at). Never consulted: older occurrences' text, other configs, masked members,
never-text roles; a newer raw whose conversion failed gives ``convert_failed``, never an older
text (§5.5). An in-band entry's own text (``convert_pin`` NULL: the collector's) is
valid at any pin (D10), unless the quality gate failed it (round 3, Q: ``text_status =
'failed_permanent'`` with a ``quality_*`` class; the ``text`` stays on the snapshot, the pointer
does not use it and does not fall back to another candidate, as for a failed conversion).

``current_text_media``: ``txt`` for an in-band ``text/plain`` body (an abstract), ``md`` for every
other text (converted output and the forum collectors' markdown); NULL unless ``ok``.
``current_revision`` is the current snapshot's ``revision``.

**Structured text** (round 5, T-RUN B8; the acquire line's ``options.structured: {jats: bool,
arxiv_html: bool}``, :func:`structured_of`; default off, and then nothing above changes). An enabled
structured role (:data:`~stack.schemas.vocab.STRUCTURED_ROLES`: ``jats`` → ``xml``, ``arxiv_html`` →
``html``) puts its raw snapshots of live members into the order (:func:`ranked`): one whose text is
``ok`` at the pin (converted, and not failed by the quality gate) sorts **immediately before its
partner**, the best-ranked ``full_text`` candidate of the same ``source_id`` (and the same
``revision`` when it carries one: arXiv ``html`` ``v2`` pairs with the ``v2`` PDF, never with
``v1``); without a partner it ranks as a ``full_text`` candidate of its own (``as_of``, captured).
So the structured text is chosen over its own source's PDF of the same revision, the PDF is the
fallback while the structured text is not converted, failed to convert or failed the quality gate,
and nothing else moves: another source's newer PDF, a newer revision, the wayback rule. Then
``current_role`` is ``xml``/``html`` (a plan filtering on ``current_role = 'full_text'`` must widen
it). ``convert`` and ``quality`` handle :func:`needed`: the order's prefix up to and including the
first non-structured candidate, with the structured snapshots that are not yet converted (or, for
``quality``, that hold a text at the pin, so a quality-failed one is re-judged under a new gate).
**Too short** (round 5, B9 after B8's ``sxad2hrqd8d7z``: a truncated LaTeXML page of 25.7k chars
against its PDF's 101k passed the gate): when both carry a quality verdict and the structured
text has fewer than :data:`STRUCTURED_MIN_RATIO` (0.5) × its partner's ``chars``
(``quality.metrics.chars``), it sorts **after** its partner, so the PDF is current
(:func:`too_short`; counted ``structured.<role>.too_short`` by the quality comparison).

**With ``options.arxiv_pdf``** (D480-D482; the rule above does not change, only which arXiv PDFs
exist for it to rank). ``always``: as above. ``on_miss``: a paper whose HTML text is ``ok`` has no
PDF, so its HTML ranks as a ``full_text`` candidate of its own and is current; ``too_short`` cannot
apply (no partner) and a truncated page that passes the quality gate stays current. A paper whose
HTML is a miss gets its PDF a run later: until then nothing of that source is text (a failed HTML is
no candidate; the pointer falls to another member or the abstract, else ``no_raw``), after it the
PDF is current (the failed HTML is never preferred; an HTML of an older revision without a PDF of
its revision ranks by capture time, so the newer PDF wins). ``never``: the HTML text, else another
candidate; never a PDF fetched under this option, but a PDF held from an earlier ``always`` run
stays a candidate (snapshots are kept) and is ranked as above. Switching to ``always`` later adds the
PDF partners, so the pointer moves only by the rule above: an HTML that is now ``too_short`` against
its PDF of the same revision gives way to it; every other ``ok`` HTML stays current.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, NamedTuple

from stack.schemas.vocab import POINTER_RANK, STRUCTURED_ROLES

_RANK = {role: i for i, role in enumerate(POINTER_RANK)}
_FAILED = frozenset({"failed_transient", "failed_permanent", "quarantined"})
#: A structured text shorter than this share of its partner PDF's text is not preferred (B9).
STRUCTURED_MIN_RATIO = 0.5
#: The ``current_*`` fields :func:`point` returns (``current_text_quality`` is the quality
#: substage's, :func:`workflows.acquire.quality.current_quality`, not the pointer's).
POINTER_FIELDS: tuple[str, ...] = ("current_snapshot_id", "current_source_id", "current_role",
                                   "current_text_sha256", "current_text_status",
                                   "current_text_media", "current_revision")


def candidates(content: Mapping[str, Any], members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """§5.5's candidates in pointer order (first = current)."""
    live = {m["source_id"] for m in members if m["is_live"]}
    cands = [s for s in content.get("snapshots") or []
             if s["kind"] in ("raw", "inband") and s["source_id"] in live and s["content_role"] in _RANK]
    return sorted(cands, key=_order)


def _order(s: Mapping[str, Any], role: str | None = None) -> tuple[Any, ...]:
    as_of = s.get("as_of")
    return (_RANK[role or s["content_role"]], s["via"] == "wayback", as_of is None,
            -(as_of.timestamp() if as_of is not None else 0.0), -s["captured_at"].timestamp(),
            s["snapshot_id"])


class Structured(NamedTuple):
    """The acquire line's ``options.structured`` (module docstring, Structured text)."""

    jats: bool = False
    arxiv_html: bool = False

    @property
    def on(self) -> bool:
        return self.jats or self.arxiv_html

    @property
    def roles(self) -> frozenset[str]:
        """The enabled structured content roles."""
        return frozenset(r for r, on in zip(STRUCTURED_ROLES, (self.jats, self.arxiv_html), strict=True) if on)


def structured_of(options: Mapping[str, Any] | None) -> Structured:
    """``options.structured`` of the acquire line (absent = off); a malformed one is a ``ValueError``
    (``acquire`` reports it as a ``StageError`` before any substage)."""
    raw = (options or {}).get("structured")
    if raw is None:
        return Structured()
    if not isinstance(raw, Mapping):
        raise ValueError(  # noqa: TRY004 — one exception type for every malformed option (callers map it)
            f"options.structured must be a mapping {{jats: bool, arxiv_html: bool}}, got {raw!r}")
    unknown = sorted(set(raw) - set(Structured._fields))
    if unknown:
        raise ValueError(f"options.structured: unknown keys {unknown} (known: {list(Structured._fields)})")
    bad = {k: v for k, v in raw.items() if not isinstance(v, bool)}
    if bad:
        raise ValueError(f"options.structured: values must be true or false, got {bad}")
    return Structured(**raw)


def _chars(s: Mapping[str, Any]) -> int | None:
    v = ((s.get("quality") or {}).get("metrics") or {}).get("chars")
    return int(v) if v is not None else None


def too_short(structured: Mapping[str, Any], partner: Mapping[str, Any]) -> bool:
    """The structured text has a verdict and fewer than :data:`STRUCTURED_MIN_RATIO` × its partner's
    ``chars`` (module docstring, Too short); unknown lengths are never too short."""
    mine, theirs = _chars(structured), _chars(partner)
    return mine is not None and bool(theirs) and mine < STRUCTURED_MIN_RATIO * theirs


def ranked(content: Mapping[str, Any], members: Sequence[Mapping[str, Any]], structured: Collection[str],
           keep: Callable[[Mapping[str, Any]], bool]) -> list[dict[str, Any]]:
    """:func:`candidates` with the raw snapshots of the ``structured`` roles that ``keep`` admits
    placed in the order (module docstring, Structured text); ``structured`` empty = :func:`candidates`."""
    base = candidates(content, members)
    if not structured:
        return base
    live = {m["source_id"] for m in members if m["is_live"]}
    keyed: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((*_order(b), 1, 0.0, 0.0, ""), b) for b in base]
    fulls = [b for b in base if b["content_role"] == "full_text"]  # best first
    for s in content.get("snapshots") or []:
        if s["kind"] != "raw" or s["content_role"] not in structured or s["source_id"] not in live or not keep(s):
            continue
        rev = s.get("revision")
        partner = next((b for b in fulls if b["source_id"] == s["source_id"]
                        and (rev is None or b.get("revision") == rev)), None)
        if partner is None:
            keyed.append(((*_order(s, "full_text"), 1, 0.0, 0.0, ""), s))
        else:
            as_of = s.get("as_of")
            keyed.append(((*_order(partner), 2 if too_short(s, partner) else 0,
                           -(as_of.timestamp() if as_of is not None else 0.0),
                           -s["captured_at"].timestamp(), s["snapshot_id"]), s))
    keyed.sort(key=lambda kv: kv[0])
    return [s for _, s in keyed]


def needed(content: Mapping[str, Any], members: Sequence[Mapping[str, Any]], structured: Collection[str],
           keep: Callable[[Mapping[str, Any]], bool]) -> list[dict[str, Any]]:
    """The snapshots the ranking needs (``convert``, ``quality``): :func:`ranked`'s prefix up to and
    including the first candidate of a non-structured role (all of it when there is none)."""
    out: list[dict[str, Any]] = []
    for s in ranked(content, members, structured, keep):
        out.append(s)
        if s["content_role"] not in structured:
            break
    return out


def text_of(snapshot: Mapping[str, Any], pin: str | None) -> tuple[str | None, str]:
    """``(current_text_sha256, current_text_status)`` of one candidate at ``pin``."""
    status, cfg = snapshot.get("text_status"), snapshot.get("convert_pin")
    digest = (snapshot.get("text") or {}).get("sha256")
    if snapshot["kind"] == "inband" and cfg is None:
        # The collector's own text (or none yet: a raw-only in-band body waits for convert, D10),
        # unless the quality gate failed it (round 3, Q).
        if status in _FAILED:
            return None, "convert_failed"
        return (digest, "ok") if digest else (None, "convert_pending")
    if cfg is None or cfg != pin:
        return None, "convert_pending"
    if status == "ok" and digest:
        return digest, "ok"
    if status in _FAILED:
        return None, "convert_failed"
    return None, "convert_pending"


def media_of(snapshot: Mapping[str, Any]) -> str:
    """The text's media (§2.6 ``current_text_media``) of an ``ok`` candidate: ``txt`` for an in-band
    ``text/plain`` body (an abstract), else ``md`` (a fetched plain-text page that converts to itself is a
    body: ``md``, though its ``text`` BLOB says ``text/plain``)."""
    if snapshot["kind"] == "inband" and str(snapshot["raw"]["media_type"]).startswith("text/plain"):
        return "txt"
    return "md"


def point(content: Mapping[str, Any], members: Sequence[Mapping[str, Any]],
          pin: str | None, structured: Collection[str] = ()) -> dict[str, Any]:
    """The ``current_*`` fields (:data:`POINTER_FIELDS`) of ``content`` over ``members`` at the
    convert ``pin``, with the enabled ``structured`` roles (module docstring). Pure: reads nothing
    else, changes nothing."""
    cands = ranked(content, members, structured, lambda s: text_of(s, pin)[1] == "ok")
    if not cands:
        return dict.fromkeys(POINTER_FIELDS) | {"current_text_status": "no_raw"}
    cur = cands[0]
    digest, status = text_of(cur, pin)
    return {"current_snapshot_id": cur["snapshot_id"], "current_source_id": cur["source_id"],
            "current_role": cur["content_role"], "current_text_sha256": digest,
            "current_text_status": status,
            "current_text_media": media_of(cur) if status == "ok" else None,
            "current_revision": cur.get("revision")}


__all__ = ["POINTER_FIELDS", "STRUCTURED_MIN_RATIO", "Structured", "candidates", "media_of", "needed", "point",
           "ranked", "structured_of", "text_of", "too_short"]
