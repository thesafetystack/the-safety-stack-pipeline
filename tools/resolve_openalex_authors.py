#!/usr/bin/env python3
"""``tools/resolve_openalex_authors.py`` — OpenAlex author ids for the researcher roster (C4).

docs/design/collection-yield.md C4 (D277; ids for all or most roster authors): the arXiv
author sweep queries names, and a common name brings its namesakes. An OpenAlex author id names one
profile. This tool finds, per roster name, the OpenAlex author profiles with that name, accepts one
when a written rule says it clearly is the researcher, writes the accepted ids into
``configs/rosters/researchers.yaml`` (``openalex_ids``, with ``--write-roster``) and writes a
markdown report of every name it could not settle, for a human to decide.

**Requests** (all through :class:`stack.collect.provider_keys.OpenAlexMeter`: the key as a bearer
header, every request reserved in the OpenAlex ledger, the run capped by ``--max-usd`` /
``--max-requests``; prices from that module, measured 2026-10-13):

1. a roster entry with an ``orcid``: ``/authors?filter=orcid:<id>`` (a list call, $0.0001);
2. every name: ``/autocomplete/authors?q=<name>`` (free; at most 10 profiles, display names only);
3. a name on the roster's ``disambiguate`` list, or one with 10 autocomplete answers (saturated),
   also ``/authors?search=<name>&per_page=100`` (a search, $0.001);
4. the profiles' details (works, topics, yearly counts, institutions, name variants):
   ``/authors?filter=id:A1|…|A50`` (a list call per 50 profiles);
5. coauthor evidence (unless ``--no-coauthors``): the works since :data:`WORKS_FROM` of every
   resolved id, ``/works?filter=authorships.author.id:A1|…|A100,from_publication_date:…`` with
   ``select=id,publication_date,title,authorships``, cursor-paged 200 a page (a list call a page),
   then the details of the name-matching authors found in them (4.).

So the whole roster (~2,250 names) costs about $0.05 plus $0.001 per searched name (2026-10-13: 680
searches, $0.69 in all); the coauthor evidence about $0.04 (2026-10-13: 1,928 ids, 372 pages). Answers
are cached (``--cache``, default ``<state dir>/openalex-authors-cache.jsonl``; the works in
``--works-cache``, default ``<state dir>/openalex-author-works-cache.jsonl``; JSON lines, last line
per key wins), so a re-run with a changed rule makes no request (``--offline`` forbids any).

**The rule** (:func:`decide`; constants below). A *candidate* is a profile whose display name or a
name variant matches the roster name under the arXiv sweep's ``names_match`` (same last name, same
first name or initial). *Active*: at least :data:`MIN_WORKS` works and a work in the last
:data:`RECENT_YEARS` years (``counts_by_year``). *CS share*: the share of the profile's topic counts
in OpenAlex field 17 (Computer Science); *adjacent share*: in the fields :data:`ADJACENT_FIELDS`
(CS, mathematics, decision sciences, economics, social sciences, psychology, neuroscience, arts and
humanities). *Org*: a ``last_known_institutions`` id, or an ``affiliations`` id with a year in the
last :data:`RECENT_YEARS` years, is one of channels.yaml's curated ``institutions``. *CS works*:
works × CS share. A name is *common* when it is on the ``disambiguate`` list or has at least
:data:`COMMON_ACTIVE` active candidates. A profile whose matching name has only an initial for the
first name ("Y. Chen") is not a candidate unless the roster name has one too: such profiles are
mostly merged namesakes. Then, in order:

- an ORCID lookup with exactly one profile → **resolved** (``orcid``);
- no candidate, or no active one → **not found**;
- one active candidate → **resolved** (``single``) when its adjacent share is at least
  :data:`ADJACENT_MIN` or it has an org; else **ambiguous** (the only match works in another field);
- several: exactly one has an org and a CS share of at least :data:`CS_MIN` → **resolved** (``org``);
  for a common name only an org among the profile's last known institutions counts (a common name's
  profile often merges namesakes whose affiliation lists span several employers);
- several, the name not common: the top candidate has at least :data:`MIN_CS_WORKS` CS works, a CS
  share of at least :data:`CS_MIN` and :data:`DOMINANCE` times the CS works of the second →
  **resolved** (``dominant``); else **ambiguous**. A common name is never resolved by dominance
  (several CS researchers share it; the most prolific need not be ours).

The name rule accepts one id per name; OpenAlex splits some researchers into several profiles.

**Coauthor evidence** (T-ARX R, 2026-10-13; D285: authors are concrete people, never accidental name
matches). A name without an id is no longer searched by name, and a wrong id brings a
namesake's whole output, so the coauthor rule accepts only clear wins. The works since
:data:`WORKS_FROM` of every resolved roster id are indexed (:func:`build_index`); a work whose
authorships are cut or exceed :data:`MAX_AUTHORS` is skipped (a list answer cuts authorships at 100
and does not serve ``is_authors_truncated`` under ``select``). For each author id the index holds
the *roster coauthors*: the distinct resolved roster people it shares works with, and the works.
Roster names that share an id are one person (:func:`merge_people`: ``Florian Tramer`` and
``Florian Tramèr``); a name's own roster namesakes never count as its coauthors; a profile that is
another, differently named roster person's id is theirs. Candidates = the name rule's candidates
plus every author of the indexed works whose display or raw name matches (``names_match`` and
``full_first``; its profile must match too). Per candidate: *coauthor link* = at least
:data:`LINK_PEOPLE` roster coauthors over at least :data:`LINK_WORKS` works; *shared fraction* =
those works over its works since :data:`WORKS_FROM` (``counts_by_year``); AI share (subfields
:data:`AI_SUBFIELDS`), CS share, ORCID. Then (:func:`decide_coauthors`):

- a profile counts only when at least :data:`SHARED_MIN` of its works since :data:`WORKS_FROM` are
  shared with roster people. OpenAlex merges a few of a researcher's works into a namesake's large
  profile (a hospital's "William H. Saunders", a mega-profile shown as "Zhiyuan Liu" for Fang Liu);
  the AI share does not see it (medical imaging counts as AI). Such a profile neither resolves nor
  blocks a name; the reason names it;
- an ambiguous or not-found name resolves (``coauthors``) when its coauthor-linked profiles are one
  person: the top one (most roster coauthors; for a common name at least
  :data:`COMMON_LINK_PEOPLE`) and its *splits*, each coauthor-linked and sharing at least
  :data:`SPLIT_SHARED` roster coauthors with it (common name: :data:`COMMON_SPLIT_SHARED`), or
  carrying the top one's ORCID and sharing one; a linked profile left over, or one with a
  different ORCID (two ORCIDs = two people), keeps the name ambiguous;
- a name with exactly one name-matching profile at all, not active, with a CS share of at least
  :data:`CS_MIN` or a configured institution, resolves (``single_inactive``);
- a name the name rule resolved gains splits by the same split rule (``added``; the report's
  ``split``); no id is removed. A profile refused only for its shared fraction but sharing roster
  coauthors with the accepted ones is reported as a possible split for a human.

**Merged-profile guard** (:func:`apply_guard`, :attr:`Candidate.merged_profile`; T-ARX,
2026-10-13, after collection-yield §9.5 found one roster id returning 2,063 of 6,938 works): a
profile with more than :data:`MERGED_CAP` works in the current or the previous calendar year, or
whose current-year works are at least :data:`JUMP_MIN` and :data:`JUMP_FACTOR` times
max(previous year, :data:`JUMP_FLOOR`), has absorbed namesakes. The guard runs over every accepted
id, the roster's old ones included (the one place an id is removed): the id leaves the roster
entry, a name left without ids drops to ambiguous ("merged profile"), and a merged profile is
neither coauthor evidence nor a candidate the coauthor rule accepts. Calibrated on all 1,973
roster ids (2026-10-13): the steady prolific stay (Yoshua Bengio 74 / 69 / 88 works in 2024-2026,
Xiangxiang Chu 19 / 29 / 82, Kai-Wei Chang's new profile 0 / 0 / 27); 11 ids go, among them one
real person's spike (Michal Valko's main profile, 9 / 1 / 46: old papers re-dated 2026 plus a few
namesakes; his two other profiles stay). Not adopted: letting profiles with 20-33% shared works
join as splits when they share >= 3 roster coauthors (it would add Jan Kautz's and Alina Oprea's
main profiles): of 30 of its 54 added ids read, 4 were namesake profiles and 5 more mixed.

The roster's ids count as resolved, so a run over a roster this rule already wrote indexes the ids
it added (one more round): decide from the roster before the write (1.3.0 from 1.2.0).
``--rounds 2`` indexes the works of the ids round one accepted as well and decides again from the
name rule (2026-10-13: 21 more names resolved, 2 back to ambiguous, 22 id sets changed). Precision
check 2026-10-13 (30 random new decisions across the kinds, recent titles read): 2 wrong under a
looser relevance test (AI share >= 20% sufficed), hence the shared fraction; then 1 of 30 on a fresh
sample (``docs/design/openalex-author-ids.md``).

**Report** (``--report``, default ``docs/design/openalex-author-ids.md``): header (date, roster
version, run, requests, cost, both rules), a before/after summary, then one table row per
ambiguous name, per not-found name, per newly resolved name, per resolved name that gained split
ids, per "check" name (resolved by the name rule, no roster coauthor, AI share below
:data:`AI_MIN`), per resolved common or ``disambiguate`` name (spot-check) and per possible split;
each row gives the reason and up to three profiles with their evidence (``A…`` id, name, works,
CS and AI share, recent works, last institution, org, ORCID, roster coauthors and shared works).
A sample from a fake transport: ``tests/fixtures/openalex_authors/sample-report.md`` (regenerated
by the test).

**Roster** (``--write-roster``): each roster line whose name was resolved becomes ``{name: "…",
openalex_ids: [A…]}`` (ids already there are kept and merged), the rest of the file (comments,
order, spelling) is untouched, and ``version`` is set to ``--roster-version``. The file is parsed
back and checked (same names, the expected ids) before it replaces the old one.

Usage::

    STACK_ENV_FILE=.env uv run --no-sync python tools/resolve_openalex_authors.py --max-usd 1.5 \\
        --rounds 2 --write-roster --roster-version 1.3.0
    uv run --no-sync python tools/resolve_openalex_authors.py --offline   # re-decide from the cache
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import importlib.util
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stack.collect import base
from stack.collect.provider_keys import OpenAlexBudgetStop, OpenAlexMeter, openalex_key
from stack.collect.roster import Roster, parse_roster, roster_name

API = "https://api.openalex.org"
SELECT = ("id,display_name,display_name_alternatives,orcid,works_count,last_known_institutions,"
          "topics,counts_by_year,affiliations")
DETAIL_BATCH = 50
SEARCH_PER_PAGE = 100
AUTOCOMPLETE_MAX = 10  # OpenAlex autocomplete answers at most 10: 10 = saturated (then a search)
DELAY_S = 0.15         # OpenAlex polite pool ~10 requests/s; stay well under
UA = "stack-collect/1.0 (+research; tools/resolve_openalex_authors.py)"
WORKS_SELECT = "id,publication_date,title,authorships"
WORKS_BATCH = 100      # author ids per `authorships.author.id` OR filter
WORKS_PER_PAGE = 200   # the cursor-paged list maximum
WORKS_FROM = "2019-01-01"

# The rule's constants (module docstring).
MIN_WORKS = 2
RECENT_YEARS = 3
CS_FIELD = "17"
ADJACENT_FIELDS = frozenset({"17", "26", "18", "20", "33", "32", "28", "12"})
AI_SUBFIELDS = frozenset({"1702", "1707"})  # Artificial Intelligence; Computer Vision and Pattern Recognition
CS_MIN = 0.2
ADJACENT_MIN = 0.3
MIN_CS_WORKS = 5
DOMINANCE = 3.0
COMMON_ACTIVE = 5
# The coauthor rule's constants (module docstring, "Coauthor evidence").
MAX_AUTHORS = 99       # works with more authorships are skipped (a list answer cuts authorships at 100)
LINK_PEOPLE = 2        # coauthor link: >= this many distinct resolved roster coauthors ...
LINK_WORKS = 2         # ... over >= this many distinct works
COMMON_LINK_PEOPLE = 3 # a common name's accepted profile needs this many roster coauthors
SPLIT_SHARED = 1       # a split shares this many roster coauthors with the accepted profile(s) ...
COMMON_SPLIT_SHARED = 2  # ... this many for a common name
# The merged-profile guard (module docstring): applied to every accepted id.
MERGED_CAP = 100       # works in the current or the previous calendar year above this: merged
JUMP_MIN = 40          # ... or the current year's works >= this ...
JUMP_FACTOR = 4        # ... and >= this times max(previous year's works, JUMP_FLOOR)
JUMP_FLOOR = 5
AI_MIN = 0.2           # "check" section: accepted profiles without roster coauthors and an AI share below this
SHARED_MIN = 1 / 3     # a profile the coauthor rule accepts shares >= this fraction of its works since
                       # WORKS_FROM with resolved roster people

Send = Callable[..., Any]


def _arxiv() -> Any:
    """The arXiv sweep module (its ``names_match``), imported by path like the runner does."""
    spec = importlib.util.spec_from_file_location(
        "_resolve_arxiv_sweep", REPO / "workflows" / "collect" / "arxiv_sweep" / "main.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ARXIV: Any = None


def names_match(target: str, other: str) -> bool:
    global _ARXIV
    if _ARXIV is None:
        _ARXIV = _arxiv()
    return bool(_ARXIV.names_match(target, other))


def full_first(variant: str, roster: str) -> bool:
    """The variant's first name is more than an initial, or the roster name's is an initial too."""
    global _ARXIV
    if _ARXIV is None:
        _ARXIV = _arxiv()
    v, r = _ARXIV._name_parts(variant), _ARXIV._name_parts(roster)
    return bool(v) and (not _ARXIV._is_initial(v["first"]) or (bool(r) and _ARXIV._is_initial(r["first"])))


def short_id(url: str) -> str:
    """``https://openalex.org/A123`` → ``A123``."""
    return str(url or "").rstrip("/").rsplit("/", 1)[-1]


def _tail(url: Any) -> str:
    return str(url or "").rstrip("/").rsplit("/", 1)[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Evidence and the rule
# ─────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Candidate:
    id: str
    name: str
    name_ok: bool
    works: int
    cs_share: float
    adj_share: float
    recent: int
    institution: str | None
    org: str | None
    orcid: bool
    org_last: bool = False      # the org is a last known institution (not only a recent affiliation)
    ai_share: float = 0.0       # topic counts in AI_SUBFIELDS
    orcid_id: str | None = None  # the bare ORCID id
    #: coauthor evidence (:func:`attach_coauthors`): resolved roster person → shared works
    coauthors: dict[str, int] = dataclasses.field(default_factory=dict)
    coauthor_works: int = 0     # distinct works shared with any resolved roster person
    works_since: int = 0        # works since WORKS_FROM (counts_by_year)
    yearly: tuple[int, int, int] = (0, 0, 0)  # works in year-2, year-1, year (counts_by_year)
    works_window: int = 0       # guard report: fetched works since October of the previous year

    @property
    def active(self) -> bool:
        return self.works >= MIN_WORKS and self.recent >= 1

    @property
    def cs_works(self) -> float:
        return self.works * self.cs_share

    @property
    def linked(self) -> bool:
        """A coauthor link: >= LINK_PEOPLE resolved roster coauthors over >= LINK_WORKS works."""
        return len(self.coauthors) >= LINK_PEOPLE and self.coauthor_works >= LINK_WORKS

    @property
    def merged_profile(self) -> bool:
        """The guard: a profile that absorbed namesakes (works in the current or previous year above
        MERGED_CAP, or a current-year jump to >= JUMP_MIN and JUMP_FACTOR x the previous year)."""
        _, prev, cur = self.yearly
        return max(prev, cur) > MERGED_CAP or (cur >= JUMP_MIN and cur >= JUMP_FACTOR * max(prev, JUMP_FLOOR))

    @property
    def shared_fraction(self) -> float:
        return self.coauthor_works / max(self.works_since, 1)

    @property
    def relevant(self) -> bool:
        """>= SHARED_MIN of its works since WORKS_FROM are shared with resolved roster people, and
        not a merged profile: a profile into which OpenAlex merged a few of the person's works and a
        namesake's whole output fails (the AI share does not see that: the namesake's field may be
        counted as AI)."""
        return not self.merged_profile and self.shared_fraction >= SHARED_MIN


def candidate(roster: str, rec: Mapping[str, Any], orgs: Mapping[str, str], year: int) -> Candidate:
    """A profile's evidence (module docstring) for the roster name ``roster``."""
    variants = [str(rec.get("display_name") or ""), *map(str, rec.get("display_name_alternatives") or [])]
    topics = [t for t in rec.get("topics") or [] if isinstance(t, dict)]
    total = sum(int(t.get("count") or 0) for t in topics)
    field_of = lambda t: _tail((t.get("field") or {}).get("id"))
    cs = sum(int(t.get("count") or 0) for t in topics if field_of(t) == CS_FIELD)
    adj = sum(int(t.get("count") or 0) for t in topics if field_of(t) in ADJACENT_FIELDS)
    ai = sum(int(t.get("count") or 0) for t in topics
             if _tail((t.get("subfield") or {}).get("id")) in AI_SUBFIELDS)
    recent = sum(int(c.get("works_count") or 0) for c in rec.get("counts_by_year") or []
                 if int(c.get("year") or 0) > year - RECENT_YEARS)
    since = sum(int(c.get("works_count") or 0) for c in rec.get("counts_by_year") or []
                if int(c.get("year") or 0) >= int(WORKS_FROM[:4]))
    by_year = {int(c.get("year") or 0): int(c.get("works_count") or 0) for c in rec.get("counts_by_year") or []}
    last = [i for i in rec.get("last_known_institutions") or [] if isinstance(i, dict)]
    last_ids = [_tail(i.get("id")) for i in last]
    inst_ids = list(last_ids)
    for a in rec.get("affiliations") or []:
        if any(int(y) > year - RECENT_YEARS for y in a.get("years") or []):
            inst_ids.append(_tail((a.get("institution") or {}).get("id")))
    org = next((orgs[i] for i in inst_ids if i in orgs), None)
    return Candidate(
        id=short_id(str(rec.get("id") or "")), name=variants[0],
        name_ok=any(v and names_match(roster, v) and full_first(v, roster) for v in variants),
        works=int(rec.get("works_count") or 0),
        cs_share=cs / total if total else 0.0, adj_share=adj / total if total else 0.0,
        recent=recent, institution=str(last[0].get("display_name")) if last else None,
        org=org, orcid=bool(rec.get("orcid")), org_last=any(i in orgs for i in last_ids),
        ai_share=ai / total if total else 0.0, orcid_id=_tail(rec.get("orcid")) or None,
        works_since=since, yearly=(by_year.get(year - 2, 0), by_year.get(year - 1, 0), by_year.get(year, 0)))


@dataclasses.dataclass
class Decision:
    name: str
    status: str                 # resolved | ambiguous | not_found | not_queried | error
    ids: list[str]
    reason: str
    how: str = ""               # resolved: orcid | single | org | dominant | coauthors | single_inactive
    candidates: list[Candidate] = dataclasses.field(default_factory=list)
    common: bool = False
    disambiguate: bool = False
    split: Candidate | None = None   # resolved: a second active CS profile
    #: every candidate profile (the report shows ``candidates``, the top three)
    pool: list[Candidate] = dataclasses.field(default_factory=list, repr=False)
    before: str = ""            # the status under the name rule, before the coauthor rule ran
    added: list[Candidate] = dataclasses.field(default_factory=list)  # splits the coauthor rule added
    removed: list[Candidate] = dataclasses.field(default_factory=list)  # merged profiles the guard removed


def _rank(cands: Iterable[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: (-c.cs_works, -c.works, c.id))


def decide(name: str, cands: list[Candidate], *, disambiguate: bool = False,
           orcid_hit: Candidate | None = None) -> Decision:
    """The rule (module docstring) over a name's candidate profiles."""
    d = Decision(name, "", [], "", disambiguate=disambiguate, pool=list(cands))
    d.common = disambiguate or sum(1 for c in cands if c.name_ok and c.active) >= COMMON_ACTIVE
    if orcid_hit is not None:
        d.status, d.ids, d.how, d.candidates = "resolved", [orcid_hit.id], "orcid", [orcid_hit]
        d.reason = "the roster's ORCID names exactly one profile"
        return d
    matched = [c for c in cands if c.name_ok]
    active = _rank(c for c in matched if c.active)
    d.candidates = active[:3] or _rank(matched)[:3] or _rank(cands)[:3]
    if not matched:
        d.status, d.reason = "not_found", f"no profile with a matching name among {len(cands)}"
        return d
    if not active:
        d.status = "not_found"
        d.reason = (f"{len(matched)} name match(es), none active (>= {MIN_WORKS} works and a work in "
                    f"the last {RECENT_YEARS} years)")
        return d
    top = active[0]
    common = d.common = disambiguate or len(active) >= COMMON_ACTIVE
    if len(active) == 1:
        if top.adj_share >= ADJACENT_MIN or top.org:
            d.status, d.ids, d.how = "resolved", [top.id], "single"
            d.reason = "the only active name match"
        else:
            d.status = "ambiguous"
            d.reason = (f"the only active name match works outside AI-adjacent fields (adjacent share "
                        f"{top.adj_share:.0%} < {ADJACENT_MIN:.0%})")
        return d
    # a common name's profiles are often merged namesakes whose affiliation lists span employers:
    # for it, only a last known institution counts as the org
    with_org = [c for c in active if c.org and c.cs_share >= CS_MIN and (c.org_last or not common)]
    if len(with_org) == 1:
        d.status, d.ids, d.how = "resolved", [with_org[0].id], "org"
        d.reason = f"the only active CS profile at a configured institution ({with_org[0].org})"
        d.candidates = [with_org[0], *[c for c in active if c is not with_org[0]][:2]]  # accepted first
        d.split = next((c for c in active if c is not with_org[0] and c.cs_share >= CS_MIN), None)
        return d
    second = active[1]
    dominant = (top.cs_works >= MIN_CS_WORKS and top.cs_share >= CS_MIN
                and top.cs_works >= DOMINANCE * max(second.cs_works, 1.0))
    if dominant and not common:
        d.status, d.ids, d.how = "resolved", [top.id], "dominant"
        d.reason = (f"{top.cs_works:.0f} CS works against {second.cs_works:.0f} for the next of "
                    f"{len(active)} active name matches")
        d.split = next((c for c in active[1:] if c.cs_share >= CS_MIN and c.cs_works >= MIN_CS_WORKS),
                       None)
        return d
    d.status = "ambiguous"
    if dominant:
        d.reason = (f"common name ({len(active)} active profiles); the top one dominates on CS works "
                    f"({top.cs_works:.0f} vs {second.cs_works:.0f}), which is not enough for a common name")
    else:
        d.reason = (f"{len(active)} active name matches; the top two are within {DOMINANCE:g}x on CS "
                    f"works ({top.cs_works:.0f} vs {second.cs_works:.0f})")
    return d


def _orcid_conflict(c: Candidate, group: Iterable[Candidate]) -> bool:
    """``c`` and a profile of ``group`` carry different ORCIDs: two people."""
    return bool(c.orcid_id) and any(g.orcid_id and g.orcid_id != c.orcid_id for g in group)


def _splits(group: list[Candidate], matched: list[Candidate], common: bool) -> list[Candidate]:
    """The name-matching profiles that join ``group`` as the same person's splits: with a shared
    fraction >= SHARED_MIN, coauthor-linked and sharing >= SPLIT_SHARED (common name:
    COMMON_SPLIT_SHARED) roster coauthors with the group, or the same ORCID as a group profile and
    >= 1 shared roster coauthor. Never a merged profile or one with a different ORCID."""
    need = COMMON_SPLIT_SHARED if common else SPLIT_SHARED
    out: list[Candidate] = []
    seen = {g.id for g in group}
    for c in _rank_linked(matched):
        if c.id in seen or c.merged_profile or _orcid_conflict(c, [*group, *out]):
            continue
        shared = set(c.coauthors) & {p for g in [*group, *out] for p in g.coauthors}
        if not c.relevant:
            continue
        same_orcid = bool(c.orcid_id) and any(g.orcid_id == c.orcid_id for g in [*group, *out])
        if (same_orcid and shared) or (c.linked and len(shared) >= need):
            out.append(c)
            seen.add(c.id)
    return out


def _rank_linked(cands: Iterable[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: (-len(c.coauthors), -c.coauthor_works, -c.cs_works, c.id))


def _who(c: Candidate, k: int = 3) -> str:
    names = sorted(c.coauthors, key=lambda p: (-c.coauthors[p], p))
    more = f" +{len(names) - k}" if len(names) > k else ""
    return f"{len(names)} roster coauthors ({', '.join(names[:k])}{more}) over {c.coauthor_works} works"


def _near_split(group: list[Candidate], matched: list[Candidate], common: bool) -> Candidate | None:
    """The best coauthor-linked profile the split rule refused only for its shared fraction
    (SHARED_MIN): it shares enough roster coauthors with ``group`` and no other ORCID. Reported as
    a possible split for a human; never accepted."""
    need = COMMON_SPLIT_SHARED if common else SPLIT_SHARED
    have = {g.id for g in group}
    pooled = {p for g in group for p in g.coauthors}
    return next((c for c in _rank_linked(matched) if c.linked and not c.relevant and c.id not in have
                 and not c.merged_profile
                 and not _orcid_conflict(c, group) and len(set(c.coauthors) & pooled) >= need), None)


def decide_coauthors(d: Decision) -> Decision:
    """The coauthor rule (module docstring) over a name-rule decision whose ``pool`` carries
    coauthor evidence (:func:`attach_coauthors`): a resolved name may gain splits (``added``); an
    ambiguous or not-found name may resolve (``coauthors``, ``single_inactive``). Never removes an
    id."""
    d.before = d.before or d.status
    if any(w.lower() == "team" for w in d.name.split()):  # "The Sora team": no person, no coauthors
        return d
    matched = [c for c in d.pool if c.name_ok]
    if d.status == "resolved":
        group = [c for c in matched if c.id in d.ids]
        if group:
            d.added = _splits(group, matched, d.common)
            d.ids = [*d.ids, *(c.id for c in d.added if c.id not in d.ids)]
            near = _near_split([*group, *d.added], matched, d.common)
            if near is not None and (d.split is None or d.split.id in d.ids):
                d.split = near
        return d
    if d.status not in ("ambiguous", "not_found"):
        return d
    linked = _rank_linked(c for c in matched if c.linked and c.relevant)
    off = [c for c in matched if c.linked and not c.relevant]
    if off:  # merged namesake profiles that carry a few of the person's works: neither accept nor block
        d.reason += (f"; {len(off)} coauthor-linked profile(s) with < {SHARED_MIN:.0%} of their works since "
                     f"{WORKS_FROM} shared with roster people (a merged namesake?): "
                     + ", ".join(f"`{c.id}` ({_who(c, 2)}, {c.works_since} works since)" for c in off[:2]))
        d.candidates = [*off[:1], *[c for c in d.candidates if c is not off[0]]][:3]
    if linked:
        top = linked[0]
        if d.common and len(top.coauthors) < COMMON_LINK_PEOPLE:
            d.status = "ambiguous"
            d.reason = (f"common name; the best coauthor-linked profile `{top.id}` has {_who(top)}, "
                        f"fewer than {COMMON_LINK_PEOPLE} roster coauthors")
            d.candidates = [top, *[c for c in d.candidates if c is not top]][:3]
            return d
        group = [top, *_splits([top], matched, d.common)]
        rivals = [c for c in linked if c not in group]
        if rivals:
            r = rivals[0]
            shared = len(set(r.coauthors) & set(top.coauthors))
            why = ("a different ORCID" if _orcid_conflict(r, group) else
                   f"{shared} shared roster coauthor(s) (a split needs "
                   f"{COMMON_SPLIT_SHARED if d.common else SPLIT_SHARED})")
            d.status = "ambiguous"
            d.reason = (f"{len(linked)} coauthor-linked profiles that are not one person: `{top.id}` "
                        f"({_who(top)}) and `{r.id}` ({_who(r)}), {why}")
            d.candidates = [top, r, *[c for c in linked if c not in (top, r)]][:3]
            return d
        d.status, d.ids, d.how = "resolved", [c.id for c in group], "coauthors"
        d.reason = f"the only coauthor-linked person: `{top.id}` has {_who(top)}" + (
            f"; {len(group) - 1} split(s) share its roster coauthors" if len(group) > 1 else "")
        d.candidates = group[:3]
        d.added = group[1:]
        d.split = _near_split(group, matched, d.common)
        return d
    if len(matched) == 1 and not matched[0].active and matched[0].works >= 1 and (
            matched[0].cs_share >= CS_MIN or matched[0].org):
        c = matched[0]
        d.status, d.ids, d.how, d.candidates = "resolved", [c.id], "single_inactive", [c]
        d.reason = (f"the only profile with a matching name, CS share {c.cs_share:.0%}"
                    f"{', org ' + c.org if c.org else ''}, not active")
    return d


# ─────────────────────────────────────────────────────────────────────────────
# OpenAlex client (through the meter) and the cache
# ─────────────────────────────────────────────────────────────────────────────
class Offline(Exception):
    """``--offline`` and the cache lacks an answer."""


class Cache:
    """JSON lines ``{"kind", "key", "value"}``; the last line per key wins. Kinds: ``name`` and
    ``author`` (the authors cache), ``work`` (a reduced work, :func:`reduce_work`) and
    ``works_of`` (an author id → the ids of its works since the floor; the works cache)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.kinds: dict[str, dict[str, Any]] = {"name": {}, "author": {}, "work": {}, "works_of": {}}
        if path is not None and path.exists():
            with path.open(encoding="utf-8", newline="\n") as fh:  # not splitlines: U+2028 in names
                for line in fh:
                    if line.strip():
                        j = json.loads(line)
                        self.kinds.setdefault(j["kind"], {})[j["key"]] = j["value"]

    @property
    def names(self) -> dict[str, Any]:
        return self.kinds["name"]

    @property
    def authors(self) -> dict[str, Any]:
        return self.kinds["author"]

    def put(self, kind: str, key: str, value: Any) -> None:
        self.kinds.setdefault(kind, {})[key] = value
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": kind, "key": key, "value": value},
                                    ensure_ascii=False) + "\n")


class Client:
    """GETs to OpenAlex through ``send`` (a :class:`stack.collect.base.Sender`, the meter's in a
    live run), paced; a non-200 answer raises ``RuntimeError`` (the caller records the name as
    ``error``); :class:`OpenAlexBudgetStop` propagates."""

    def __init__(self, send: Send | None, *, mailto: str | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.send, self.mailto, self.sleep = send, mailto, sleep
        self.requests = Counter[str]()

    def get(self, path: str, params: dict[str, Any], kind: str) -> dict[str, Any]:
        if self.send is None:
            raise Offline(path)
        if self.mailto:
            params = {**params, "mailto": self.mailto}
        url = base.full_url(f"{API}{path}", params)
        resp = self.send("GET", url, headers={"User-Agent": UA}, body=None, timeout=60.0,
                         max_bytes=50_000_000)
        self.requests[kind] += 1
        self.sleep(DELAY_S)
        if resp.status != 200:
            raise RuntimeError(f"OpenAlex {path} answered {resp.status}")
        data = json.loads(resp.body or b"null")
        if not isinstance(data, dict):
            raise TypeError(f"OpenAlex {path}: not a JSON object")
        return data

    def autocomplete(self, name: str) -> list[dict[str, Any]]:
        return list(self.get("/autocomplete/authors", {"q": name}, "autocomplete").get("results") or [])

    def search(self, name: str) -> list[dict[str, Any]]:
        return list(self.get("/authors", {"search": name, "per_page": SEARCH_PER_PAGE,
                                          "select": SELECT}, "search").get("results") or [])

    def by_orcid(self, orcid: str) -> list[dict[str, Any]]:
        return list(self.get("/authors", {"filter": f"orcid:{orcid}", "select": SELECT},
                             "orcid").get("results") or [])

    def details(self, ids: list[str]) -> list[dict[str, Any]]:
        return list(self.get("/authors", {"filter": "id:" + "|".join(ids), "per_page": len(ids),
                                          "select": SELECT}, "details").get("results") or [])

    def works(self, ids: list[str], floor: str, cursor: str) -> dict[str, Any]:
        """One cursor page of the works of any of ``ids`` published since ``floor``."""
        return self.get("/works", {"filter": f"authorships.author.id:{'|'.join(ids)},"
                                             f"from_publication_date:{floor}",
                                   "select": WORKS_SELECT, "per_page": WORKS_PER_PAGE,
                                   "cursor": cursor}, "works")


# ─────────────────────────────────────────────────────────────────────────────
# Coauthor evidence
# ─────────────────────────────────────────────────────────────────────────────
def reduce_work(w: Mapping[str, Any]) -> dict[str, Any]:
    """What the coauthor index keeps of a work: id, date, title, the authorship count, whether the
    authorships are cut, and (for a work of at most MAX_AUTHORS) ``[author id, display name, raw
    name]`` per authorship."""
    auth = [a for a in w.get("authorships") or [] if isinstance(a, dict)]
    trunc = bool(w.get("is_authors_truncated")) or len(auth) > MAX_AUTHORS
    authors = [] if trunc else [
        [short_id((a.get("author") or {}).get("id") or ""), str((a.get("author") or {}).get("display_name") or ""),
         str(a.get("raw_author_name") or "")] for a in auth]
    return {"id": short_id(str(w.get("id") or "")), "date": w.get("publication_date"),
            "title": w.get("title"), "n": len(auth), "trunc": trunc,
            "authors": [a for a in authors if a[0]]}


def fetch_works(client: Client, wcache: Cache, ids: Iterable[str], *, floor: str = WORKS_FROM,
                log: Callable[[str], None] = lambda s: None) -> str | None:
    """The works of every id since ``floor`` into ``wcache`` (``work`` and ``works_of``), WORKS_BATCH
    ids per OR filter, cursor-paged; ids already cached are skipped. Returns the budget-stop
    reason, ``"offline"`` when ids are missing and no request may be made, else ``None``."""
    missing = sorted(set(ids) - set(wcache.kinds["works_of"]))
    for k in range(0, len(missing), WORKS_BATCH):
        batch = missing[k:k + WORKS_BATCH]
        got: list[dict[str, Any]] = []
        cursor: str | None = "*"
        try:
            while cursor:
                page = client.works(batch, floor, cursor)
                results = [r for r in page.get("results") or [] if isinstance(r, dict)]
                for r in results:
                    red = reduce_work(r)
                    wcache.put("work", red["id"], red)
                    got.append(red)
                cursor = (page.get("meta") or {}).get("next_cursor") if results else None
        except OpenAlexBudgetStop as e:
            log(f"works: budget stop ({e.reason}) at batch {k // WORKS_BATCH}")
            return e.reason
        except Offline:
            return "offline"
        mine: dict[str, list[str]] = {a: [] for a in batch}
        for r in got:
            for a in {x[0] for x in r["authors"]} & set(batch):
                mine[a].append(r["id"])
        for a in batch:
            wcache.put("works_of", a, mine[a])
        log(f"works: batch {k // WORKS_BATCH + 1}/{-(-len(missing) // WORKS_BATCH)}, {len(got)} works")
    return None


@dataclasses.dataclass
class CoauthorIndex:
    """Over the works of the resolved roster people: per author id, the roster people it shares
    works with (``links``: author id → person → work ids) and the names it carries in them."""

    links: dict[str, dict[str, set[str]]]
    names: dict[str, set[str]]
    by_last: dict[str, set[str]]  # normalised last name → author ids
    works_used: int = 0
    works_skipped: int = 0


def _last_key(name: str) -> str | None:
    global _ARXIV
    if _ARXIV is None:
        _ARXIV = _arxiv()
    parts = _ARXIV._name_parts(name)
    return _ARXIV._norm_name(parts["last"]) if parts else None


def build_index(works: Iterable[Mapping[str, Any]], person_ids: Mapping[str, Iterable[str]]) -> CoauthorIndex:
    """The coauthor index (:class:`CoauthorIndex`); works whose authorships are cut or exceed
    MAX_AUTHORS are skipped. An author id never links to a person it is an id of."""
    owner: dict[str, set[str]] = {}
    for p, ids in person_ids.items():
        for a in ids:
            owner.setdefault(a, set()).add(p)
    idx = CoauthorIndex({}, {}, {})
    seen: set[str] = set()
    for w in works:
        if w["id"] in seen:
            continue
        seen.add(w["id"])
        if w.get("trunc") or int(w.get("n") or 0) > MAX_AUTHORS:
            idx.works_skipped += 1
            continue
        idx.works_used += 1
        people = {p for a in w["authors"] for p in owner.get(a[0], ())}
        for aid, display, raw in w["authors"]:
            for p in people - owner.get(aid, set()):
                idx.links.setdefault(aid, {}).setdefault(p, set()).add(w["id"])
            names = idx.names.setdefault(aid, set())
            for s in (display, raw):
                if s and s not in names:
                    names.add(s)
                    key = _last_key(s)
                    if key:
                        idx.by_last.setdefault(key, set()).add(aid)
    return idx


def works_candidates(idx: CoauthorIndex, name: str) -> set[str]:
    """Author ids in the indexed works whose display or raw name matches ``name``."""
    key = _last_key(name)
    return {a for a in idx.by_last.get(key or "", ())
            if any(names_match(name, s) and full_first(s, name) for s in idx.names.get(a, ()))}


def attach_coauthors(c: Candidate, idx: CoauthorIndex, exclude: set[str]) -> None:
    """``c.coauthors`` / ``c.coauthor_works`` from the index, without the people in ``exclude`` (the
    name itself and its roster namesakes: a twin entry is not evidence)."""
    got = {p: ws for p, ws in idx.links.get(c.id, {}).items() if p not in exclude}
    c.coauthors = {p: len(ws) for p, ws in got.items()}
    c.coauthor_works = len(set().union(*got.values())) if got else 0


# ─────────────────────────────────────────────────────────────────────────────
# The run
# ─────────────────────────────────────────────────────────────────────────────
def ordered_names(roster: Roster, only: Iterable[str] | None = None) -> list[str]:
    """``disambiguate`` names first (C4's), then the rest in roster order; ``only`` narrows."""
    names = roster.names
    if only is not None:
        want = {roster_name(n) for n in only}
        unknown = sorted(want - set(names))
        if unknown:
            raise SystemExit(f"--names: {unknown} are not roster names")
        names = [n for n in names if n in want]
    return [n for n in names if n in roster.disambiguate] + [n for n in names if n not in roster.disambiguate]


def resolve(roster: Roster, client: Client, cache: Cache, *, orgs: Mapping[str, str], year: int,
            names: list[str], log: Callable[[str], None] = lambda s: None) -> list[Decision]:
    """Three passes (module docstring): names (ORCID, autocomplete, search), details, decisions.
    A budget stop ends the requests; names whose answers are incomplete are ``not_queried``."""
    stop: str | None = None
    errors: dict[str, str] = {}
    orcid_of = {e.name: e.orcid for e in roster.entries}
    for i, n in enumerate(names):
        if stop or n in cache.names:
            continue
        try:
            got: dict[str, Any] = {}
            if orcid_of.get(n):
                recs = client.by_orcid(str(orcid_of[n]))
                for r in recs:
                    cache.put("author", short_id(r["id"]), r)
                got["orcid"] = [short_id(r["id"]) for r in recs]
            ac = client.autocomplete(n)
            got["autocomplete"] = [short_id(r.get("id", "")) for r in ac]
            got["common"] = n in roster.disambiguate or len(ac) >= AUTOCOMPLETE_MAX  # = searched
            if got["common"]:
                recs = client.search(n)
                for r in recs:
                    cache.put("author", short_id(r["id"]), r)
                got["search"] = [short_id(r["id"]) for r in recs]
            cache.put("name", n, got)
        except OpenAlexBudgetStop as e:
            stop = e.reason
            log(f"budget stop ({e.reason}) at name {i} of {len(names)}")
        except Offline:
            continue
        except (RuntimeError, TypeError, ValueError, KeyError) as e:
            errors[n] = str(e)
        if i % 100 == 0:
            log(f"names {i}/{len(names)}")
    wanted = sorted({a for n in names for a in _ids_of(cache.names.get(n))} - set(cache.authors))
    for k in range(0, len(wanted), DETAIL_BATCH):
        if stop:
            break
        batch = wanted[k:k + DETAIL_BATCH]
        try:
            fetch_detail_batch(client, cache, batch)
        except OpenAlexBudgetStop as e:
            stop = e.reason
        except Offline:
            break
        except (RuntimeError, TypeError, ValueError, KeyError) as e:
            for n in names:
                if set(_ids_of(cache.names.get(n))) & set(batch):
                    errors.setdefault(n, str(e))
    out: list[Decision] = []
    for n in names:
        dis = n in roster.disambiguate
        got = cache.names.get(n)
        if n in errors:
            out.append(Decision(n, "error", [], errors[n], disambiguate=dis))
            continue
        ids = _ids_of(got)
        if got is None or any(a not in cache.authors for a in ids):
            out.append(Decision(n, "not_queried", [], f"budget stop ({stop})" if stop else "no answer cached",
                                disambiguate=dis))
            continue
        cands = [candidate(n, cache.authors[a], orgs, year) for a in dict.fromkeys(ids)
                 if cache.authors[a] is not None]
        orcid_hits = [c for c in cands if c.id in (got.get("orcid") or [])]
        out.append(decide(n, cands, disambiguate=dis,
                          orcid_hit=orcid_hits[0] if len(orcid_hits) == 1 else None))
    return out


def fetch_detail_batch(client: Client, cache: Cache, batch: list[str]) -> None:
    """The profiles of ``batch`` (at most DETAIL_BATCH ids) into ``cache``; an id not served is
    cached as ``None`` (a merged profile: no evidence)."""
    got_ids = set()
    for r in client.details(batch):
        cache.put("author", short_id(r["id"]), r)
        got_ids.add(short_id(r["id"]))
    for a in batch:
        if a not in got_ids:
            cache.put("author", a, None)


def _ids_of(got: Any) -> list[str]:
    if not got:
        return []
    return [a for a in (*(got.get("orcid") or []), *(got.get("autocomplete") or []),
                        *(got.get("search") or [])) if a]


def is_merged(rec: Mapping[str, Any] | None, year: int) -> bool:
    """The guard (:attr:`Candidate.merged_profile`) over a cached profile record; no record: no."""
    return rec is not None and candidate("", rec, {}, year).merged_profile


def apply_guard(decisions: list[Decision], roster: Roster, cache: Cache, *, orgs: Mapping[str, str],
                year: int, wcache: Cache | None = None) -> int:
    """The merged-profile guard over every accepted id, the roster's included: a merged id leaves
    the decision (``removed``); a name left without ids drops to ambiguous ("merged profile").
    The one place an id is removed. ``wcache``: counts the removed profile's fetched works since
    October of the previous year (shown in the report). Returns the number of ids removed."""
    roster_ids = {e.name: set(e.openalex_ids) for e in roster.entries}
    n = 0
    for d in decisions:
        gone = {c.id for c in d.removed}
        ids = [a for a in (*d.ids, *sorted(roster_ids.get(d.name, ()))) if a not in gone]
        ids = list(dict.fromkeys(ids))
        bad = []
        for a in ids:
            rec = cache.authors.get(a)
            if rec is not None and is_merged(rec, year):
                c = next((c for c in d.pool if c.id == a), None) or candidate(d.name, rec, orgs, year)
                if wcache is not None:
                    c.works_window = sum(1 for w in wcache.kinds["works_of"].get(a) or ()
                                           if (wcache.kinds["work"].get(w) or {}).get("date", "") >= f"{year - 1}-10-01")
                bad.append(c)
        if not bad:
            continue
        n += len(bad)
        d.removed = [*d.removed, *bad]
        d.ids = [a for a in d.ids if a not in {c.id for c in bad}]
        if d.status == "resolved" and not d.ids:
            d.before = d.before or d.status
            d.status, d.how = "ambiguous", ""
            d.reason = "merged profile: " + "; ".join(
                f"`{c.id}` {c.name} ({c.yearly[0]} / {c.yearly[1]} / {c.yearly[2]} works in "
                f"{year - 2}-{year})" for c in bad) + " — removed"
    return n


def id_counts(roster: Roster, decisions: list[Decision]) -> dict[str, int]:
    """The roster's ids and names with several ids, before and after the decisions are written."""
    before = {e.name: set(e.openalex_ids) for e in roster.entries}
    after = {n: set(ids) for n, ids in before.items()}
    for d in decisions:
        after.setdefault(d.name, set()).difference_update(c.id for c in d.removed)
        if d.status == "resolved":
            after[d.name].update(d.ids)
    return {"ids_before": sum(map(len, before.values())), "ids_after": sum(map(len, after.values())),
            "multi_before": sum(1 for s in before.values() if len(s) > 1),
            "multi_after": sum(1 for s in after.values() if len(s) > 1)}


class _People:
    """Roster people by normalised last name, for the namesake exclusion."""

    def __init__(self, names: Iterable[str]) -> None:
        self.by_last: dict[str, list[str]] = {}
        for n in names:
            k = _last_key(n)
            if k:
                self.by_last.setdefault(k, []).append(n)

    def namesakes(self, name: str) -> set[str]:
        """``name`` and every roster person whose name matches it either way."""
        return {name} | {p for p in self.by_last.get(_last_key(name) or "", [])
                         if names_match(name, p) or names_match(p, name)}


def merge_people(person_ids: Mapping[str, Iterable[str]]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Roster names that share an id are one person (a spelling twin, e.g. ``Florian Tramer`` and
    ``Florian Tramèr``): ``({key: ids}, {name: key})`` with the key the names joined by `` / ``."""
    parent = {n: n for n in person_ids}

    def root(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    first: dict[str, str] = {}
    for n, ids in person_ids.items():
        for a in ids:
            if a in first:
                parent[root(n)] = root(first[a])
            else:
                first[a] = n
    groups: dict[str, list[str]] = {}
    for n in person_ids:
        groups.setdefault(root(n), []).append(n)
    merged: dict[str, set[str]] = {}
    canon: dict[str, str] = {}
    for members in groups.values():
        key = " / ".join(sorted(members))
        merged[key] = {a for m in members for a in person_ids[m]}
        canon.update({m: key for m in members})
    return merged, canon


def resolve_coauthors(decisions: list[Decision], roster: Roster, client: Client, cache: Cache,
                      wcache: Cache, *, orgs: Mapping[str, str], year: int, floor: str = WORKS_FROM,
                      rounds: int = 1, log: Callable[[str], None] = lambda s: None,
                      ) -> tuple[list[Decision], dict[str, Any]]:
    """The coauthor rule over the name rule's ``decisions`` (module docstring): fetch the works of
    every resolved id (the roster's and the decisions'), index them, add the name-matching authors
    of those works as candidates (their profiles fetched), attach the evidence and decide again
    (:func:`decide_coauthors`). Round two indexes the works of the ids round one accepted as well
    and decides again from the name rule's decisions. Returns the new decisions and statistics."""
    people = _People(roster.names)
    base_ids: dict[str, set[str]] = {e.name: set(e.openalex_ids) for e in roster.entries if e.openalex_ids}
    for d in decisions:
        if d.status == "resolved":
            base_ids.setdefault(d.name, set()).update(d.ids)
    bad = {a for ids in base_ids.values() for a in ids if is_merged(cache.authors.get(a), year)}
    person_ids = {p: ids - bad for p, ids in base_ids.items() if ids - bad}  # a merged profile is no evidence
    stats: dict[str, Any] = {"rounds": []}
    out = decisions
    for rnd in range(rounds):
        all_ids = {a for ids in person_ids.values() for a in ids}
        stop = fetch_works(client, wcache, all_ids, floor=floor, log=log)
        works_of, work = wcache.kinds["works_of"], wcache.kinds["work"]
        unfetched = sorted(a for a in all_ids if a not in works_of)
        grouped, canon = merge_people(person_ids)
        idx = build_index((work[w] for a in sorted(all_ids) for w in works_of.get(a) or () if w in work),
                          grouped)
        owner: dict[str, set[str]] = {}
        for p, ids in person_ids.items():
            for a in ids:
                owner.setdefault(a, set()).add(p)
        extra: dict[str, list[str]] = {}
        for d in decisions:
            if d.status in ("resolved", "ambiguous", "not_found"):
                have = {c.id for c in d.pool}
                extra[d.name] = sorted(works_candidates(idx, d.name) - have)
        wanted = sorted({a for ids in extra.values() for a in ids} - set(cache.authors))
        for k in range(0, len(wanted), DETAIL_BATCH):
            if stop:
                break
            try:
                fetch_detail_batch(client, cache, wanted[k:k + DETAIL_BATCH])
            except OpenAlexBudgetStop as e:
                stop = e.reason
            except Offline:
                stop = "offline"
        new: list[Decision] = []
        for d in decisions:
            if d.name not in extra:
                new.append(d)
                continue
            same = people.namesakes(d.name)
            if d.name in canon:  # roster spellings sharing an id with it (merge_people): one person
                same |= {n for n, k in canon.items() if k == canon[d.name]}
            pool = [dataclasses.replace(c, coauthors={}) for c in d.pool]
            pool += [candidate(d.name, cache.authors[a], orgs, year) for a in extra[d.name]
                     if cache.authors.get(a) is not None]
            # a profile that is another (differently named) roster person's id is theirs
            pool = [c for c in pool if c.id in d.ids or not (owner.get(c.id, set()) - same)]
            for c in pool:
                attach_coauthors(c, idx, same | {canon[n] for n in same if n in canon})
            nd = dataclasses.replace(d, pool=pool, ids=list(d.ids), added=[], candidates=list(d.candidates))
            nd.candidates = [next((c for c in pool if c.id == x.id), x) for x in nd.candidates]
            new.append(decide_coauthors(nd))
        out = new
        accepted = {d.name: set(d.ids) for d in new if d.status == "resolved"}
        grown = {p: ids for p, ids in accepted.items() if not ids <= person_ids.get(p, set())}
        stats["rounds"].append({
            "round": rnd + 1, "ids_indexed": len(all_ids), "ids_without_works": len(unfetched),
            "works_used": idx.works_used,
            "works_cut": sum(1 for w in work.values() if w.get("trunc")),
            "authors_linked": sum(1 for a in idx.links if len(idx.links[a]) >= LINK_PEOPLE),
            "candidates_from_works": sum(len(v) for v in extra.values()),
            "resolved": sum(1 for d in new if d.status == "resolved"),
            "new_resolved": sum(1 for d in new if d.status == "resolved" and d.before != "resolved"),
            "splits_added": sum(len(d.added) for d in new), "stop": stop})
        log(f"coauthor round {rnd + 1}: {stats['rounds'][-1]}")
        if stop or not grown:
            break
        for p, ids in grown.items():
            person_ids.setdefault(p, set()).update(ids - bad)
    return out, stats


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def _md(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def cand_cell(c: Candidate) -> str:
    parts = [f"`{c.id}` {_md(c.name)}", f"{c.works} works", f"CS {c.cs_share:.0%}",
             f"AI {c.ai_share:.0%}", f"{c.recent} recent"]
    if c.institution:
        parts.append(_md(c.institution))
    if c.org:
        parts.append(f"org {c.org}")
    if c.orcid:
        parts.append(f"ORCID {c.orcid_id}" if c.orcid_id else "ORCID")
    if c.coauthors:
        parts.append(_md(_who(c, 4)))
    return "; ".join(parts)


RULE_NAMES = (
    f"candidate = name match (`names_match`); active = ≥ {MIN_WORKS} works and a work in the last "
    f"{RECENT_YEARS} years; one active candidate → resolved if adjacent-field share ≥ {ADJACENT_MIN:.0%} "
    f"or a configured institution; several → the only one at a configured institution (CS share ≥ "
    f"{CS_MIN:.0%}; for a common name a last known one), else, for a name that is not common, the top "
    f"one when it has ≥ {MIN_CS_WORKS} CS works, CS share ≥ {CS_MIN:.0%} and {DOMINANCE:g}x the next "
    f"one's CS works. Common = on the roster's `disambiguate` list or ≥ {COMMON_ACTIVE} active "
    "candidates. A profile named with a first initial only is not a candidate.")
RULE_COAUTHORS = (
    f"the works since {{floor}} of every resolved roster id, those with at most {MAX_AUTHORS} "
    "authorships; their name-matching authors join the candidates. Coauthor link = ≥ "
    f"{LINK_PEOPLE} distinct resolved roster coauthors over ≥ {LINK_WORKS} distinct works (the name "
    "itself and its roster namesakes do not count; roster spellings sharing an id are one person; a "
    "profile that is another roster person's id is theirs). A profile counts only when ≥ "
    f"{SHARED_MIN:.0%} of its works since {{floor}} are shared with roster people (else a merged "
    "namesake: it neither resolves nor blocks). An ambiguous or not-found name resolves (`coauthors`) "
    "when its coauthor-linked profiles are one person: the top one (most roster coauthors; for a "
    f"common name ≥ {COMMON_LINK_PEOPLE}) and its splits, each linked and sharing ≥ {SPLIT_SHARED} "
    f"roster coauthor with it (common name: ≥ {COMMON_SPLIT_SHARED}), or carrying its ORCID and "
    "sharing one; a linked profile left over, or one with a different ORCID, keeps the name "
    "ambiguous. A name with exactly one name-matching profile, not active, with CS share ≥ "
    f"{CS_MIN:.0%} or a configured institution, resolves (`single_inactive`). Resolved names gain "
    "splits by the same split rule; no id is removed.")


def _accepted(d: Decision) -> list[Candidate]:
    return [c for c in d.pool if c.id in d.ids and c not in d.added]


def check_rows(decisions: list[Decision]) -> list[Decision]:
    """Names the name rule resolved whose accepted profiles have no roster coauthor and an AI share
    below AI_MIN (not changed; for a human look)."""
    out = []
    for d in decisions:
        acc = _accepted(d) if d.status == "resolved" and d.before == "resolved" else []
        if acc and not d.added and all(not c.coauthors for c in acc) and max(c.ai_share for c in acc) < AI_MIN:
            out.append(d)
    return out


def report(decisions: list[Decision], *, meta: Mapping[str, Any]) -> str:
    """The markdown report (module docstring)."""
    by = Counter(d.status for d in decisions)
    how = Counter(d.how for d in decisions if d.status == "resolved")
    co = meta.get("coauthors")  # the coauthor rule ran: its statistics
    lines = [
        "# OpenAlex author ids for the researcher roster",
        "",
        (f"Generated {meta['date']} by `tools/resolve_openalex_authors.py` (round 5 (RUN) B7b, C4 of "
         "`docs/design/collection-yield.md`"
         + ("; coauthor evidence, T-ARX" if co else "") + "). For Tomáš: decide the ambiguous and "
         "not-found names below; the resolved ids are already in the roster."),
        "",
        (f"- Roster: `{meta['roster']}` version {meta['version_in']} → {meta['version_out']}; "
         f"{len(decisions)} names considered."),
        (f"- {meta['provenance']}" if meta.get("provenance") else
         f"- Requests: {meta['requests']}; OpenAlex cost {meta['cost']} (run `{meta['run_id']}`)."),
        f"- Rule (names): {RULE_NAMES} Details in the tool's docstring.",
    ]
    if co:
        lines.append(f"- Rule (coauthors): {RULE_COAUTHORS.format(floor=co['floor'])}")
        for r in co["rounds"]:
            lines.append(
                f"- Coauthor round {r['round']}: {r['ids_indexed']} resolved ids indexed "
                f"({r['ids_without_works']} without fetched works), {r['works_used']} works used, "
                f"{r['works_cut']} fetched works skipped (> {MAX_AUTHORS} authorships), "
                f"{r['candidates_from_works']} "
                f"candidates from works; {r['new_resolved']} names newly resolved, {r['splits_added']} "
                f"split ids added" + (f"; stopped: {r['stop']}" if r.get("stop") else "") + ".")
    lines.extend([
        ("- Candidate cells: `id` name; works; CS share and AI share (subfields 1702, 1707) of topic "
         f"counts; works in the last {RECENT_YEARS} years; last known institution; configured org; ORCID"
         + ("; resolved roster coauthors (most shared works first) and the distinct works shared."
            if co else " present.")),
        "",
        "## Summary",
        "",
    ])
    hows = ("orcid", "single", "org", "dominant", "coauthors", "single_inactive")
    if co:
        bef = Counter(d.before or d.status for d in decisions)
        bhow = Counter(d.how for d in decisions if (d.before or d.status) == "resolved")
        new = sum(1 for d in decisions if d.status == "resolved" and d.before != "resolved")
        added = [d for d in decisions if d.added and d.before == "resolved"]
        lines.extend([
            "| | Before (name rule) | After (+ coauthor rule) |",
            "|---|---|---|",
            f"| resolved (ids in the roster) | {bef['resolved']} | {by['resolved']} |",
            f"| of which newly resolved | | {new} |",
            *(f"| resolved: {h} | {bhow[h]} | {how[h]} |" for h in hows if how[h] or bhow[h]),
            f"| resolved names that gained split ids | | {len(added)} ({sum(len(d.added) for d in added)} ids) |",
            f"| ambiguous (decide) | {bef['ambiguous']} | {by['ambiguous']} |",
            f"| not found | {bef['not_found']} | {by['not_found']} |",
            (f"| not queried (budget) or error | {bef['not_queried'] + bef['error']} | "
             f"{by['not_queried'] + by['error']} |"),
            f"| ids removed by the merged-profile guard | | {sum(len(d.removed) for d in decisions)} |",
            f"| ids in the roster | {co['ids_before']} | {co['ids_after']} |",
            f"| roster names with several ids | {co['multi_before']} | {co['multi_after']} |",
            "",
        ])
    else:
        lines.extend([
            "| Status | Names |",
            "|---|---|",
            f"| resolved (ids in the roster) | {by['resolved']} |",
            *(f"| resolved: {h} | {how[h]} |" for h in hows if how[h]),
            f"| ambiguous (decide) | {by['ambiguous']} |",
            f"| not found | {by['not_found']} |",
            f"| not queried (budget) | {by['not_queried']} |",
            f"| error | {by['error']} |",
            "",
        ])

    def table(title: str, rows: list[Decision], note: str, *, cands: Callable[[Decision], list[Candidate]]
              | None = None, how_col: bool = False) -> None:
        lines.extend([f"## {title} ({len(rows)})", "", note, ""])
        if not rows:
            lines.extend(["None.", ""])
            return
        head = "| Name | C4 |" + (" How |" if how_col else "") + " Reason | Candidate 1 | Candidate 2 | Candidate 3 |"
        lines.extend([head, "|---|---|" + ("---|" if how_col else "") + "---|---|---|---|"])
        for d in rows:
            cs = (cands(d) if cands else d.candidates)[:3]
            cells = [cand_cell(c) for c in cs] + [""] * (3 - len(cs))
            lines.append(f"| {_md(d.name)} | {'yes' if d.disambiguate else ''} | "
                         + (f"{d.how} | " if how_col else "") + f"{_md(d.reason)} | "
                         + " | ".join(cells) + " |")
        lines.append("")

    pick = lambda st: [d for d in decisions if d.status == st]
    table("Ambiguous", pick("ambiguous"),
          "To accept a candidate, add its id to the roster entry (`- {name: \"…\", openalex_ids: [A…]}`); "
          "several ids are fine when they are one person's split profiles.")
    table("Not found", pick("not_found"),
          "No accepted OpenAlex profile with a matching name; the name is not swept by id.")
    if co:
        newly = [d for d in decisions if d.status == "resolved" and d.before != "resolved"]
        table("Newly resolved by the coauthor rule", newly,
              "Ids written to the roster in this run; candidates 1-3 are the accepted profiles (the "
              "ids column of the roster has them all).",
              cands=lambda d: [c for c in d.pool if c.id in d.ids], how_col=True)
        added = [d for d in decisions if d.added and d.before == "resolved"]
        lines.extend([f"## Split profiles added to resolved names ({len(added)})", "",
                      ("Same person by the split rule (shared roster coauthors or the same ORCID); "
                       "accepted profile first."), ""])
        if added:
            lines.extend(["| Name | How | Accepted | Added 1 | Added 2 |", "|---|---|---|---|---|"])
            for d in added:
                acc = _accepted(d)
                cells = [cand_cell(c) for c in d.added[:2]] + [""] * (2 - len(d.added[:2]))
                lines.append(f"| {_md(d.name)} | {d.how} | {cand_cell(acc[0]) if acc else ''} | "
                             + " | ".join(cells) + " |")
        else:
            lines.append("None.")
        lines.append("")
        chk = check_rows(decisions)
        lines.extend([f"## Check: resolved without roster coauthors and with a low AI share ({len(chk)})", "",
                      (f"Resolved by the name rule; the accepted profile shares no work since {co['floor']} "
                       f"with a resolved roster person and has an AI share below {AI_MIN:.0%}. Not "
                       "changed: a human look."), ""])
        if chk:
            lines.extend(["| Name | How | Accepted |", "|---|---|---|"])
            for d in chk:
                lines.append(f"| {_md(d.name)} | {d.how} | "
                             + "<br>".join(cand_cell(c) for c in _accepted(d)) + " |")
        else:
            lines.append("None.")
        lines.append("")
    gone = [d for d in decisions if d.removed]
    y = int(str(meta["date"])[:4])
    lines.extend([f"## Removed: merged profiles ({sum(len(d.removed) for d in gone)} ids, {len(gone)} names)", "",
                  (f"The guard: a profile with more than {MERGED_CAP} works in {y - 1} or {y}, or with ≥ "
                   f"{JUMP_MIN} works in {y} and ≥ {JUMP_FACTOR}x max(works in {y - 1}, {JUMP_FLOOR}), has "
                   "absorbed namesakes; its id is removed from the roster (old ids too) and is no coauthor "
                   "evidence. A name left without ids is ambiguous. Yearly works from `counts_by_year`; "
                   f"\"fetched since {y - 1}-10\": its works in the coauthor works cache dated from "
                   f"{y - 1}-10-01."), ""])
    if gone:
        lines.extend(["| Name | Now | Removed profile | Works " + f"{y - 2} / {y - 1} / {y}" + f" | Fetched since {y - 1}-10 |",
                      "|---|---|---|---|---|"])
        for d in gone:
            for c in d.removed:
                lines.append(f"| {_md(d.name)} | {d.status}{' (' + ', '.join(d.ids) + ')' if d.ids else ''} | "
                             f"`{c.id}` {_md(c.name)}; {c.works} works | {c.yearly[0]} / {c.yearly[1]} / "
                             f"{c.yearly[2]} | {c.works_window} |")
    else:
        lines.append("None.")
    lines.append("")
    spot = [d for d in decisions if d.status == "resolved" and (d.common or d.disambiguate)
            and d.before in ("", "resolved")]
    table("Resolved common names (spot-check)", spot,
          "Resolved names that are on the `disambiguate` list or common; the accepted id is candidate 1.")
    split = [d for d in decisions if d.status == "resolved" and d.split is not None
             and d.split.id not in d.ids]
    lines.extend([f"## Possible split profiles ({len(split)})", "",
                  ("Resolved names with a further profile of the same name that no rule added: a second "
                   "active CS profile (name rule), or a coauthor-linked one sharing the accepted "
                   f"profiles' roster coauthors but < {SHARED_MIN:.0%} of its works since {WORKS_FROM} "
                   "(coauthor rule). One person split by OpenAlex, or a namesake's profile holding some "
                   "of the person's works. Add the id when it is the same person."), ""])
    if split:
        lines.extend(["| Name | Accepted | Second profile |", "|---|---|---|"])
        for d in split:
            a = next((c for c in d.pool if c.id in d.ids), None) or next(
                (c for c in d.candidates if c.id in d.ids), None)
            assert d.split is not None
            sp = next((c for c in d.pool if c.id == d.split.id), d.split)
            lines.append(f"| {_md(d.name)} | {cand_cell(a) if a else ', '.join(d.ids)} | {cand_cell(sp)} |")
    else:
        lines.append("None.")
    lines.append("")
    if by["not_queried"] or by["error"]:
        rest = [d for d in decisions if d.status in ("not_queried", "error")]
        lines.extend([f"## Not queried or failed ({len(rest)})", "",
                      ", ".join(f"{_md(d.name)} ({d.status})" for d in rest), ""])
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Roster writer
# ─────────────────────────────────────────────────────────────────────────────
def _entry_line(indent: str, raw: Any) -> tuple[str, list[str], str | None] | None:
    """``(raw name, ids, orcid)`` of a ``names:`` list line's YAML value, else ``None``."""
    try:
        val = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if isinstance(val, str):
        return val, [], None
    if isinstance(val, dict) and "name" in val:
        return str(val["name"]), [str(i) for i in val.get("openalex_ids") or []], val.get("orcid")
    return None


def write_roster_text(text: str, accepted: Mapping[str, list[str]], version: str,
                      removed: Iterable[str] = ()) -> str:
    """``text`` with every ``names`` entry of a resolved name rewritten to ``{name, openalex_ids}``
    (existing ids merged; ``orcid`` kept), the ``removed`` ids (the merged-profile guard) dropped
    (an entry left without ids and ORCID becomes a bare name) and ``version`` set; nothing else
    changes."""
    drop = set(removed)
    out: list[str] = []
    in_names = False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        if body.startswith("version:"):
            out.append(f'version: "{version}"\n')
            continue
        if not body.startswith((" ", "-", "#")) and body.strip():
            in_names = body.startswith("names:")
        if in_names and body.startswith("  - "):
            parsed = _entry_line("  ", body[4:])
            if parsed is not None:
                raw, ids, orcid = parsed
                new = sorted((set(ids) | set(accepted.get(roster_name(raw), []))) - drop)
                if not new and ids and not orcid:
                    out.append(f"  - {json.dumps(raw, ensure_ascii=False)}\n")
                    continue
                if new != sorted(ids):
                    fields = [f"name: {json.dumps(raw, ensure_ascii=False)}"]
                    if new:
                        fields.append(f"openalex_ids: [{', '.join(new)}]")
                    if orcid:
                        fields.append(f"orcid: {orcid}")
                    out.append("  - {" + ", ".join(fields) + "}\n")
                    continue
        out.append(line)
    return "".join(out)


def write_roster(path: Path, accepted: Mapping[str, list[str]], version: str,
                 removed: Iterable[str] = ()) -> int:
    """Rewrite the roster at ``path`` (:func:`write_roster_text`), parse it back and check the
    names are unchanged, every accepted id is there and no removed one; returns the number of names
    with ids."""
    removed = set(removed)
    old_text = path.read_text("utf-8")
    old = parse_roster(yaml.safe_load(old_text), source=str(path))
    new_text = write_roster_text(old_text, accepted, version, removed)
    new = parse_roster(yaml.safe_load(new_text), source=f"{path} (rewritten)")
    if new.names != old.names or new.disambiguate != old.disambiguate:
        raise SystemExit("roster rewrite changed the names or the disambiguate list; not written")
    missing = {n: ids for n, ids in accepted.items() if not set(ids) <= set(new.ids_of(n))}
    if missing:
        raise SystemExit(f"roster rewrite lost ids for {sorted(missing)[:5]}…; not written")
    kept = {a for e in new.entries for a in e.openalex_ids} & removed
    if kept:
        raise SystemExit(f"roster rewrite kept removed ids {sorted(kept)[:5]}; not written")
    path.write_text(new_text, "utf-8")
    return sum(1 for e in new.entries if e.openalex_ids)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
class _MeterHost:
    """What :class:`OpenAlexMeter` reads of a collector: ``counts`` and ``ctx.run_id/stage``."""

    def __init__(self, run_id: str) -> None:
        self.counts: dict[str, int] = {}
        self.ctx = argparse.Namespace(run_id=run_id, stage="tools.resolve_openalex_authors")


def configured_orgs() -> dict[str, str]:
    """channels.yaml ``institutions``: OpenAlex institution id → org slug."""
    from stack.config import load

    return {i.id: i.org for i in load().channels.institutions()}


def main(argv: list[str] | None = None) -> int:
    from stack.llm import provider_key
    from stack.state import state_dir

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roster", type=Path, default=REPO / "configs" / "rosters" / "researchers.yaml")
    ap.add_argument("--names", help="comma-separated roster names (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="first N names after ordering (0 = all)")
    ap.add_argument("--max-usd", type=float, default=0.25, help="the run's OpenAlex cap (default 0.25)")
    ap.add_argument("--max-requests", type=int, default=0, help="0 = no request cap")
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--offline", action="store_true", help="decide from the cache only")
    ap.add_argument("--keyless", action="store_true", help="allow a run without OPENALEX_API_KEY")
    ap.add_argument("--report", type=Path, default=REPO / "docs" / "design" / "openalex-author-ids.md")
    ap.add_argument("--decisions", type=Path, default=None, help="also write every decision as JSON lines")
    ap.add_argument("--write-roster", action="store_true")
    ap.add_argument("--roster-version", default=None, help="the rewritten roster's version")
    ap.add_argument("--works-cache", type=Path, default=None,
                    help="the coauthor works cache (default <state dir>/openalex-author-works-cache.jsonl)")
    ap.add_argument("--rounds", type=int, default=1, help="coauthor rounds (2 also indexes round 1's ids)")
    ap.add_argument("--no-coauthors", action="store_true", help="the name rule only")
    ap.add_argument("--provenance", default=None,
                    help="replaces the report's requests line (an --offline re-decision names the live run)")
    args = ap.parse_args(argv)

    text = args.roster.read_text("utf-8")
    roster = parse_roster(yaml.safe_load(text), source=str(args.roster))
    names = ordered_names(roster, args.names.split(",") if args.names else None)
    if args.limit:
        names = names[:args.limit]
    cache = Cache(args.cache or state_dir() / "openalex-authors-cache.jsonl")
    run_id = f"resolve-oa-authors-{int(time.time())}"
    host = _MeterHost(run_id)
    if args.offline:
        client = Client(None)
    else:
        key = openalex_key()
        if not key and not args.keyless:
            raise SystemExit("no OPENALEX_API_KEY (environment or $STACK_ENV_FILE); --keyless to run "
                             "on the free daily budget")
        meter = OpenAlexMeter(host, key=key, max_requests=args.max_requests, max_usd=args.max_usd)  # type: ignore[arg-type]
        client = Client(meter.wrap(None), mailto=provider_key("STACK_MAILTO"))
    year = dt.datetime.now(dt.UTC).year
    log = lambda s: print(s, file=sys.stderr, flush=True)
    orgs = configured_orgs()
    decisions = resolve(roster, client, cache, orgs=orgs, year=year, names=names, log=log)
    co: dict[str, Any] | None = None
    wcache = None if args.no_coauthors else Cache(args.works_cache or state_dir() / "openalex-author-works-cache.jsonl")
    n_removed = apply_guard(decisions, roster, cache, orgs=orgs, year=year, wcache=wcache)
    if not args.no_coauthors:
        assert wcache is not None
        decisions, co = resolve_coauthors(decisions, roster, client, cache, wcache, orgs=orgs,
                                          year=year, rounds=args.rounds, log=log)
        co["floor"] = WORKS_FROM
        missing = co["rounds"][-1]["ids_without_works"]
        if missing:
            log(f"WARNING: {missing} resolved ids have no fetched works: their coauthor evidence is missing")
        n_removed += apply_guard(decisions, roster, cache, orgs=orgs, year=year, wcache=wcache)
        co.update(id_counts(roster, decisions))
    log(f"merged-profile guard: {n_removed} ids removed")
    cost = host.counts.get("openalex_cost_micro_usd", 0) / 1e6
    version_out = args.roster_version or roster.version
    meta = {"date": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "roster": args.roster.relative_to(REPO) if args.roster.is_relative_to(REPO) else args.roster,
            "version_in": roster.version, "version_out": version_out if args.write_roster else roster.version,
            "requests": ", ".join(f"{k} {v}" for k, v in sorted(client.requests.items())) or "none (cache)",
            "cost": f"${cost:.4f}", "run_id": run_id, "provenance": args.provenance,
            "coauthors": co}
    args.report.write_text(report(decisions, meta=meta) + "\n", "utf-8")
    if args.decisions:
        with args.decisions.open("w", encoding="utf-8") as fh:
            for d in decisions:
                row = dataclasses.asdict(d)
                row.pop("pool")
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    accepted = {d.name: d.ids for d in decisions if d.status == "resolved"}
    written = None
    if args.write_roster:
        if not args.roster_version:
            raise SystemExit("--write-roster needs --roster-version (the roster's version bump)")
        written = write_roster(args.roster, accepted, version_out,
                               removed={c.id for d in decisions for c in d.removed})
    summary = {"names": len(decisions), **Counter(d.status for d in decisions),
               "requests": dict(client.requests), "cost_usd": round(cost, 6), "run_id": run_id,
               "roster_names_with_ids": written, "report": str(args.report),
               "coauthor_rounds": (co or {}).get("rounds")}
    print(json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
