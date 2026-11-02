"""Substage ``history`` (network; after ``inband``, before ``fetch``): which version of an arXiv
paper is current (§5.6 "v2 = new raw snapshot, ``revision = 'v2'``, pointer moves").

The step notices when arXiv's default (latest) version differs from the one we hold; it does not
list earlier versions. Its only provider question is the arXiv lane's batched Atom ``id_list``
sweep (:meth:`ArxivLane.history <workflows.acquire.lanes.arxiv.ArxivLane.history>`: one request per
:data:`stack.arxiv.ATOM_BATCH` ids, no per-paper request), whose answer fills ``content.revisions``
through :func:`atom_revisions` and :func:`merge_revisions`. Until 2026-10-13 a paper lacking its
current version also got v3's OAI-PMH ``arXivRaw`` ``GetRecord`` (every version with its date, the
title, the ``source_type = 'I'`` withdrawal flag that arXiv no longer serves, T3.6): one request per
paper through arXiv's 3.1 s gate, ≥ 34 min for 650 new ids (the std-v3 rehearsal), so it went, with v3's unused
``zenodo_history`` port and the ``request_over`` adapter that served both. Withdrawal is
``gather``'s (``metadata.is_withdrawn``, a member's ``withdrawn_at``); the Atom entry carries no
reliable withdrawal marker (the lane's module docstring says what happens to a withdrawn paper).

**Seam** (D106): ``run_history(rows, env) -> rows`` over the in-scope working rows (with
``max_rows``, only the rows the fetch can reach, in rounds: ``main._history``, D410) and the run's
:class:`~workflows.acquire.lanes.LaneEnv`. A lane owns its network, so history is a per-lane hook:
each fetch lane whose ``LANE`` has a ``history(rows, env)`` method gets the rows in
:data:`~workflows.acquire.lanes.FETCH_LANES` order (``arxiv``: :meth:`ArxivLane.history
<workflows.acquire.lanes.arxiv.ArxivLane.history>`). A lane registered without the hook (a test
fake) means no history network for its rows. A hook may change ``content.revisions`` and make a
role of its own lane due (``retry_after := now``) when the current revision is not held; nothing
else. ``content.current_revision`` is the pointer's (:mod:`.point`: the current snapshot's
``revision``), so it moves only when the lane has minted the new revision. Skipped under
``--only`` unless named (not pure): ``--only convert`` makes no history call.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from stack.arxiv import AtomVersion
from workflows.acquire import lanes
from workflows.acquire.lanes import LaneEnv

Row = dict[str, Any]


# ---------------------------------------------------------------- REVISION lists
def revision_number(revision: str | None) -> int:
    """``'v12'`` → 12; anything else sorts first (0)."""
    s = str(revision or '')
    return int(s[1:]) if s[:1] == 'v' and s[1:].isdigit() else 0


def merge_revisions(prior: Iterable[Mapping[str, Any]] | None,
                    new: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``REVISION`` dicts keyed by ``revision``: a new entry's non-NULL fields win, a prior entry
    missing from ``new`` stays (a listing never forgets a version); sorted by version number."""
    out: dict[str, dict[str, Any]] = {}
    for rev in prior or []:
        out[rev['revision']] = {'revision': rev['revision'], 'published_at': rev.get('published_at'),
                                'title': rev.get('title')}
    for rev in new:
        cur = out.setdefault(rev['revision'], {'revision': rev['revision'], 'published_at': None, 'title': None})
        for key in ('published_at', 'title'):
            if rev.get(key) is not None:
                cur[key] = rev[key]
    return sorted(out.values(), key=lambda r: (revision_number(r['revision']), r['revision']))


def atom_revisions(atom: AtomVersion) -> list[dict[str, Any]]:
    """``REVISION`` dicts of one Atom ``id_list`` answer: the current ``vN`` dated by the entry's
    ``<updated>`` and, when N > 1, ``v1`` dated by its ``<published>`` (the versions between are
    not in the answer); dates are aware UTC midnights (``None`` when the feed gave none), ``title``
    ``None``."""
    def day(iso: str) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.UTC) if iso else None
        except ValueError:
            return None

    out = [{'revision': f'v{atom.version}', 'published_at': day(atom.updated), 'title': None}]
    if atom.version > 1:
        out.insert(0, {'revision': 'v1', 'published_at': day(atom.published), 'title': None})
    return out


# ---------------------------------------------------------------- the substage
def run_history(rows: list[Row], env: LaneEnv) -> list[Row]:
    """Each fetch lane's ``history`` hook over the working rows (module docstring)."""
    for name in lanes.FETCH_LANES:
        hook = getattr(lanes.get_lane(name), 'history', None)
        if callable(hook):
            rows = hook(rows, env)
    return rows


__all__ = ["atom_revisions", "merge_revisions", "revision_number", "run_history"]
