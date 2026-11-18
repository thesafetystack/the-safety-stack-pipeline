"""Substage ``carry`` (pure; D6, X21, §6 retry state): bring a row's ``content`` onto its current
members before anything else runs.

* **Regroup by member.** Entries are keyed by ``source_id``: snapshots and roles of sources that
  are no longer members of the row are dropped (they belong to the document that now holds the
  source). On a stale row (``content`` NULL or ``members_digest`` ≠ ``identity.members_digest``)
  the retired rows named in ``identity.former_doc_ids`` also contribute their entries for the
  row's members: a split hands each part its members' entries; a merge unions them (``gather``
  already carried the absorbed row's ``content`` onto the survivor, D6, so this is idempotent).
  Snapshots union by ``snapshot_id``; roles union keyed ``(source_id, content_role)`` without
  summing attempts (:func:`merge_roles`: ``ok`` wins, else most attempts; earliest
  ``first_attempt_at``, latest ``last_attempt_at``, ``tried_urls`` unioned — the v4e mock's R13).
* **Wanted roles.** For each live member, every role of ``acquire.wants[<channel>]`` (D11)
  without an entry and without an in-band entry gets one: ``skipped[gated]`` when triage does not
  let the row fetch, ``skipped[withdrawn]`` for a withdrawn member, else
  ``not_attempted[limit]`` (not yet reached by a fetch; S4 requires a class on
  ``not_attempted``). ``lane`` per :func:`~workflows.acquire.lanes.lane_for`. A live member of a
  row triage lets fetch whose channel has **no** ``wants`` key and no in-band entry gets no role
  and would end ``no_raw`` silently; it is counted ``carry.no_wants`` (one total per run; T3.6
  found dev-v2's ``wants`` keyed by non-channels).
  ``lane`` is chosen with the member's key (an arXiv-keyed ``paper`` member → ``arxiv``,
  T3.6-f3a); an existing role of an arXiv-keyed ``paper`` member still on ``oa`` that has made no
  progress (``not_attempted``, ``skipped[gated]``, ``skipped[not_needed]``) is re-laned to
  ``arxiv`` (``carry.relaned``); a role with progress keeps its lane.
* **arXiv HTML for every arXiv id** (R6 item 2; ``arxiv_html``, the acquire line's
  ``options.structured.arxiv_html``). ``wants`` is per channel, so a paper found only through
  OpenAlex or S2 (member key ``arxiv:<id>`` or DOI ``10.48550/arxiv.<id>``,
  :func:`~workflows.acquire.lanes.arxiv_key`) would get its PDF through the arXiv lane but never
  LaTeXML text. With ``arxiv_html`` on, every live member whose key carries an arXiv id and whose
  role would be on lane ``arxiv`` wants ``html`` too, whichever channel found it (same gate and
  withdrawal rules as above; ``carry.arxiv_html_added`` counts such roles not listed in the
  channel's ``wants``). The one-fetch rule below then keeps one ``html`` fetch per arXiv id
  (``arxiv`` channel first). Off: as before. **An arXiv id held only in ``identity.keys``** (R6
  item 2b: a key adder's key, e.g. OpenAlex's arXiv location, on a member joined by a publisher DOI;
  :func:`~workflows.acquire.lanes.keys_arxiv_ids`) and in no live member's key: one live ``paper``
  member that holds it (``arxiv`` channel first, then channel id, ``source_id``) wants ``html`` on
  lane ``arxiv`` (``carry.arxiv_html_from_keys``), so it is one role per id, never a second one
  beside a member key's; the request builder passes the id to the lane
  (:data:`~workflows.acquire.lanes.KEY_ARXIV_ID`). The one-fetch rule groups such a role under
  ``arxiv:<id>`` too, so a member key that later carries the id still yields one fetch. Its
  ``full_text`` is untouched (the publisher DOI's, on ``oa``). An id only in metadata gets nothing.
* **arXiv PDF** (D480; the acquire line's ``options.arxiv_pdf``, :func:`arxiv_pdf_of`: ``always``,
  the default and the behaviour before the option, ``on_miss`` or ``never``; ``on_miss`` and ``never``
  need ``options.structured.arxiv_html: true``). It governs every ``full_text`` role on lane
  ``arxiv`` of a live member, whichever channel found the paper (an OpenAlex or S2 member keyed by an
  arXiv id or an arXiv DOI too); it adds no role ``wants`` does not list. Applied on a row triage lets
  fetch, after the gate moves and before the one-fetch rule below. Such a role is **requested** under
  ``always``; never under ``never``; under ``on_miss`` exactly when its arXiv id's HTML is a **miss**
  (:func:`html_verdict`): no live ``html`` role on lane ``arxiv`` holds the id (``no_html_role``);
  every such role (``skipped[not_needed]`` ones aside) ended ``empty`` (arXiv answered 404/410 or an
  unacceptable page; ``html_empty``), ``failed_permanent`` or ``quarantined``
  (``html_failed_permanent``, ``html_quarantined``; several: ``html_failed``), even when an older
  revision's text is held; else the newest held ``html`` snapshot of those roles' sources has a
  failed text at the pin: a failed conversion (``convert_failed``) or the quality gate's verdict
  (``quality``); else, with no text held, one of the roles is skipped for a reason other than
  ``withdrawn`` or ``gated`` (``html_skipped``). Not a miss: an ``ok`` text at the pin (the HTML is
  the text); a ``withdrawn`` HTML role (the PDF of a withdrawal version is withdrawn too); anything
  still pending (not fetched yet,
  ``failed_transient`` before the cap, not converted at the pin yet, a ``failed_transient`` conversion
  that convert retries). ``too_short`` (B9) plays no
  part: it compares against a held PDF of the same revision, so it can only move the pointer to a PDF
  already held, never make one due. A role not requested becomes ``skipped[not_requested]`` with
  ``rule = 'arxiv_pdf'`` (D690) and error ``arxiv_pdf: <mode>[, <why>]`` whatever its status was (``ok`` and failed ones included;
  roles skipped for another reason are left alone), its other fields kept; held snapshots are not
  touched, so the pointer still sees a PDF fetched earlier. **Except** an ``ok`` role while its id's
  HTML is ``pending`` (D643, amends D480): it is left as it is (``arxiv_pdf.kept_pending``) and set
  aside only once the HTML says ``ok`` or ``withdrawn``. A merge that brings in a member with a
  not-yet-fetched ``html`` role makes a settled id ``pending`` again (VERIFY-2A); setting the fetched
  PDF aside then changed the row's primary role after triage had read it, for nothing: a miss would
  request the PDF again. A role this rule skipped (``skipped[not_requested]`` with its ``rule``) that is
  requested again becomes ``not_attempted[limit]`` (``rule`` NULL) (due; the lane answers a
  held revision without a PDF request). Counts ``arxiv_pdf.not_requested``, ``arxiv_pdf.requested``
  and, under ``on_miss``, ``arxiv_pdf.miss.<why>`` per role so requested. **When**: the rule reads the
  row as ``carry`` finds it, so an HTML that this run's fetch, convert or quality finds missing is
  decided by acquire's second pass of this rule after ``quality`` (:mod:`.main`): the PDF becomes due
  there and is fetched by the next run (the run ends scope-incomplete, ``arxiv_pdf.due_next_run``).
* **Gate moves.** A fetch role ``skipped[gated]`` on a row triage now lets fetch becomes
  ``not_attempted[limit]`` (due); a ``not_attempted`` role on a row it no longer lets fetch
  becomes ``skipped[gated]``. Other statuses are progress and stay.
* **One fetch per paper identifier** (T3.6-f3a, amends D152 for same-identifier duplicates;
  only on a row triage lets fetch, after the gate moves). A paper usually has several members
  carrying the same identifier (arXiv, OpenAlex, Semantic Scholar on one DOI); fetching each is
  the same retrieval again. The fetch roles (lane in ``FETCH_LANES``) of live members are grouped
  by ``(content_role, fetch key)`` (:func:`fetch_key`: ``arxiv:<id>``, ``doi:<lowercased>``,
  ``pmid:<value>``; other keys are never grouped) and ordered: lane ``arxiv``, ``oa``, ``http``;
  then channel ``arxiv`` first, channel id, ``source_id``. The first role in that order that is
  not terminal (``failed_permanent``, ``quarantined``) **and whose lane has not ended terminal on
  the identifier** (T3.x-f4: another member's role in the same lane would repeat the same
  retrieval; T3.6's continuation re-opened five such OA roles for 207 requests, and a dead arXiv
  id was retried once per member, D165b's residue; ``carry.same_key_lane_terminal``) is the
  group's fetch role, **unless the identifier is already held** (D807): in an ``arxiv:<id>`` group, an
  ``ok`` role on lane ``arxiv`` whose source holds a raw snapshot of that role at the version the lane
  would fetch (:func:`held_lead`) is the fetch role, the first such in the order above
  (``carry.same_key_held`` when that is not the role the order picks). So a member a merge adds that sorts
  first (the ``arxiv`` channel's beside an OpenAlex member's) no longer fetches the same version again,
  minting a second snapshot of the same text. The fetch key carries no version (``vN`` is stripped) and
  neither does the member's key reliably; the lane fetches the current version whichever member asks.
  That version is the newest revision in ``content.revisions`` when the history listed one; a newer one
  listed but not held (the history's answer) leaves the order to decide, as before. With nothing listed
  (an arXiv paper read through its HTML only, ``arxiv_pdf: on_miss``, never gets a listing) the held
  revision stands for it: the held role's own recheck (the weekly ``retry_after`` of an ``ok`` arXiv
  role, D22; due at most cycles) asks arXiv for the current version and fetches a newer one once, so the
  newcomer's fetch would only repeat it; a newer version then waits for that recheck, at most one recheck
  period. The recheck itself is not changed, and a due held role is still the lead (it is called anyway).
  The held role's member no longer live: as before. When there is no fetch role, the group fetches
  nothing; every other
  ``not_attempted`` role of the group becomes ``skipped[not_needed]`` ("same <key> as
  <source_id> (<channel>)"; never due, never charged; ``carry.same_key_skipped``). A
  ``skipped[not_needed]`` role that is no longer covered by another role (it is now its group's
  fetch role, e.g. the one before it ended dead, or its group shrank) becomes
  ``not_attempted[limit]`` (``carry.same_key_reopened``). Roles with progress (``ok``,
  ``failed_transient``, …) are never changed. A terminal role is not the end of its lane on the
  identifier for good: it is rechecked itself 90 days after it ended
  (:data:`stack.fetchcycle.TERMINAL_RECHECK`, R4-2) or when ``stack acquire reopen`` makes it due,
  and the same-key members of that lane wait on it. An ``xml`` role keyed ``arxiv:<id>`` is not grouped:
  ``not_attempted`` → ``skipped[not_needed]`` ("arXiv serves no JATS XML",
  ``carry.arxiv_xml_skipped``). On a row triage does not let fetch nothing here changes.
* ``members_digest := identity.members_digest`` (X21). ``same_raw_doc_ids`` is recomputed over
  the whole view at ``point`` (C29/X25), not here.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

from stack.schemas.vocab import RECORD_ROLES
from workflows.acquire.history import revision_number
from workflows.acquire.lanes import FETCH_LANES, INBAND, arxiv_key, keys_arxiv_ids, lane_for
from workflows.acquire.point import structured_of, text_of
from workflows.acquire.scope import FETCH, is_stale

Row = dict[str, Any]
_FAR = dt.datetime.max.replace(tzinfo=dt.UTC)
RoleKey = tuple[str | None, str]
#: Terminal role statuses: a group's fetch role is the first role not in these.
TERMINAL = frozenset({"failed_permanent", "quarantined"})
_LANE_RANK = {name: n for n, name in enumerate(FETCH_LANES)}  # arxiv, oa, http
_UNPROGRESSED_SKIPS = frozenset({"gated", "not_needed"})
ARXIV_XML_ERROR = "arXiv serves no JATS XML"
#: The arXiv LaTeXML role (``workflows.acquire.lanes.arxiv.HTML_ROLE``; not imported: carry stays pure).
HTML_ROLE = "html"
#: ``options.arxiv_pdf`` values (module docstring, arXiv PDF); the first is the default (absent or null).
ARXIV_PDF_MODES: tuple[str, ...] = ("always", "on_miss", "never")
#: The ``rule`` of a ``full_text`` role the arXiv PDF rule skipped (and may reopen; D690), and its error's prefix.
ARXIV_PDF_RULE = "arxiv_pdf"
ARXIV_PDF_ERROR = "arxiv_pdf:"
_HTML_SETTLED_MISS = frozenset({"empty", "failed_permanent", "quarantined"})


def new_content(members_digest: str, pin: str | None) -> Row:
    """An empty ``content`` block (the row had none)."""
    return {"lane": None, "convert_pin": pin, "snapshots": [], "roles": [],
            "current_snapshot_id": None, "current_source_id": None, "current_role": None,
            "current_text_sha256": None, "current_text_status": "no_raw", "current_text_media": None,
            "current_text_quality": None, "current_revision": None, "revisions": [],
            "members_digest": members_digest, "same_raw_doc_ids": [], "run_id": "", "updated_at": None}


def new_role(content_role: str, source_id: str | None, *, status: str, error_class: str | None,
             lane: str | None, error: str | None = None) -> Row:
    return {"content_role": content_role, "source_id": source_id, "status": status,
            "error_class": error_class, "error": error, "attempts": 0, "first_attempt_at": None,
            "last_attempt_at": None, "last_outcome": None, "retry_after": None, "tried_urls": [],
            "last_source_id": None, "etag": None, "last_modified": None, "validator_url": None,
            "snapshot_id": None, "lane": lane, "rule": None}


def role_key(role: Mapping[str, Any]) -> RoleKey:
    return role.get("source_id"), role["content_role"]


def merge_roles(states: list[Row]) -> Row:
    """One role from several states of the same key (no summing of attempts)."""
    if len(states) == 1:
        return states[0]
    pool = [s for s in states if s["status"] == "ok"] or states

    def rank(s: Row) -> tuple[int, dt.datetime]:
        return (-int(s.get("attempts") or 0), s.get("first_attempt_at") or _FAR)

    base = copy.deepcopy(min(pool, key=rank))
    firsts = [s["first_attempt_at"] for s in pool if s.get("first_attempt_at")]
    lasts = [s["last_attempt_at"] for s in pool if s.get("last_attempt_at")]
    base["first_attempt_at"] = min(firsts) if firsts else None
    base["last_attempt_at"] = max(lasts) if lasts else None
    base["tried_urls"] = list(dict.fromkeys(u for s in pool for u in s.get("tried_urls") or []))
    return base


def fetch_key(member: Mapping[str, Any]) -> str | None:
    """The identifier a member's fetch roles are grouped by (module docstring): ``arxiv:<id>``
    for an ``arxiv`` key or an arXiv DOI (``vN`` stripped), ``doi:<lowercased>`` for another DOI,
    ``pmid:<value>`` for a PMID, else ``None`` (urls, ``fm_post``, OpenAlex/S2 ids: never
    grouped). Only the member's key is read, not its ``canonical_url``."""
    kind = str(member.get("key_kind") or "").lower()
    value = str(member.get("key_value") or "").strip()
    aid = arxiv_key(kind, value)
    if aid:
        return f"arxiv:{aid}"
    if kind == "doi":
        doi = value.lower().removeprefix("https://doi.org/").removeprefix("doi:")
        return f"doi:{doi}" if doi else None
    if kind == "pmid" and value:
        return f"pmid:{value}"
    return None


def held_lead(keys: list[RoleKey], roles: Mapping[RoleKey, Row], snapshots: list[Row],
              revisions: list[Row] | None) -> RoleKey | None:
    """The first of ``keys`` (one ``arxiv:<id>`` group, in fetch order) that already holds what the arXiv
    lane would fetch (module docstring, One fetch per paper identifier, D807): ``ok`` on lane ``arxiv``,
    its source holding a raw snapshot of its role at the newest revision of ``revisions``, or at any
    revision when nothing is listed."""
    listed = [str(r["revision"]) for r in revisions or [] if r.get("revision")]
    newest = max(listed, key=lambda v: (revision_number(v), v)) if listed else None
    for k in keys:
        r = roles[k]
        if r["status"] == "ok" and r.get("lane") == "arxiv" and any(
                s["kind"] == "raw" and s["source_id"] == k[0] and s["content_role"] == r["content_role"]
                and s.get("revision") and (newest is None or s["revision"] == newest) for s in snapshots):
            return k
    return None


def _not_needed(r: Row, error: str) -> Row:
    return {**r, "status": "skipped", "error_class": "not_needed", "error": error, "retry_after": None}


def _one_fetch_per_key(roles: dict[RoleKey, Row], live: Mapping[str, Row],
                       counts: dict[str, int] | None, key_ids: Mapping[str, str] | None = None,
                       snapshots: list[Row] | None = None, revisions: list[Row] | None = None) -> None:
    """The same-identifier rule (module docstring), in place on ``roles``. ``key_ids``
    (:func:`~workflows.acquire.lanes.keys_arxiv_ids`): an ``html`` role on lane ``arxiv`` whose
    member's key names no arXiv id groups under the id ``identity.keys`` gives its source.
    ``snapshots`` and ``revisions`` (the block's): an arXiv id already held keeps its fetch role (D807)."""
    def bump(name: str) -> None:
        if counts is not None:
            counts[name] = counts.get(name, 0) + 1

    groups: dict[tuple[str, str], list[RoleKey]] = {}
    covered: set[RoleKey] = set()  # roles another role of their group stands for
    for key, r in roles.items():
        m = live.get(r.get("source_id"))
        if m is None or r.get("lane") not in FETCH_LANES:
            continue
        fk = fetch_key(m)
        if (r["content_role"] == HTML_ROLE and r.get("lane") == "arxiv" and not (fk or "").startswith("arxiv:")
                and key_ids and r["source_id"] in key_ids):
            fk = f"arxiv:{key_ids[r['source_id']]}"
        if fk is None:
            continue
        if r["content_role"] == "xml" and fk.startswith("arxiv:"):
            covered.add(key)
            if r["status"] == "not_attempted":
                roles[key] = _not_needed(r, ARXIV_XML_ERROR)
                bump("carry.arxiv_xml_skipped")
            continue
        groups.setdefault((r["content_role"], fk), []).append(key)
    for (_role, fk), keys in groups.items():
        def order(k: RoleKey) -> tuple[int, bool, str, str]:
            m = live[roles[k]["source_id"]]
            return (_LANE_RANK[roles[k]["lane"]], m["channel"] != "arxiv", m["channel"], k[0] or "")

        keys.sort(key=order)
        # A lane that already ended terminal on this identifier would only repeat itself (T3.x-f4).
        dead = {roles[k]["lane"]: k for k in reversed(keys) if roles[k]["status"] in TERMINAL}
        lead = next((k for k in keys if roles[k]["status"] not in TERMINAL and roles[k]["lane"] not in dead), None)
        held = held_lead(keys, roles, snapshots or [], revisions) if fk.startswith("arxiv:") else None
        if held is not None and held != lead:  # D807: the held version is not fetched again
            lead = held
            bump("carry.same_key_held")
        if lead is None and not dead:
            continue
        for k in keys:
            r = roles[k]
            if k == lead:
                continue
            covered.add(k)
            if r["status"] == "not_attempted" or (r["status"] == "skipped" and r["error_class"] == "not_needed"):
                if lead is not None:
                    error = f"same {fk} as {lead[0]} ({live[roles[lead]['source_id']]['channel']})"
                else:
                    d = roles[dead[r["lane"]]] if r["lane"] in dead else roles[next(iter(dead.values()))]
                    error = (f"same {fk} as {d['source_id']} ({live[d['source_id']]['channel']}), which ended "
                             f"{d['status']}[{d['error_class']}] in lane {d['lane']}")
                if r["status"] == "not_attempted":
                    bump("carry.same_key_skipped")
                if r["lane"] in dead:
                    bump("carry.same_key_lane_terminal")
                roles[k] = _not_needed(r, error)
    for key, r in roles.items():
        if (key not in covered and r["source_id"] in live and r.get("lane") in FETCH_LANES
                and r["status"] == "skipped" and r["error_class"] == "not_needed"):
            roles[key] = {**r, "status": "not_attempted", "error_class": "limit", "error": "not yet fetched"}
            bump("carry.same_key_reopened")


def _html_from_keys(members: list[Row], key_ids: Mapping[str, str],
                    channel_group: Callable[[str], str | None]) -> dict[str, str]:
    """``source_id`` → arXiv id for the members that want ``html`` through ``identity.keys`` (module
    docstring, item 2b): per id named by no live member's key, the first live ``paper`` member
    (``arxiv`` channel first, channel id, ``source_id``) whose source holds it."""
    if not key_ids:
        return {}
    live = [m for m in members if m["is_live"]]
    held = {arxiv_key(m.get("key_kind"), m.get("key_value")) for m in live}
    out: dict[str, str] = {}
    for m in sorted(live, key=lambda m: (m["channel"] != "arxiv", m["channel"], m["source_id"])):
        aid = key_ids.get(m["source_id"])
        if (aid and aid not in held and aid not in out.values() and channel_group(m["channel"]) == "paper"
                and not arxiv_key(m.get("key_kind"), m.get("key_value"))):
            out[m["source_id"]] = aid
    return out


def arxiv_pdf_of(options: Mapping[str, Any] | None) -> str:
    """``options.arxiv_pdf`` of the acquire line (module docstring, arXiv PDF): one of
    :data:`ARXIV_PDF_MODES`, absent or null = ``always``. A value outside them, or ``on_miss``/``never``
    without ``options.structured.arxiv_html: true``, is a ``ValueError`` (``acquire`` reports it as a
    ``StageError`` before any substage, and a build refuses it at its start)."""
    raw = (options or {}).get("arxiv_pdf")
    mode = ARXIV_PDF_MODES[0] if raw is None else raw
    if not isinstance(mode, str) or mode not in ARXIV_PDF_MODES:
        raise ValueError(f"options.arxiv_pdf must be one of {list(ARXIV_PDF_MODES)}, got {raw!r}")
    if mode != "always" and not structured_of(options).arxiv_html:
        raise ValueError(f"options.arxiv_pdf: {mode} needs options.structured.arxiv_html: true (without the "
                         f"arXiv HTML a paper found through arXiv would get no text but its abstract)")
    return mode


def html_verdict(aid: str, roles: Mapping[RoleKey, Row], live: Mapping[str, Row], snapshots: list[Row],
                 pin: str | None, key_ids: Mapping[str, str]) -> str:
    """What the arXiv HTML of ``aid`` says about its PDF under ``on_miss`` (module docstring, arXiv
    PDF): ``ok``, ``pending``, ``withdrawn`` or ``miss:<why>``. ``key_ids``:
    :func:`~workflows.acquire.lanes.keys_arxiv_ids` (an ``html`` role whose member's key names no arXiv
    id stands for the id its source holds in ``identity.keys``)."""
    def aid_of(r: Row) -> str:
        m = live[r["source_id"]]
        return arxiv_key(m.get("key_kind"), m.get("key_value")) or key_ids.get(r["source_id"], "")

    html = [r for r in roles.values()
            if r["content_role"] == HTML_ROLE and r.get("lane") == "arxiv" and r.get("source_id") in live
            and not (r["status"] == "skipped" and r["error_class"] == "not_needed") and aid_of(r) == aid]
    if not html:
        return "miss:no_html_role"
    if any(r["status"] == "skipped" and r["error_class"] == "withdrawn" for r in html):
        return "withdrawn"
    if all(r["status"] in _HTML_SETTLED_MISS for r in html):
        return f"miss:html_{html[0]['status']}" if len({r["status"] for r in html}) == 1 else "miss:html_failed"
    sources = {r["source_id"] for r in html}
    held = [s for s in snapshots if s["kind"] == "raw" and s["content_role"] == HTML_ROLE and s["source_id"] in sources]
    if held:
        newest = max(held, key=lambda s: (s["captured_at"], s["snapshot_id"]))
        status = text_of(newest, pin)[1]
        if status == "ok":
            return "ok"
        if status == "convert_failed" and newest.get("text_status") != "failed_transient":  # convert retries those
            return ("miss:quality" if str(newest.get("text_error_class") or "").startswith("quality_")
                    else "miss:convert_failed")
    if any(r["status"] == "skipped" and r["error_class"] != "gated" for r in html):
        return "miss:html_skipped"
    return "pending"


def _arxiv_pdf(roles: dict[RoleKey, Row], live: Mapping[str, Row], snapshots: list[Row], *, mode: str,
               pin: str | None, key_ids: Mapping[str, str], counts: dict[str, int] | None) -> None:
    """The arXiv PDF rule (module docstring, arXiv PDF), in place on ``roles`` of a row triage lets fetch."""
    def bump(name: str) -> None:
        if counts is not None:
            counts[name] = counts.get(name, 0) + 1

    verdicts: dict[str, str] = {}
    for key, r in roles.items():
        m = live.get(r.get("source_id"))
        if m is None or r["content_role"] != "full_text" or r.get("lane") != "arxiv":
            continue
        ours = r["status"] == "skipped" and r["error_class"] == "not_requested" and r.get("rule") == ARXIV_PDF_RULE
        if r["status"] == "skipped" and not ours:
            continue  # withdrawn, gated, not_needed (the one-fetch rule's), …: not this rule's
        if mode == "always":
            verdict = "always"
        elif mode == "never":
            verdict = "never"
        else:
            aid = arxiv_key(m.get("key_kind"), m.get("key_value"))
            if aid not in verdicts:
                verdicts[aid] = html_verdict(aid, roles, live, snapshots, pin, key_ids)
            verdict = verdicts[aid]
        if verdict == "always" or verdict.startswith("miss:"):
            if ours:
                roles[key] = {**r, "status": "not_attempted", "error_class": "limit", "error": "not yet fetched",
                              "retry_after": None, "rule": None}
                bump("arxiv_pdf.requested")
                if verdict != "always":
                    bump(f"arxiv_pdf.{verdict.replace(':', '.')}")
            continue
        if verdict == "pending" and r["status"] == "ok":
            bump("arxiv_pdf.kept_pending")
            continue  # a fetched PDF stays the role's answer until the HTML is settled (D643)
        why = {"never": "never", "ok": "on_miss, the arXiv HTML is the text",
               "pending": "on_miss, waiting for the arXiv HTML",
               "withdrawn": "on_miss, the arXiv version is withdrawn"}[verdict]
        error = f"{ARXIV_PDF_ERROR} {why}"
        if not ours:
            bump("arxiv_pdf.not_requested")
        if not ours or r.get("error") != error:
            roles[key] = {**r, "status": "skipped", "error_class": "not_requested", "error": error,
                          "retry_after": None, "rule": ARXIV_PDF_RULE}


def carry(row: Row, by_id: Mapping[str, Row], *, wants: Mapping[str, list[str]],
          channel_group: Callable[[str], str | None], permit: str, pin: str | None,
          counts: dict[str, int] | None = None, arxiv_html: bool = False, arxiv_pdf: str = "always") -> Row:
    """The row's regrouped ``content`` (a new dict; ``row`` is not modified); ``carry.no_wants``,
    ``carry.relaned``, ``carry.same_key_skipped``, ``carry.same_key_reopened``,
    ``carry.arxiv_xml_skipped`` and ``carry.arxiv_html_added`` go to ``counts`` when given (module
    docstring). ``arxiv_html``: every arXiv-keyed member wants ``html`` (module docstring);
    ``arxiv_pdf``: the arXiv PDF rule's mode (:func:`arxiv_pdf_of`, module docstring)."""
    ident = row["identity"]
    keep = set(ident["member_ids"])
    sources: list[Row] = []
    if row.get("content") is not None:
        sources.append(row["content"])
    if is_stale(row):
        for fid in ident["former_doc_ids"] or []:
            old = by_id.get(fid)
            if old is not None and old["state"] == "retired" and old.get("content") is not None:
                sources.append(old["content"])
    content = copy.deepcopy(sources[0]) if sources else new_content(ident["members_digest"], pin)

    snaps: dict[str, Row] = {}
    for c in sources:
        for s in c["snapshots"] or []:
            if s["source_id"] in keep and s["snapshot_id"] not in snaps:
                snaps[s["snapshot_id"]] = copy.deepcopy(s)
    states: dict[RoleKey, list[Row]] = {}
    for c in sources:
        for r in c["roles"] or []:
            if r.get("source_id") is None or r["source_id"] in keep:
                states.setdefault(role_key(r), []).append(copy.deepcopy(r))
    roles = {k: merge_roles(v) for k, v in states.items()}

    inband_keys = {(e["source_id"], e["content_role"]) for e in row["inband"]["entries"] or []}
    inband_sources = {e["source_id"] for e in row["inband"]["entries"] or []
                      if e["content_role"] not in RECORD_ROLES}  # D522: a record is not content
    key_ids = keys_arxiv_ids(ident) if arxiv_html else {}
    from_keys = _html_from_keys(ident["members"], key_ids, channel_group)
    for m in ident["members"]:
        if not m["is_live"]:
            continue
        if (counts is not None and permit == FETCH and m["channel"] not in wants
                and m["source_id"] not in inband_sources):
            counts["carry.no_wants"] = counts.get("carry.no_wants", 0) + 1
        group = channel_group(m["channel"])
        wanted = list(wants.get(m["channel"], []))
        added_html = (arxiv_html and HTML_ROLE not in wanted and arxiv_key(m.get("key_kind"), m.get("key_value"))
                      and lane_for(m["channel"], group, HTML_ROLE, key_kind=m.get("key_kind"),
                                   key_value=m.get("key_value")) == "arxiv")
        keyed = not added_html and HTML_ROLE not in wanted and m["source_id"] in from_keys
        if added_html or keyed:
            wanted.append(HTML_ROLE)
        for want in wanted:
            key = (m["source_id"], want)
            if key in roles or key in inband_keys:
                continue
            if want == HTML_ROLE and (added_html or keyed) and counts is not None:
                counts["carry.arxiv_html_added"] = counts.get("carry.arxiv_html_added", 0) + 1
                if keyed:
                    counts["carry.arxiv_html_from_keys"] = counts.get("carry.arxiv_html_from_keys", 0) + 1
            lane = ("arxiv" if want == HTML_ROLE and keyed else
                    lane_for(m["channel"], group, want, key_kind=m.get("key_kind"), key_value=m.get("key_value")))
            if permit != FETCH:
                roles[key] = new_role(want, m["source_id"], status="skipped", error_class="gated",
                                      lane=lane, error=f"triage: {permit}")
            elif m.get("withdrawn_at") is not None:
                roles[key] = new_role(want, m["source_id"], status="skipped", error_class="withdrawn",
                                      lane=lane, error=m.get("withdrawn_reason"))
            else:
                roles[key] = new_role(want, m["source_id"], status="not_attempted", error_class="limit",
                                      lane=lane, error="not yet fetched")
    live = {m["source_id"]: m for m in ident["members"] if m["is_live"]}
    for key, r in roles.items():
        m = live.get(r.get("source_id"))
        if (m is not None and r.get("lane") == "oa" and arxiv_key(m.get("key_kind"), m.get("key_value"))
                and channel_group(m["channel"]) == "paper"
                and (r["status"] == "not_attempted"
                     or (r["status"] == "skipped" and r["error_class"] in _UNPROGRESSED_SKIPS))):
            roles[key] = {**r, "lane": "arxiv"}
            if counts is not None:
                counts["carry.relaned"] = counts.get("carry.relaned", 0) + 1
    for key, r in roles.items():
        if r.get("lane") == INBAND:
            continue
        if permit == FETCH and r["status"] == "skipped" and r["error_class"] == "gated":
            roles[key] = {**r, "status": "not_attempted", "error_class": "limit", "error": "not yet fetched"}
        elif permit != FETCH and r["status"] == "not_attempted":
            roles[key] = {**r, "status": "skipped", "error_class": "gated", "error": f"triage: {permit}"}
    if permit == FETCH:
        _arxiv_pdf(roles, live, list(snaps.values()), mode=arxiv_pdf, pin=pin,
                   key_ids=key_ids or keys_arxiv_ids(ident), counts=counts)
        _one_fetch_per_key(roles, live, counts, key_ids, list(snaps.values()), content.get("revisions"))

    content["snapshots"] = list(snaps.values())
    content["roles"] = list(roles.values())
    content["members_digest"] = ident["members_digest"]
    return content


__all__ = ["ARXIV_PDF_ERROR", "ARXIV_PDF_MODES", "ARXIV_PDF_RULE", "ARXIV_XML_ERROR", "TERMINAL", "arxiv_pdf_of", "carry", "fetch_key",
           "held_lead", "html_verdict", "merge_roles", "new_content", "new_role", "role_key"]
