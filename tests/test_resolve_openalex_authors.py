"""``tools/resolve_openalex_authors.py`` (C4, round 5 (RUN) B7b): the acceptance rule, the report
format, the roster rewrite, and a run over ``tests/fakes/http.py`` through the OpenAlex meter (a
temporary ledger; no network).

The report sample ``tests/fixtures/openalex_authors/sample-report.md`` is the report of the fake
run below; :func:`test_report_matches_the_committed_sample` regenerates and compares it (set
``STACK_REGEN_FIXTURES=1`` to rewrite it after an intended format change).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
from typing import Any

import pytest
import yaml

#: a one-off tool's tests (run 2026-10-13, output in configs/rosters/): not the core tier (S25-97, L6-16)
pytestmark = pytest.mark.tools

from stack.collect.provider_keys import OpenAlexMeter
from stack.collect.roster import parse_roster
from stack.llm.ledger import Ledger
from tests.fakes.http import FakeHttp

REPO = pathlib.Path(__file__).resolve().parent.parent
SAMPLE = REPO / "tests" / "fixtures" / "openalex_authors" / "sample-report.md"


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_resolve_openalex_authors_under_test", REPO / "tools" / "resolve_openalex_authors.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


tool = _load_tool()
YEAR = 2026
ORGS = {"I4387930290": "anthropic", "I4210090411": "google-deepmind"}


def author(aid: str, name: str, *, works: int = 40, cs: int = 8, other: int = 2,
           other_field: str = "27", recent: int = 5, inst: str | None = None,
           inst_name: str = "Some University", alts: tuple[str, ...] = (),
           orcid: str | None = None) -> dict[str, Any]:
    """An OpenAlex author record in the shape the tool selects (field 27 = medicine)."""
    topic = lambda f, n: {"count": n, "field": {"id": f"https://openalex.org/fields/{f}"}}
    return {
        "id": f"https://openalex.org/{aid}", "display_name": name,
        "display_name_alternatives": list(alts), "orcid": orcid, "works_count": works,
        "topics": [topic("17", cs), topic(other_field, other)],
        "counts_by_year": [{"year": YEAR, "works_count": recent}, {"year": YEAR - 5, "works_count": 3}],
        "last_known_institutions": [{"id": f"https://openalex.org/{inst or 'I1'}",
                                     "display_name": inst_name}],
        "affiliations": [],
    }


def cand(rec: dict[str, Any], roster_name: str) -> Any:
    return tool.candidate(roster_name, rec, ORGS, YEAR)


# ─────────────────────────────────────────────────────────────────────────────
# (A) the rule, branch by branch
# ─────────────────────────────────────────────────────────────────────────────
def test_candidate_evidence() -> None:
    c = cand(author("A1", "Ethan Perez", works=77, cs=9, other=1, inst="I4387930290",
                    inst_name="Anthropic (United States)", orcid="https://orcid.org/0000-0001-2345-6789"),
             "Ethan Perez")
    assert (c.id, c.name_ok, c.works, round(c.cs_share, 2), c.recent, c.org, c.orcid) == (
        "A1", True, 77, 0.9, 5, "anthropic", True)
    assert c.active and round(c.cs_works, 1) == 69.3
    # a name variant counts; a first initial alone does not (merged namesakes), unless the roster's
    # name has one too; another first name does not
    assert not cand(author("A2", "E. Perez"), "Ethan Perez").name_ok
    assert cand(author("A2", "E. Perez", alts=("Ethan Perez",)), "Ethan Perez").name_ok
    assert cand(author("A2", "A. Cooper"), "A. Feder Cooper").name_ok
    assert cand(author("A3", "Ethel Perez-Smith", alts=("Ethan Perez",)), "Ethan Perez").name_ok
    assert not cand(author("A4", "Estefania Perez"), "Ethan Perez").name_ok
    # inactive: no work in the last RECENT_YEARS years
    assert not cand(author("A5", "Ethan Perez", recent=0), "Ethan Perez").active


def test_decide_single_active_match_needs_an_adjacent_field_or_an_org() -> None:
    ok = tool.decide("Ann Lee", [cand(author("A1", "Ann Lee"), "Ann Lee"),
                                 cand(author("A2", "Ann Lee", recent=0), "Ann Lee")])
    assert (ok.status, ok.ids, ok.how) == ("resolved", ["A1"], "single")
    med = cand(author("A3", "Ann Lee", cs=0, other=10), "Ann Lee")
    assert tool.decide("Ann Lee", [med]).status == "ambiguous"
    med_org = cand(author("A3", "Ann Lee", cs=0, other=10, inst="I4210090411"), "Ann Lee")
    assert tool.decide("Ann Lee", [med_org]).ids == ["A3"]


def test_decide_not_found() -> None:
    d = tool.decide("Ann Lee", [cand(author("A1", "Bob Lee"), "Ann Lee")])
    assert (d.status, d.ids) == ("not_found", []) and "no profile" in d.reason
    d = tool.decide("Ann Lee", [cand(author("A1", "Ann Lee", recent=0), "Ann Lee")])
    assert d.status == "not_found" and "none active" in d.reason


def test_decide_several_org_dominance_and_common_names() -> None:
    big = author("A1", "Yu Cheng", works=200, cs=9, other=1)
    small = author("A2", "Yu Cheng", works=30, cs=5, other=5)
    near = author("A3", "Yu Cheng", works=150, cs=9, other=1)
    at_org = author("A4", "Yu Cheng", works=20, cs=9, other=1, inst="I4387930290")
    c = lambda *recs: [cand(r, "Yu Cheng") for r in recs]
    d = tool.decide("Yu Cheng", c(big, small))
    assert (d.status, d.ids, d.how) == ("resolved", ["A1"], "dominant")
    assert d.split is not None and d.split.id == "A2"  # a second active CS profile: reported
    d = tool.decide("Yu Cheng", c(big, near))
    assert d.status == "ambiguous" and "within 3x" in d.reason
    # a common name is never resolved by dominance ...
    d = tool.decide("Yu Cheng", c(big, small), disambiguate=True)
    assert d.status == "ambiguous" and "common name" in d.reason
    # ... but the only active CS profile at a configured institution is, common or not
    d = tool.decide("Yu Cheng", c(big, near, at_org), disambiguate=True)
    assert (d.status, d.ids, d.how) == ("resolved", ["A4"], "org")
    assert d.candidates[0].id == "A4" and d.split is not None and d.split.id == "A1"
    # the ORCID route wins outright
    orc = cand(at_org, "Yu Cheng")
    assert tool.decide("Yu Cheng", c(big, near), disambiguate=True, orcid_hit=orc).ids == ["A4"]


def test_common_names_need_a_last_known_org_and_never_win_by_dominance() -> None:
    """Five or more active profiles make a name common (as the disambiguate list does): dominance
    no longer resolves it, and an org seen only among recent affiliations (merged namesakes span
    employers) does not either; one among the last known institutions does."""
    recs = [author(f"A{i}", "Bo Li", works=10, cs=9, other=1) for i in range(1, 5)]
    big = author("A9", "Bo Li", works=400, cs=9, other=1)
    assert tool.decide("Bo Li", [cand(r, "Bo Li") for r in [big, *recs[:3]]]).how == "dominant"
    d = tool.decide("Bo Li", [cand(r, "Bo Li") for r in [big, *recs]])
    assert d.status == "ambiguous" and d.common
    aff = author("A8", "Bo Li", works=30, cs=9, other=1)
    aff["affiliations"] = [{"institution": {"id": "https://openalex.org/I4387930290"}, "years": [YEAR]}]
    c_aff = cand(aff, "Bo Li")
    assert c_aff.org == "anthropic" and not c_aff.org_last
    assert tool.decide("Bo Li", [cand(r, "Bo Li") for r in [big, *recs]] + [c_aff]).status == "ambiguous"
    assert tool.decide("Bo Li", [cand(big, "Bo Li"), c_aff]).ids == ["A8"]  # not common: counts


def test_cache_survives_unicode_line_separators(tmp_path: pathlib.Path) -> None:
    """A display name may hold U+2028; the cache is split on newlines only."""
    c = tool.Cache(tmp_path / "c.jsonl")
    c.put("author", "A1", {"display_name": "Ann\u2028Lee"})
    c.put("name", "Ann Lee", {"autocomplete": ["A1"]})
    again = tool.Cache(tmp_path / "c.jsonl")
    assert again.authors["A1"]["display_name"] == "Ann\u2028Lee" and again.names["Ann Lee"]


# ─────────────────────────────────────────────────────────────────────────────
# (B) roster rewrite
# ─────────────────────────────────────────────────────────────────────────────
ROSTER = '''# a comment that must survive
version: "1.1.0"
disambiguate:
  - "Yu Cheng"
names:
  - "Ann Lee"
  # an inline comment
  - "Lee, Ann"
  - {name: "Bo Chen", openalex_ids: [A9]}
  - "Yu Cheng"
  - "Zed Unresolved"
'''


def test_write_roster_text_rewrites_only_resolved_entries() -> None:
    out = tool.write_roster_text(ROSTER, {"Ann Lee": ["A1"], "Bo Chen": ["A2"], "Yu Cheng": []},
                                 "1.2.0")
    assert out.splitlines() == [
        "# a comment that must survive", 'version: "1.2.0"', "disambiguate:", '  - "Yu Cheng"',
        "names:", '  - {name: "Ann Lee", openalex_ids: [A1]}', "  # an inline comment",
        '  - {name: "Lee, Ann", openalex_ids: [A1]}', '  - {name: "Bo Chen", openalex_ids: [A2, A9]}',
        '  - "Yu Cheng"', '  - "Zed Unresolved"']
    r = parse_roster(yaml.safe_load(out))
    assert r.version == "1.2.0" and r.ids_of("Ann Lee") == ("A1",) and r.ids_of("Bo Chen") == ("A2", "A9")
    assert r.names == parse_roster(yaml.safe_load(ROSTER)).names


def test_write_roster_checks_before_replacing(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text(ROSTER, "utf-8")
    assert tool.write_roster(p, {"Ann Lee": ["A1"]}, "1.2.0") == 2  # Ann Lee and Bo Chen
    p.write_text(ROSTER, "utf-8")
    with pytest.raises(SystemExit, match="lost ids"):
        tool.write_roster(p, {"Not On Roster": ["A1"]}, "1.2.0")
    assert p.read_text("utf-8") == ROSTER  # refused: untouched


# ─────────────────────────────────────────────────────────────────────────────
# (C) a run over a fake transport, through the meter; the report sample
# ─────────────────────────────────────────────────────────────────────────────
API = "https://api.openalex.org"
FAKE_ROSTER = '''version: "1.1.0"
disambiguate:
  - "Yu Cheng"
names:
  - "Ann Lee"
  - "Bo Chen"
  - "Cy Dorn"
  - "Di Evans"
  - "Yu Cheng"
  - {name: "Ed Fox", orcid: 0000-0001-2345-6789}
'''
RECS = {
    "A101": author("A101", "Ann Lee", works=60, cs=9, other=1, inst="I4387930290",
                   inst_name="Anthropic (United States)"),
    "A102": author("A102", "Ann Lee", works=3, recent=0),
    "A201": author("A201", "Bo Chen", works=90, cs=8, other=2),
    "A202": author("A202", "Bo Chen", works=80, cs=8, other=2, inst_name="Tsinghua University"),
    "A301": author("A301", "Cy Dorn", works=40, cs=0, other=10, inst_name="Hospital X"),
    "A501": author("A501", "Yu Cheng", works=300, cs=9, other=1, inst_name="Univ A"),
    "A502": author("A502", "Yu Cheng", works=50, cs=9, other=1, inst_name="Univ B"),
    "A503": author("A503", "Yu | Cheng", works=10, cs=5, other=5, inst_name="Univ C"),
    "A601": author("A601", "Ed Fox", works=12, orcid="https://orcid.org/0000-0001-2345-6789"),
}


def _fake_openalex(recs: dict[str, Any] | None = None) -> FakeHttp:
    recs = recs or RECS  # the module's records, or a variant
    fake = FakeHttp()
    ac = lambda *ids: {"results": [{"id": f"https://openalex.org/{i}"} for i in ids]}
    fake.add(f"{API}/autocomplete/authors", {"q": "Yu Cheng"}, json=ac("A501", "A502"))
    fake.add(f"{API}/authors", {"search": "Yu Cheng", "per_page": tool.SEARCH_PER_PAGE,
                                "select": tool.SELECT},
             json={"results": [recs["A501"], recs["A502"], recs["A503"]]})
    fake.add(f"{API}/autocomplete/authors", {"q": "Ann Lee"}, json=ac("A101", "A102"))
    fake.add(f"{API}/autocomplete/authors", {"q": "Bo Chen"}, json=ac("A201", "A202"))
    fake.add(f"{API}/autocomplete/authors", {"q": "Cy Dorn"}, json=ac("A301"))
    fake.add(f"{API}/autocomplete/authors", {"q": "Di Evans"}, json=ac())
    fake.add(f"{API}/authors", {"filter": "orcid:0000-0001-2345-6789", "select": tool.SELECT},
             json={"results": [recs["A601"]]})
    fake.add(f"{API}/autocomplete/authors", {"q": "Ed Fox"}, json=ac("A601"))
    ids = ["A101", "A102", "A201", "A202", "A301"]
    fake.add(f"{API}/authors", {"filter": "id:" + "|".join(ids), "per_page": len(ids),
                                "select": tool.SELECT},
             json={"results": [recs[i] for i in ids if i != "A102"]})  # A102: not served by id
    return fake


class _Host:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ctx = type("Ctx", (), {"run_id": "resolve-test", "stage": "tools.resolve_openalex_authors"})()


def _run(tmp_path: pathlib.Path, fake: FakeHttp, **meter: Any) -> tuple[list[Any], Ledger, _Host]:
    roster = parse_roster(yaml.safe_load(FAKE_ROSTER))
    ledger = Ledger(cap_usd=10.0)
    host = _Host()
    m = OpenAlexMeter(host, key="test-oa-key", ledger=ledger, **meter)  # type: ignore[arg-type]
    client = tool.Client(m.wrap(fake), sleep=lambda s: None)
    cache = tool.Cache(tmp_path / "cache.jsonl")
    out = tool.resolve(roster, client, cache, orgs=ORGS, year=YEAR,
                       names=tool.ordered_names(roster))
    return out, ledger, host


def test_fake_run_through_the_meter(tmp_path: pathlib.Path) -> None:
    fake = _fake_openalex()
    decisions, ledger, host = _run(tmp_path, fake)
    got = {d.name: (d.status, d.ids, d.how) for d in decisions}
    assert decisions[0].name == "Yu Cheng"  # disambiguate names first
    assert got == {
        "Yu Cheng": ("ambiguous", [], ""),           # common: dominance is not enough
        "Ann Lee": ("resolved", ["A101"], "single"),  # A102 inactive (and not served by id)
        "Bo Chen": ("ambiguous", [], ""),             # two active CS profiles within 3x
        "Cy Dorn": ("ambiguous", [], ""),             # only match is outside AI-adjacent fields
        "Di Evans": ("not_found", [], ""),
        "Ed Fox": ("resolved", ["A601"], "orcid")}
    # every request carried the key as a bearer header and went through the ledger
    assert all(c.headers.get("Authorization") == "Bearer test-oa-key" for c in fake.calls)
    assert all("test-oa-key" not in c.url for c in fake.calls)
    assert host.counts["openalex_keyed_requests"] == len(fake.calls) == 9
    assert len([ln for ln in ledger.lines() if ln["kind"] == "reserve"]) == 9
    # a second run decides from the cache: no request
    fake2 = FakeHttp()
    again, _, _ = _run(tmp_path, fake2)
    assert fake2.calls == [] and [(d.name, d.status) for d in again] == [
        (d.name, d.status) for d in decisions]


def test_budget_stop_leaves_the_rest_not_queried(tmp_path: pathlib.Path) -> None:
    decisions, _, host = _run(tmp_path, _fake_openalex(), max_requests=3)
    st = {d.name: d.status for d in decisions}
    assert host.counts["openalex_budget_stop_max_requests"] == 1
    assert st["Yu Cheng"] == "ambiguous"  # autocomplete + search: answered before the stop
    assert {st[n] for n in ("Bo Chen", "Cy Dorn", "Di Evans", "Ed Fox")} == {"not_queried"}


def test_report_matches_the_committed_sample(tmp_path: pathlib.Path) -> None:
    decisions, _, _ = _run(tmp_path, _fake_openalex())
    decisions, co = _coauthor_run(tmp_path, decisions, _coauthor_fake())
    co.update(floor=tool.WORKS_FROM, **tool.id_counts(parse_roster(yaml.safe_load(FAKE_ROSTER)), decisions))
    meta = {"date": "2026-10-13 00:00 UTC", "roster": "tests (fake roster)", "version_in": "1.1.0",
            "version_out": "1.3.0", "requests": "autocomplete 6, details 2, orcid 1, search 1, works 1",
            "cost": "$0.0018", "run_id": "resolve-test", "coauthors": co}
    text = tool.report(decisions, meta=meta) + "\n"
    if os.environ.get("STACK_REGEN_FIXTURES") == "1":
        SAMPLE.parent.mkdir(parents=True, exist_ok=True)
        SAMPLE.write_text(text, "utf-8")
    assert text == SAMPLE.read_text("utf-8")
    # the format's load-bearing parts, independent of the sample's wording
    assert "| ambiguous (decide) | 3 | 2 |" in text and "| not found | 1 | 1 |" in text
    assert "| resolved: coauthors | 0 | 1 |" in text and "## Newly resolved by the coauthor rule (1)" in text
    assert "Yu \\| Cheng" in text  # a pipe in a display name is escaped, the table stays intact
    cells = None  # every table row has its header's cell count
    for line in text.splitlines():
        if not line.startswith("|"):
            cells = None
            continue
        n = line.replace("\\|", "").count("|")
        cells = cells or n
        assert n == cells, line


# ─────────────────────────────────────────────────────────────────────────────
# (D) coauthor evidence: the works fetch and cache, the index, the coauthor rule
# ─────────────────────────────────────────────────────────────────────────────
def work(wid: str, *authors: tuple[str, str], extra: int = 0, trunc: bool | None = None) -> dict[str, Any]:
    """An OpenAlex work in the shape the tool selects; ``extra`` filler authorships."""
    auth = [{"author": {"id": f"https://openalex.org/{a}", "display_name": n}, "raw_author_name": n}
            for a, n in authors]
    auth += [{"author": {"id": f"https://openalex.org/AX{wid}{i}", "display_name": f"Filler {i}"},
              "raw_author_name": f"Filler {i}"} for i in range(extra)]
    w = {"id": f"https://openalex.org/{wid}", "publication_date": "2024-01-01", "title": f"T {wid}",
         "authorships": auth}
    if trunc is not None:
        w["is_authors_truncated"] = trunc
    return w


def test_index_links_coauthors_and_skips_cut_works() -> None:
    people = {"P One": {"A1"}, "P Two": {"A2"}, "P Three": {"A3"}}
    works = [tool.reduce_work(w) for w in [
        work("W1", ("A1", "P One"), ("A2", "P Two"), ("A9", "Ann Lee")),
        work("W2", ("A1", "P One"), ("A3", "P Three"), ("A9", "A. Lee"), ("A8", "Ann Lee")),
        work("W3", ("A2", "P Two"), ("A8", "Ann Lee"), extra=tool.MAX_AUTHORS),   # too many authors
        work("W4", ("A2", "P Two"), ("A8", "Ann Lee"), trunc=True),               # cut authorships
        work("W1", ("A1", "P One"), ("A2", "P Two"), ("A9", "Ann Lee")),          # a duplicate
    ]]
    assert works[2]["trunc"] and works[2]["authors"] == [] and works[2]["n"] == tool.MAX_AUTHORS + 2
    assert works[3]["trunc"]
    idx = tool.build_index(works, people)
    assert (idx.works_used, idx.works_skipped) == (2, 2)
    assert {p: sorted(ws) for p, ws in idx.links["A9"].items()} == {
        "P One": ["W1", "W2"], "P Two": ["W1"], "P Three": ["W2"]}
    assert set(idx.links["A8"]) == {"P One", "P Three"}  # W3, W4 skipped
    assert set(idx.links["A1"]) == {"P Two", "P Three"}  # never linked to its own person
    assert tool.works_candidates(idx, "Ann Lee") == {"A8", "A9"}
    c = cand(author("A9", "Ann Lee"), "Ann Lee")
    tool.attach_coauthors(c, idx, {"Ann Lee", "P Three"})  # an excluded namesake does not count
    assert (c.coauthors, c.coauthor_works, c.linked) == ({"P One": 2, "P Two": 1}, 2, True)


def test_spelling_twins_are_one_coauthor() -> None:
    """Roster names sharing an id (``Florian Tramer`` / ``Florian Tramèr``) count as one person."""
    merged, canon = tool.merge_people({"Flo Tramer": {"A1"}, "Flo Tramèr": {"A1", "A2"}, "P Two": {"A3"}})
    assert merged == {"Flo Tramer / Flo Tramèr": {"A1", "A2"}, "P Two": {"A3"}}
    assert canon["Flo Tramèr"] == canon["Flo Tramer"] == "Flo Tramer / Flo Tramèr"
    works = [tool.reduce_work(work(w, ("A1", "Flo Tramer"), ("A9", "Ann Lee"))) for w in ("W1", "W2")]
    c = cand(author("A9", "Ann Lee"), "Ann Lee")
    tool.attach_coauthors(c, tool.build_index(works, merged), {"Ann Lee"})
    assert not c.linked and c.coauthors == {"Flo Tramer / Flo Tramèr": 2}


def test_resolve_coauthors_counts_spelling_twins_once(tmp_path: pathlib.Path) -> None:
    """In the run: two works with the twins' shared id are one roster coauthor, not a link."""
    roster = parse_roster({"version": "1", "names": [{"name": "Flo Tramer", "openalex_ids": ["A1"]},
                                                     {"name": "Flo Tramèr", "openalex_ids": ["A1"]},
                                                     "Ann Lee"]})
    wc, cache = tool.Cache(None), tool.Cache(None)
    for w in ("W1", "W2"):
        wc.put("work", w, tool.reduce_work(work(w, ("A1", "Flo Tramer"), ("A9", "Ann Lee"))))
    wc.put("works_of", "A1", ["W1", "W2"])
    cache.put("author", "A9", author("A9", "Ann Lee", works=2))
    d = tool.Decision("Ann Lee", "not_found", [], "x", pool=[])
    out, _ = tool.resolve_coauthors([d], roster, tool.Client(None), cache, wc, orgs=ORGS, year=YEAR)
    c = next(c for c in out[0].pool if c.id == "A9")
    assert c.coauthors == {"Flo Tramer / Flo Tramèr": 2} and not c.linked and out[0].status == "not_found"


def test_fetch_works_pages_caches_and_reruns_offline(tmp_path: pathlib.Path) -> None:
    fake = FakeHttp()
    base = {"filter": "authorships.author.id:A1|A2,from_publication_date:2019-01-01",
            "select": tool.WORKS_SELECT, "per_page": tool.WORKS_PER_PAGE}
    fake.add(f"{API}/works", {**base, "cursor": "*"},
             json={"meta": {"next_cursor": "c2"}, "results": [work("W1", ("A1", "P One"), ("A9", "X"))]})
    fake.add(f"{API}/works", {**base, "cursor": "c2"},
             json={"meta": {"next_cursor": "c3"}, "results": [work("W2", ("A2", "P Two"))]})
    fake.add(f"{API}/works", {**base, "cursor": "c3"}, json={"meta": {"next_cursor": None}, "results": []})
    ledger = Ledger(cap_usd=10.0)
    m = OpenAlexMeter(_Host(), key="test-oa-key", ledger=ledger)  # type: ignore[arg-type]
    wc = tool.Cache(tmp_path / "works.jsonl")
    assert tool.fetch_works(tool.Client(m.wrap(fake), sleep=lambda s: None), wc, ["A2", "A1"]) is None
    assert len(fake.calls) == 3 and len([ln for ln in ledger.lines() if ln["kind"] == "reserve"]) == 3
    again = tool.Cache(tmp_path / "works.jsonl")
    assert again.kinds["works_of"] == {"A1": ["W1"], "A2": ["W2"]} and set(again.kinds["work"]) == {"W1", "W2"}
    # cached: no request even offline; a missing id offline is reported, not silently skipped
    assert tool.fetch_works(tool.Client(None), again, ["A1", "A2"]) is None
    assert tool.fetch_works(tool.Client(None), again, ["A1", "A3"]) == "offline"


def linked(aid: str, name: str, coauthors: dict[str, int], *, works: int | None = None,
           since: int | None = None, orcid: str | None = None, **kw: Any) -> Any:
    """A candidate with coauthor evidence; ``since`` (its works since WORKS_FROM) defaults to the
    shared works (all its recent output is with roster people)."""
    c = cand(author(aid, name, orcid=f"https://orcid.org/{orcid}" if orcid else None, **kw), name)
    c.coauthors = dict(coauthors)
    c.coauthor_works = works if works is not None else max([*coauthors.values(), 0])
    c.works_since = since if since is not None else c.coauthor_works
    return c


def _undecided(name: str, pool: list[Any], *, status: str = "ambiguous", common: bool = False) -> Any:
    return tool.Decision(name, status, [], "name rule", pool=pool, common=common, candidates=pool[:3])


def test_coauthor_rule_unique_linked_profile_and_its_splits() -> None:
    top = linked("A1", "Jo Ma", {"P1": 3, "P2": 2, "P3": 1}, works=4, cs=0, other=10)  # CS 0%: coauthors see it
    split = linked("A2", "Jo Ma", {"P2": 1, "P4": 1}, works=2)
    weak = linked("A3", "Jo Ma", {"P9": 1}, works=1)       # one roster coauthor: neither joins nor blocks
    d = tool.decide_coauthors(_undecided("Jo Ma", [top, split, weak, cand(author("A4", "Jo Ma"), "Jo Ma")]))
    assert (d.status, d.ids, d.how, d.before) == ("resolved", ["A1", "A2"], "coauthors", "ambiguous")
    assert [c.id for c in d.added] == ["A2"] and "3 roster coauthors (P1, P2, P3)" in d.reason
    # not found by the name rule (no active profile): the coauthor rule still resolves it
    d = tool.decide_coauthors(_undecided("Jo Ma", [linked("A1", "Jo Ma", {"P1": 1, "P2": 1}, works=2, recent=0)],
                                         status="not_found"))
    assert (d.status, d.ids) == ("resolved", ["A1"])
    # a coauthor link needs two works and two people
    assert not linked("A1", "Jo Ma", {"P1": 1, "P2": 1}, works=1).linked
    assert not linked("A1", "Jo Ma", {"P1": 5}, works=5).linked


def test_coauthor_rule_disjoint_or_orcid_conflicting_profiles_stay_ambiguous() -> None:
    a = linked("A1", "Jo Ma", {"P1": 2, "P2": 2}, works=3)
    b = linked("A2", "Jo Ma", {"P3": 2, "P4": 1}, works=2)
    d = tool.decide_coauthors(_undecided("Jo Ma", [a, b]))
    assert d.status == "ambiguous" and d.ids == [] and "not one person" in d.reason and "0 shared" in d.reason
    assert [c.id for c in d.candidates[:2]] == ["A1", "A2"]
    # shared coauthors but different ORCIDs: two people, never splits
    a = linked("A1", "Jo Ma", {"P1": 2, "P2": 2}, works=3, orcid="0000-0001-0000-0001")
    b = linked("A2", "Jo Ma", {"P1": 1, "P2": 1}, works=2, orcid="0000-0001-0000-0002")
    d = tool.decide_coauthors(_undecided("Jo Ma", [a, b]))
    assert d.status == "ambiguous" and "different ORCID" in d.reason
    # the same ORCID is one person: it joins with one shared roster coauthor, unlinked
    c = linked("A3", "Jo Ma", {"P1": 1}, works=1, orcid="0000-0001-0000-0001")
    d = tool.decide_coauthors(_undecided("Jo Ma", [a, c]))
    assert (d.status, d.ids) == ("resolved", ["A1", "A3"]) and not c.linked
    # ... but not without one (the ORCID alone joined unrelated profiles in the live data)
    c = linked("A3", "Jo Ma", {"P9": 2}, works=2, orcid="0000-0001-0000-0001")
    assert c.relevant and tool.decide_coauthors(_undecided("Jo Ma", [a, c])).ids == ["A1"]


def test_coauthor_rule_common_name_thresholds() -> None:
    two = linked("A1", "Hao Li", {"P1": 2, "P2": 2}, works=3)
    d = tool.decide_coauthors(_undecided("Hao Li", [two], common=True))
    assert d.status == "ambiguous" and "fewer than 3 roster coauthors" in d.reason
    top = linked("A1", "Hao Li", {"P1": 2, "P2": 2, "P3": 1}, works=4)
    one_shared = linked("A2", "Hao Li", {"P1": 1, "P7": 1}, works=2)
    two_shared = linked("A3", "Hao Li", {"P1": 1, "P2": 1}, works=2)
    assert tool.decide_coauthors(_undecided("Hao Li", [top, two_shared], common=True)).ids == ["A1", "A3"]
    # one shared coauthor is a split for a rare name, not for a common one (then: ambiguous)
    assert tool.decide_coauthors(_undecided("Hao Li", [top, one_shared])).ids == ["A1", "A2"]
    d = tool.decide_coauthors(_undecided("Hao Li", [top, one_shared], common=True))
    assert d.status == "ambiguous" and "1 shared roster coauthor(s) (a split needs 2)" in d.reason


def test_merged_namesake_profiles_neither_resolve_nor_block() -> None:
    """A profile holding a few of the person's works and a namesake's large output (shared fraction
    below SHARED_MIN) is not accepted, does not make the name ambiguous, and is named in the reason;
    sharing the accepted profile's coauthors it is reported as a possible split."""
    big = linked("A1", "Jo Ma", {"P1": 2, "P2": 2, "P3": 1}, works=4, since=300)  # 4 of 300
    d = tool.decide_coauthors(_undecided("Jo Ma", [big]))
    assert d.status == "ambiguous" and "`A1`" in d.reason and "300 works since" in d.reason
    small = linked("A2", "Jo Ma", {"P1": 1, "P4": 1}, works=2, since=6)       # 2 of 6
    assert small.relevant and not big.relevant
    d = tool.decide_coauthors(_undecided("Jo Ma", [big, small]))
    assert (d.status, d.ids) == ("resolved", ["A2"]) and d.split is not None and d.split.id == "A1"
    # exactly a third counts
    assert linked("A3", "Jo Ma", {"P1": 1, "P2": 1}, works=2, since=6).relevant


def test_single_inactive_and_splits_of_resolved_names() -> None:
    lone = cand(author("A1", "Abe Dee", works=17, cs=9, other=1, recent=0), "Abe Dee")
    d = tool.decide_coauthors(_undecided("Abe Dee", [lone], status="not_found"))
    assert (d.status, d.ids, d.how) == ("resolved", ["A1"], "single_inactive")
    # a team's name is no person's ("The Sora team" matched "The Omnilingual MT Team" in the live data)
    team = cand(author("A1", "The Omnilingual MT Team", works=1, recent=0), "The Sora team")
    assert team.name_ok and tool.decide_coauthors(_undecided("The Sora team", [team], status="not_found")).ids == []
    med = cand(author("A1", "Abe Dee", works=17, cs=0, other=10, recent=0), "Abe Dee")
    assert tool.decide_coauthors(_undecided("Abe Dee", [med], status="not_found")).status == "not_found"
    two = [lone, cand(author("A2", "Abe Dee", recent=0), "Abe Dee")]
    assert tool.decide_coauthors(_undecided("Abe Dee", two, status="not_found")).status == "not_found"
    # a resolved name gains a split under the split rule and keeps its id; a rival is not added
    acc = linked("A1", "Ann Lee", {"P1": 2, "P2": 1}, works=3)
    sp = linked("A2", "Ann Lee", {"P1": 1, "P3": 1}, works=2)
    rival = linked("A3", "Ann Lee", {"P8": 1, "P9": 1}, works=2)
    d = tool.Decision("Ann Lee", "resolved", ["A1"], "x", how="single", pool=[acc, sp, rival])
    d = tool.decide_coauthors(d)
    assert (d.ids, [c.id for c in d.added], d.how, d.before) == (["A1", "A2"], ["A2"], "single", "resolved")


def _coauthor_fake() -> FakeHttp:
    """The works of the fake run's resolved ids (Ann Lee A101, Ed Fox A601): two works link a Bo
    Chen profile no name search found (A203) to both; one 120-author work is skipped."""
    fake = FakeHttp()
    flt = "authorships.author.id:A101|A601,from_publication_date:2019-01-01"
    fake.add(f"{API}/works", {"filter": flt, "select": tool.WORKS_SELECT, "per_page": tool.WORKS_PER_PAGE,
                              "cursor": "*"},
             json={"meta": {"next_cursor": None}, "results": [
                 work("W1", ("A101", "Ann Lee"), ("A601", "Ed Fox"), ("A203", "Bo Chen")),
                 work("W2", ("A101", "Ann Lee"), ("A601", "Ed Fox"), ("A203", "Bo Chen")),
                 work("W3", ("A101", "Ann Lee"), ("A601", "Ed Fox"), ("A301", "Cy Dorn"), extra=120)]})
    rec = author("A203", "Bo Chen", works=5)
    rec["counts_by_year"] = [{"year": YEAR, "works_count": 3}]  # 2 of its 3 works since 2019 shared
    fake.add(f"{API}/authors", {"filter": "id:A203", "per_page": 1, "select": tool.SELECT},
             json={"results": [rec]})
    return fake


def _coauthor_run(tmp_path: pathlib.Path, decisions: list[Any], fake: FakeHttp | None) -> tuple[list[Any], dict[str, Any]]:
    roster = parse_roster(yaml.safe_load(FAKE_ROSTER))
    if fake is None:
        client = tool.Client(None)
    else:
        m = OpenAlexMeter(_Host(), key="test-oa-key", ledger=Ledger(cap_usd=10.0))  # type: ignore[arg-type]
        client = tool.Client(m.wrap(fake), sleep=lambda s: None)
    return tool.resolve_coauthors(decisions, roster, client, tool.Cache(tmp_path / "cache.jsonl"),
                                  tool.Cache(tmp_path / "works.jsonl"), orgs=ORGS, year=YEAR)


def test_resolve_coauthors_over_the_fake_run(tmp_path: pathlib.Path) -> None:
    """End to end on the fake run: the works-only Bo Chen profile is fetched and accepted; the
    offline re-run makes no request and decides the same."""
    decisions, _, _ = _run(tmp_path, _fake_openalex())
    fake = _coauthor_fake()
    out, stats = _coauthor_run(tmp_path, decisions, fake)
    got = {d.name: (d.status, d.ids, d.how) for d in out}
    assert got["Bo Chen"] == ("resolved", ["A203"], "coauthors")
    assert got["Ann Lee"] == ("resolved", ["A101"], "single") and got["Cy Dorn"][0] == "ambiguous"
    assert len(fake.calls) == 2 and stats["rounds"][0]["new_resolved"] == 1
    assert stats["rounds"][0]["works_used"] == 2 and stats["rounds"][0]["works_cut"] == 1
    off, _ = _coauthor_run(tmp_path, decisions, None)
    assert [(d.name, d.status, d.ids) for d in off] == [(d.name, d.status, d.ids) for d in out]


# ─────────────────────────────────────────────────────────────────────────────
# (E) the merged-profile guard
# ─────────────────────────────────────────────────────────────────────────────
def yearly(aid: str, y2: int, y1: int, y0: int, name: str = "Ann Lee") -> dict[str, Any]:
    rec = author(aid, name)
    rec["counts_by_year"] = [{"year": YEAR - 2, "works_count": y2}, {"year": YEAR - 1, "works_count": y1},
                             {"year": YEAR, "works_count": y0}]
    return rec


@pytest.mark.parametrize(("counts", "merged"), [
    ((64, 221, 2571), True),    # roster "Jian Yang": a profile that absorbed namesakes
    ((1, 7, 183), True),
    ((2, 3, 69), True),         # a current-year jump below the cap
    ((74, 69, 88), False),      # Yoshua Bengio: steady and prolific
    ((19, 29, 82), False),      # Xiangxiang Chu: growing, < 4x
    ((0, 0, 27), False),        # a new profile, below JUMP_MIN
    ((52, 101, 7), True),       # the previous year above the cap
    ((52, 100, 7), False),      # the cap itself is allowed
    ((0, 10, 40), True),        # the jump's edges are inclusive: 40 >= JUMP_MIN and >= 4 x 10
    ((0, 10, 39), False),
])
def test_merged_profile_thresholds(counts: tuple[int, int, int], merged: bool) -> None:
    c = cand(yearly("A1", *counts), "Ann Lee")
    assert c.yearly == counts and c.merged_profile is merged
    assert tool.is_merged(yearly("A1", *counts), YEAR) is merged


def test_guard_removes_merged_ids_old_and_new(tmp_path: pathlib.Path) -> None:
    """Every accepted id, the roster's included: a merged one leaves; a name left without ids is
    ambiguous ("merged profile"); one with another id stays resolved; the roster rewrite drops the
    id (a bare name when none is left)."""
    roster_text = ROSTER.replace('"Ann Lee"', '{name: "Ann Lee", openalex_ids: [A7]}', 1)
    roster = parse_roster(yaml.safe_load(roster_text))
    cache = tool.Cache(None)
    cache.put("author", "A7", yearly("A7", 64, 221, 2571))       # old roster id, exploding
    cache.put("author", "A1", yearly("A1", 74, 69, 88))          # steady prolific
    cache.put("author", "A2", yearly("A2", 1, 7, 183, "Bo Chen"))
    cache.put("author", "A9", yearly("A9", 3, 3, 3, "Bo Chen"))
    ds = [tool.Decision("Ann Lee", "resolved", ["A1"], "x", how="single"),
          tool.Decision("Bo Chen", "resolved", ["A2"], "x", how="single"),
          tool.Decision("Yu Cheng", "resolved", ["A2", "A9"], "x", how="org")]
    assert tool.apply_guard(ds, roster, cache, orgs=ORGS, year=YEAR) == 3
    got = {d.name: (d.status, d.ids, [c.id for c in d.removed]) for d in ds}
    assert got == {"Ann Lee": ("resolved", ["A1"], ["A7"]), "Bo Chen": ("ambiguous", [], ["A2"]),
                   "Yu Cheng": ("resolved", ["A9"], ["A2"])}
    assert ds[1].reason.startswith("merged profile: `A2`") and "1 / 7 / 183" in ds[1].reason
    assert ds[1].before == "resolved"
    assert tool.apply_guard(ds, roster, cache, orgs=ORGS, year=YEAR) == 0  # idempotent
    p = tmp_path / "r.yaml"
    p.write_text(roster_text, "utf-8")
    tool.write_roster(p, {d.name: d.ids for d in ds if d.status == "resolved"}, "1.3.1",
                      removed={c.id for d in ds for c in d.removed})
    r = parse_roster(yaml.safe_load(p.read_text("utf-8")))
    assert r.ids_of("Ann Lee") == ("A1",) and r.ids_of("Bo Chen") == ("A9",) and r.ids_of("Yu Cheng") == ("A9",)
    out = tool.write_roster_text('names:\n  - {name: "Ann Lee", openalex_ids: [A7]}\n', {}, "1", ["A7"])
    assert out == 'names:\n  - "Ann Lee"\n'


def test_guard_over_the_fake_run(tmp_path: pathlib.Path) -> None:
    """Through the fake transport: Ann Lee's only profile explodes this year; the guard drops it
    before the coauthor rule, whose works query then asks for Ed Fox's id only."""
    recs = dict(RECS)
    recs["A101"] = {**RECS["A101"], "counts_by_year": [{"year": YEAR - 1, "works_count": 3},
                                                      {"year": YEAR, "works_count": 150}]}
    decisions, _, _ = _run(tmp_path, _fake_openalex(recs))
    roster = parse_roster(yaml.safe_load(FAKE_ROSTER))
    cache = tool.Cache(tmp_path / "cache.jsonl")
    assert tool.apply_guard(decisions, roster, cache, orgs=ORGS, year=YEAR) == 1
    fake = FakeHttp()
    flt = "authorships.author.id:A601,from_publication_date:2019-01-01"
    fake.add(f"{API}/works", {"filter": flt, "select": tool.WORKS_SELECT, "per_page": tool.WORKS_PER_PAGE,
                              "cursor": "*"}, json={"meta": {"next_cursor": None}, "results": []})
    m = OpenAlexMeter(_Host(), key="test-oa-key", ledger=Ledger(cap_usd=10.0))  # type: ignore[arg-type]
    out, _ = tool.resolve_coauthors(decisions, roster, tool.Client(m.wrap(fake), sleep=lambda s: None),
                                    cache, tool.Cache(tmp_path / "w.jsonl"), orgs=ORGS, year=YEAR)
    got = {d.name: (d.status, d.ids) for d in out}
    assert got["Ann Lee"] == ("ambiguous", []) and got["Ed Fox"] == ("resolved", ["A601"])
    assert len(fake.calls) == 1  # A101 is no coauthor evidence


def test_merged_profile_is_neither_evidence_nor_accepted() -> None:
    """An old roster id that is a merged profile links nobody; a merged candidate is not accepted
    however well linked."""
    roster = parse_roster({"version": "1", "names": [{"name": "Ann Lee", "openalex_ids": ["A7"]},
                                                     {"name": "Ed Fox", "openalex_ids": ["A6"]}, "Bo Chen"]})
    wc, cache = tool.Cache(None), tool.Cache(None)
    for w in ("W1", "W2"):
        wc.put("work", w, tool.reduce_work(work(w, ("A7", "Ann Lee"), ("A6", "Ed Fox"), ("A9", "Bo Chen"))))
    wc.put("works_of", "A6", ["W1", "W2"])
    wc.put("works_of", "A7", ["W1", "W2"])
    cache.put("author", "A7", yearly("A7", 64, 221, 2571))
    cache.put("author", "A6", yearly("A6", 5, 5, 5, "Ed Fox"))
    cache.put("author", "A9", yearly("A9", 1, 1, 1, "Bo Chen"))
    d = tool.Decision("Bo Chen", "not_found", [], "x", pool=[])
    out, _ = tool.resolve_coauthors([d], roster, tool.Client(None), cache, wc, orgs=ORGS, year=YEAR)
    c = next(c for c in out[0].pool if c.id == "A9")
    assert c.coauthors == {"Ed Fox": 2} and not c.linked and out[0].status == "not_found"
    m = linked("A1", "Jo Ma", {"P1": 2, "P2": 2}, works=3)
    m.yearly = (1, 7, 183)
    assert m.linked and not m.relevant
    assert tool.decide_coauthors(_undecided("Jo Ma", [m])).status == "ambiguous"
