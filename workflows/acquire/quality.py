"""Substage ``quality`` (pure; round 3, Q): the LLM-free quality verdict of every current text,
between ``convert`` and ``point``. The checks, the thresholds and the gate are
:mod:`stack.conversion.quality`; this module applies them to snapshots.

**Scope** (one snapshot per row, as ``convert``): the pointer's current candidate
(:func:`workflows.acquire.convert.current_candidate`) when it holds a text: a conversion at the
plan's pin (``ok``, or failed earlier by this gate) or an in-band collector text
(``convert_pin`` NULL with ``text``, D10). Older occurrences are never judged
(§5.5: never consulted).

**When** (:func:`needs_verdict`): the snapshot has no ``quality``, or one of another
:data:`~stack.conversion.quality.CHECKS_VERSION` (re-measured from the text), or of another gate
(re-judged from the stored metrics, no text read), or it was converted in this run (re-measured,
with the conversion's page count, truncation signal and converter stamp).

**Verdict on the snapshot** (``SNAPSHOT.quality``, D204): ``version``, ``bucket``, ``flags``,
``fails``, ``gate_digest``, ``converter`` with its ``tier`` (D690) and the ``metrics``. A ``failed`` bucket sets
``text_status = 'failed_permanent'`` with ``text_error_class = quality_<reason>`` and keeps
``text`` (the text stays reviewable and re-judgeable); the pointer then reads the row as
``convert_failed`` (:func:`workflows.acquire.point.text_of`), it never falls back to another
snapshot's text (§5.5). A later verdict that does not fail restores ``ok``. ``failed_permanent``
is final at the pin for ``convert``, so a quality failure is never re-converted at the same pin
(the bytes and the ladder are deterministic); a new pin or a new raw snapshot is converted and
judged afresh. One exception (D661): a PDF failed ``quality_garbled`` from a layout tier's
text is converted once more by the raw-text tier (:func:`workflows.acquire.convert.garbled_retry`)
and that text is judged here like any other.

**Facts** for :func:`stack.conversion.quality.measure`: the snapshot's role and media kind, its
raw size (``bytes``), the document's ``metadata.abstract``; from a conversion of this run the PDF
page count, the converter's truncation signal and its stamp; otherwise those stored in the
earlier verdict (``converter = 'inband'`` for a collector's text).

**Gate**: the acquire stage line's ``options.quality`` (:func:`gate_of`; a malformed one is a
:class:`~stack.stage_runner.StageError` before any lane call).

**Article in Press → version of record** (round 4, P; D206; through the history/refresh
mechanism). A verdict made from the text adds the provenance flag ``article_in_press`` when
:func:`stack.conversion.quality.in_press` says so (the snapshot's ``url``/``final_url`` is a known
Article-in-Press location, or the first page of a paper body carries the publisher's banner); a
re-judge from stored metrics keeps the flag of the earlier verdict. While the **current** text is so
flagged, :func:`arm_refresh` keeps the fetch role that holds it (same ``source_id`` and
``content_role``, a fetch lane, status ``ok``) due again at ``now + acquire.recheck.article_in_press``
(default 14 days; ``retry_after``, the D19/D22 recheck: no other scheduler), lowering a later
``retry_after``, never raising one. When it is due, ``fetch`` asks the lane again (the OA lane
tries the version-of-record URL first, :func:`workflows.acquire.lanes.oa.vor_first`): equal bytes
mint nothing and only the next due time is written; other bytes become a new raw snapshot, which
``convert`` and this substage judge afresh, and the pointer moves to it (§5.5, as an arXiv v2 does),
so the flag no longer describes the current text and the refresh stops. A row that is not due is
not in scope and makes no request.

**Stable supplier** (D807, :mod:`workflows.acquire.point`). The pointer keeps the previous current
snapshot over a new candidate with the same text; so that snapshot is judged too (:func:`judged_snapshots`)
when it is still ranked and holds the same text as a judged one, and ``content.current_text_quality``
reads a verdict of this gate whichever of the two the pointer keeps. A gate that fails the text fails
both, and the pointer then reads the order's first as before.

**Structured text** (round 5, T-RUN B8; the acquire line's ``options.structured``,
:mod:`workflows.acquire.point`). The scope becomes :func:`judged_snapshots`: every text in
:func:`workflows.acquire.point.needed` (the enabled structured snapshots ranked ahead of the current
candidate, and that candidate), so a structured text is judged before the pointer may choose it and
its PDF fallback is judged too. A structured text is judged as a paper body (role ``full_text``'s
thresholds, :func:`judge_role`); a ``failed`` one is ``failed_permanent[quality_*]`` as above and the
pointer then takes the PDF. Article-in-Press refresh follows the text the pointer will choose.
**The comparison** (:func:`compare`): for each structured text with a verdict whose partner (the
``full_text`` candidate that ends the prefix, same source and revision) has one too, when either verdict was
made in this run: ``structured.<role>.compared``, ``structured.<role>.bucket.<b>`` and
``.base_bucket.<b>`` (both verdicts), ``structured.<role>.len_ratio.<bin>`` (structured chars over
the partner's: ``lt0.5``, ``0.5-0.8``, ``0.8-1.25``, ``1.25-2``, ``ge2``), the
:func:`stack.conversion.quality.structure_markers` sums ``structured.<role>.math.structured|base``
and ``structured.<role>.tables.structured|base``, and which one the pointer takes
(``structured.<role>.current`` or ``.fallback``); ``structured.<role>.too_short`` when the pointer
falls back because the structured text is under half its partner's length
(:func:`workflows.acquire.point.too_short`, B9). Both verdicts stay on their snapshots
(``quality``), so the choice is visible per row too.

Counts: ``quality.measured``, ``quality.rejudged``, ``quality.<bucket>``, ``quality.flag.<check>``,
``quality.fail.<check>`` (over the verdicts made this run; ``quality.flag.article_in_press`` too),
``quality.blob_missing``, ``refresh.article_in_press`` (roles whose next due time this run set).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from stack.conversion import quality as q
from stack.conversion.ladder import stamp_tier
from stack.plan import Recheck, parse_duration
from stack.schemas.vocab import STRUCTURED_ROLES
from stack.stage_runner import StageError
from stack.store.blobs import BlobNotFound
from workflows.acquire.lanes import FETCH_LANES, LaneEnv
from workflows.acquire.point import candidates, needed, point, ranked, structured_of, too_short

Row = dict[str, Any]

#: ``converter`` of a collector's in-band text.
INBAND_CONVERTER = "inband"


class Converted(NamedTuple):
    """What ``convert`` learned about a text it made this run (keyed ``(doc_id, snapshot_id)``)."""

    converter: str | None
    pages: int | None
    truncated: bool | None


def gate_of(options: Mapping[str, Any] | None) -> q.Gate:
    """The run's gate from the stage line's options (module docstring)."""
    try:
        return q.gate_from_options(options)
    except ValueError as exc:
        raise StageError(f"acquire: {exc}") from exc


def _judged_text(snap: Mapping[str, Any], pin: str | None) -> bool:
    """``snap`` holds a text this substage judges at ``pin`` (module docstring, Scope)."""
    if not snap.get("text"):
        return False
    cfg = snap.get("convert_pin")
    if cfg is None:
        return snap["kind"] == "inband"
    if cfg != pin:
        return False
    status = snap.get("text_status")
    return status == "ok" or (status == "failed_permanent" and is_quality_class(snap.get("text_error_class")))


def is_quality_class(error_class: str | None) -> bool:
    return error_class in q.FAIL_CLASSES.values()


def current_text_snapshot(row: Mapping[str, Any], pin: str | None) -> Row | None:
    """The current candidate of ``row`` when it holds a text this substage judges, else ``None``."""
    c = row.get("content")
    if c is None:
        return None
    cands = candidates(c, row["identity"]["members"])
    if not cands or not _judged_text(cands[0], pin):
        return None
    return cands[0]


def judged_snapshots(row: Mapping[str, Any], pin: str | None, structured: frozenset[str] = frozenset()) -> list[Row]:
    """The texts of ``row`` this substage judges at ``pin``: the current candidate's
    (:func:`current_text_snapshot`), or with enabled ``structured`` roles every text of
    :func:`workflows.acquire.point.needed` (module docstring, Structured text); then the previous
    current snapshot when it holds the same text as one of those (module docstring, Stable supplier)."""
    if not structured:
        snap = current_text_snapshot(row, pin)
        return [] if snap is None else _with_previous(row, [snap], pin, structured)
    c = row.get("content")
    if c is None:
        return []
    return _with_previous(row, [s for s in needed(c, row["identity"]["members"], structured,
                                                  lambda s: _judged_text(s, pin)) if _judged_text(s, pin)],
                          pin, structured)


def _with_previous(row: Mapping[str, Any], snaps: list[Row], pin: str | None, structured: frozenset[str]) -> list[Row]:
    """``snaps`` plus the block's previous current snapshot when it is still ranked, holds a text judged
    here and that text is one of ``snaps``' (D807: the pointer may keep it, so it carries this gate's verdict)."""
    c = row["content"]
    prev_id = c.get("current_snapshot_id")
    texts = {(s.get("text") or {}).get("sha256") for s in snaps}
    if prev_id is None or not snaps or prev_id in {s["snapshot_id"] for s in snaps}:
        return snaps
    prev = next((s for s in ranked(c, row["identity"]["members"], structured, lambda s: _judged_text(s, pin))
                 if s["snapshot_id"] == prev_id), None)
    if prev is None or not _judged_text(prev, pin) or (prev.get("text") or {}).get("sha256") not in texts:
        return snaps
    return [*snaps, prev]


def judge_role(content_role: str) -> str:
    """The role whose thresholds judge a text: a structured representation is a paper body."""
    return "full_text" if content_role in STRUCTURED_ROLES else content_role


def needs_verdict(row: Mapping[str, Any], pin: str | None, gate_digest: str,
                  structured: frozenset[str] = frozenset()) -> bool:
    """A text of ``row`` this substage judges has no verdict of this checks version under this gate."""
    for snap in judged_snapshots(row, pin, structured):
        qual = snap.get("quality")
        if qual is None or qual["version"] != q.CHECKS_VERSION or qual["gate_digest"] != gate_digest:
            return True
    return False


def _facts(row: Mapping[str, Any], snap: Mapping[str, Any], fresh: Converted | None) -> tuple[q.Facts, str | None]:
    old = snap.get("quality") if fresh is None else None
    metrics = (old or {}).get("metrics") or {}
    if fresh is not None:
        converter, pages, truncated = fresh
    elif snap.get("convert_pin") is None:
        converter, pages, truncated = INBAND_CONVERTER, None, None
    else:
        converter, pages, truncated = (old or {}).get("converter"), metrics.get("pages"), metrics.get("truncated")
    facts = q.Facts(role=judge_role(snap["content_role"]), kind=q.kind_of(snap["raw"]["media_type"]),
                    raw_bytes=snap["raw"]["bytes"], pages=pages, truncated=truncated,
                    abstract=(row.get("metadata") or {}).get("abstract"))
    return facts, converter


def apply_verdict(snap: Row, verdict: q.Verdict, metrics: Mapping[str, Any], gate_digest: str,
                  converter: str | None) -> None:
    """Write ``verdict`` on ``snap`` and settle its ``text_status`` (module docstring)."""
    snap["quality"] = {"version": q.CHECKS_VERSION, "bucket": verdict.bucket, "flags": list(verdict.flags),
                       "fails": list(verdict.fails), "gate_digest": gate_digest, "converter": converter,
                       "tier": stamp_tier(converter), "metrics": {k: metrics.get(k) for k in q.METRIC_FIELDS}}
    if verdict.bucket == "failed":
        snap.update(text_status="failed_permanent", text_error_class=verdict.error_class)
    elif snap.get("text_status") != "ok":
        snap.update(text_status="ok", text_error_class=None)  # a quality failure the gate lifted


def run_quality(rows: Sequence[Row], env: LaneEnv, gate: q.Gate,
                converted: Mapping[tuple[str, str], Converted] | None = None) -> None:
    """Judge each row's current text in place, then keep an Article-in-Press text's role due for
    its refresh (module docstring). ``converted``: what ``convert`` reported for the texts it made
    in this run."""
    digest = gate.digest()
    converted = converted or {}
    interval = refresh_interval(env)
    roles = structured_of(env.options).roles
    for row in rows:
        snaps = judged_snapshots(row, env.convert_pin, roles)
        if not snaps:
            continue
        made = {s["snapshot_id"] for s in snaps
                if _judge(row, s, env, gate, digest, converted.get((row["doc_id"], s["snapshot_id"])))}
        # the snapshot the pointer keeps (D807: perhaps a same-text previous current) is the one refreshed
        cur_id = point(row["content"], row["identity"]["members"], env.convert_pin, roles)["current_snapshot_id"]
        if roles:
            compare(row, snaps, made, cur_id, env)
        cur = next((s for s in snaps if s["snapshot_id"] == cur_id), None)
        if interval is not None and cur is not None:
            arm_refresh(row, cur, env, interval)


def _ratio_bin(r: float) -> str:
    return next((name for top, name in ((0.5, "lt0.5"), (0.8, "0.5-0.8"), (1.25, "0.8-1.25"), (2.0, "1.25-2"))
                 if r < top), "ge2")


def compare(row: Mapping[str, Any], snaps: Sequence[Mapping[str, Any]], made: set[str], current_id: str | None,
            env: LaneEnv) -> None:
    """The structured-vs-partner comparison counts of one row (module docstring, The comparison)."""
    base = next((s for s in snaps if s["content_role"] not in STRUCTURED_ROLES), None)
    if base is None or base["content_role"] != "full_text" or base.get("quality") is None:
        return  # no PDF fallback in the prefix (e.g. the abstract is next): nothing to compare
    base_text: str | None = None
    for s in snaps:
        if (s["content_role"] not in STRUCTURED_ROLES or s.get("quality") is None
                or s["source_id"] != base["source_id"]
                or (s.get("revision") is not None and s.get("revision") != base.get("revision"))
                or not ({s["snapshot_id"], base["snapshot_id"]} & made)):
            continue
        pre = f"structured.{s['content_role']}"
        env.count(f"{pre}.compared")
        env.count(f"{pre}.bucket.{s['quality']['bucket']}")
        env.count(f"{pre}.base_bucket.{base['quality']['bucket']}")
        env.count(f"{pre}.{'current' if s['snapshot_id'] == current_id else 'fallback'}")
        if too_short(s, base):
            env.count(f"{pre}.too_short")
        sc, bc = (s["quality"]["metrics"] or {}).get("chars"), (base["quality"]["metrics"] or {}).get("chars")
        if sc and bc:
            env.count(f"{pre}.len_ratio.{_ratio_bin(sc / bc)}")
        try:
            text = env.blobs.get(s["text"]["sha256"]).decode("utf-8", errors="replace")
            if base_text is None:
                base_text = env.blobs.get(base["text"]["sha256"]).decode("utf-8", errors="replace")
        except BlobNotFound:
            env.count("quality.blob_missing")
            continue
        mine, theirs = q.structure_markers(text), q.structure_markers(base_text)
        for k in ("math", "tables"):
            env.count(f"{pre}.{k}.structured", mine[k])
            env.count(f"{pre}.{k}.base", theirs[k])


def _judge(row: Row, snap: Row, env: LaneEnv, gate: q.Gate, digest: str, fresh: Converted | None) -> bool:
    """One snapshot's verdict when it needs one (module docstring, When); ``True`` when one was made."""
    qual = snap.get("quality")
    urls = (snap.get("url"), snap.get("final_url"))
    role = judge_role(snap["content_role"])
    if fresh is None and qual is not None and qual["version"] == q.CHECKS_VERSION:
        if qual["gate_digest"] == digest:
            return False
        metrics = dict(qual["metrics"])
        verdict = q.judge(metrics, role, gate)
        if q.ARTICLE_IN_PRESS in (qual.get("flags") or []) or q.in_press(None, role, urls):
            verdict = q.with_in_press(verdict)
        apply_verdict(snap, verdict, metrics, digest, qual.get("converter"))
        env.count("quality.rejudged")
    else:
        try:
            text = env.blobs.get(snap["text"]["sha256"]).decode("utf-8", errors="replace")
        except BlobNotFound:
            env.count("quality.blob_missing")
            return False
        facts, converter = _facts(row, snap, fresh)
        metrics, verdict = q.assess(text, facts, gate)
        if q.in_press(text, role, urls):
            verdict = q.with_in_press(verdict)
        apply_verdict(snap, verdict, metrics, digest, converter)
        env.count("quality.measured")
    env.count(f"quality.{verdict.bucket}")
    for name in verdict.flags:
        env.count(f"quality.{'fail' if name in verdict.fails else 'flag'}.{name}")
    return True


def refresh_interval(env: LaneEnv) -> dt.timedelta | None:
    """``acquire.recheck.article_in_press`` of the run's plan (:class:`stack.plan.Recheck`'s
    default without one); ``None`` = no refresh."""
    recheck = getattr(env.acquire, "recheck", None) or Recheck()
    return parse_duration(recheck.article_in_press) if recheck.article_in_press is not None else None


def arm_refresh(row: Row, snap: Mapping[str, Any], env: LaneEnv, interval: dt.timedelta) -> None:
    """While ``snap`` (the row's current text) is flagged ``article_in_press``, the ``ok`` fetch
    role that holds it is due at ``env.now + interval`` at the latest (module docstring)."""
    if q.ARTICLE_IN_PRESS not in ((snap.get("quality") or {}).get("flags") or []):
        return
    due = env.now + interval
    roles = row["content"]["roles"]
    for i, role in enumerate(roles):
        if ((role.get("source_id"), role.get("content_role")) != (snap["source_id"], snap["content_role"])
                or role.get("lane") not in FETCH_LANES or role.get("status") != "ok"):
            continue
        ra = role.get("retry_after")
        if ra is None or ra > due:
            roles[i] = {**role, "retry_after": due}
            env.count("refresh.article_in_press")


def current_quality(row: Mapping[str, Any], current_snapshot_id: str | None, current_text_status: str) -> str | None:
    """``content.current_text_quality``: the current snapshot's bucket when its text is current
    (``ok``) or the gate failed it (``convert_failed`` with a ``quality_*`` class), else NULL."""
    if current_snapshot_id is None:
        return None
    snap = next((s for s in row["content"]["snapshots"] if s["snapshot_id"] == current_snapshot_id), None)
    qual = (snap or {}).get("quality")
    if qual is None:
        return None
    if current_text_status == "ok" or (current_text_status == "convert_failed"
                                       and is_quality_class(snap.get("text_error_class"))):
        return str(qual["bucket"])
    return None


__all__ = ["INBAND_CONVERTER", "Converted", "apply_verdict", "arm_refresh", "compare", "current_quality",
           "current_text_snapshot", "gate_of", "is_quality_class", "judge_role", "judged_snapshots", "needs_verdict",
           "refresh_interval", "run_quality"]
