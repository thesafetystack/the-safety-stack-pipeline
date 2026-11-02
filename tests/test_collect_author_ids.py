"""C4 (docs/design/collection-yield.md §3.2, §9.3; round 5 (RUN) B7b): the roster's
``openalex_ids`` and ``disambiguate`` list (:mod:`stack.collect.roster`), the arXiv author sweep's
filter for the listed names (and the option that leaves a resolved one to OpenAlex), and
``collect.openalex``'s author-id family. End-to-end cases run ``run_stage`` on a temporary store over
``tests/fakes/http.py`` (no network)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import time
from typing import Any

import pytest
import yaml

from stack.collect.roster import parse_roster
from stack.config import load
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.store.chain import read_view, resolve
from tests.fakes.http import FakeHttp

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_collect_{name}_c4_under_test", REPO / "workflows" / "collect" / name / "main.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


arxiv = _load_wf("arxiv_sweep")
openalex = _load_wf("openalex")


# ─────────────────────────────────────────────────────────────────────────────
# (A) the roster file
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_roster_entries_ids_and_disambiguate() -> None:
    r = parse_roster(yaml.safe_load('''
version: "1.2.0"
disambiguate: ["Cheng, Yu"]
names:
  - "Yu Cheng"
  - {name: "Perez, Ethan", openalex_ids: [A5091112967]}
  - {name: "Ethan Perez", openalex_ids: [A5030682501, A5091112967], orcid: 0000-0002-1825-009x}
  - "Ann Other"
'''))
    assert r.version == "1.2.0" and r.names == ["Ann Other", "Ethan Perez", "Yu Cheng"]
    assert r.ids_of("Perez, Ethan") == ("A5030682501", "A5091112967")  # twins merged
    assert r.entries[1].orcid == "0000-0002-1825-009X"
    assert r.disambiguate == frozenset({"Yu Cheng"})


@pytest.mark.parametrize(("text", "match"), [
    ('names: [{name: "A B", openalex_ids: [W123]}]', "not OpenAlex author ids"),
    ('names: [{name: "A B", orcid: "12-34"}]', "not a bare ORCID"),
    ('names: [{name: "A B", openalex: [A1]}]', "unknown keys"),
    ('names: ["A B"]\ndisambiguate: ["A C"]', "not roster names"),
])
def test_parse_roster_refuses_malformed_entries(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_roster(yaml.safe_load(text))


def test_repo_roster_lists_the_nine_c4_names_and_the_ten_chinese_names() -> None:
    r = arxiv.load_roster_full(load(CONFIG_DIR), CONFIG_DIR / "rosters" / "researchers.yaml")
    assert r.disambiguate == frozenset({"Zheng Zhang", "Yu Cheng", "Han Wang", "Xiaomin Li",
                                        "Hao Sun", "Yi Dong", "Pang Wei Koh", "Anirudh Goyal",
                                        "Jan Kautz",
                                        # The Chinese-coverage work: four common names without ids, six prolific generalists
                                        "Yu Qiao", "Jing Shao", "Yingchun Wang", "Yi Zeng", "Yu-Gang Jiang",
                                        "Xipeng Qiu", "Xuanjing Huang", "Minlie Huang", "Deyi Xiong", "Tao Gui"})


def test_repo_roster_carries_the_chinese_safety_ids() -> None:
    """The Chinese-coverage work: ids checked by hand against the person's Concordia-database papers (the OpenAlex works of their
    arXiv DOIs carry the id under the name); the resolver's own picks differ for Xipeng Qiu and Yinpeng Dong."""
    r = arxiv.load_roster_full(load(CONFIG_DIR), CONFIG_DIR / "rosters" / "researchers.yaml")
    assert r.ids_of("Minlie Huang") == ("A5044042138",)
    assert r.ids_of("Xipeng Qiu") == ("A5044665993",)        # not A5000302538 (13 works)
    assert r.ids_of("Yinpeng Dong") == ("A5068755794", "A5139424245")
    assert r.ids_of("Jing Shao") == () and r.ids_of("Yu Qiao") == ()  # papers over several profiles: names only


def test_repo_roster_carries_the_scope_r2_safety_ids() -> None:
    """D655: ids checked by hand against a known work of the person (the name search
    misresolves these names); the roster still plans within OpenAlex's OR limit."""
    r = arxiv.load_roster_full(load(CONFIG_DIR), CONFIG_DIR / "rosters" / "researchers.yaml")
    assert r.ids_of("Stuart Armstrong") == ("A5005897864",)  # "Good and safe uses of AI Oracles"
    assert r.ids_of("Nick Bostrom") == ("A5027943191", "A5139109693")  # a split profile
    assert r.ids_of("Jessica Taylor") == ("A5078386507",)  # "Logical Induction", not a namesake
    assert r.ids_of("Guy Katz") == ("A5102986148",)  # Reluplex
    assert r.ids_of("Hilton, Benjamin") == ()  # no OpenAlex profile carries his works
    ids = {i for e in r.entries for i in e.openalex_ids}
    assert "A5038101556" not in ids  # a climate scientist's profile that absorbed METR papers
    plan = openalex.plan_authors(r)
    assert sum(len(p["ids"]) for p in plan) == len(ids)
    assert max(len(p["ids"]) for p in plan) <= openalex.MAX_IDS_PER_FILTER


# ─────────────────────────────────────────────────────────────────────────────
# (B) the arXiv filter
# ─────────────────────────────────────────────────────────────────────────────
ROSTER_NAMES = ["Yu Cheng", "Ethan Perez", "Ann Other"]


@pytest.mark.parametrize(("title", "abstract", "authors", "kept"), [
    ("Faster video codecs", "We compress video.", ["Yu Cheng", "Bo Third"], False),
    ("Jailbreaks of video models", "", ["Yu Cheng"], True),                      # stem in title
    ("Faster video codecs", "Robust oversight of codecs.", ["Yu Cheng"], True),  # stem in abstract
    ("Faster video codecs", "We compress video.", ["Yu Cheng", "E. Perez"], True),  # 2nd roster author
    # the query's own name in another spelling is not a second roster author
    ("Faster video codecs", "We compress video.", ["Yu Cheng", "Cheng, Yu"], False),
])
def test_keep_disambiguated_hit(title: str, abstract: str, authors: list[str], kept: bool) -> None:
    index = arxiv.roster_index(ROSTER_NAMES)
    entry = {"title": title, "abstract": abstract, "authors": authors}
    assert arxiv.keep_disambiguated_hit(entry, ["Yu Cheng"], index) is kept


def test_plan_marks_disambiguate_names() -> None:
    plan = {q["query"]: q for q in arxiv.plan_author_queries(ROSTER_NAMES, frozenset({"Yu Cheng"}))}
    assert plan["Cheng_Yu"]["disambiguate"] == ["Yu Cheng"] and plan["Perez_Ethan"]["disambiguate"] == []


ARXIV_API = "https://export.arxiv.org/api/query"


def _atom(entries: list[tuple[str, str, list[str]]]) -> str:
    parts = ['<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">']
    for aid, title, authors in entries:
        names = "".join(f"<author><name>{a}</name></author>" for a in authors)
        parts.append(f"<entry><id>http://arxiv.org/abs/{aid}v1</id><title>{title}</title>"
                     f"<summary>About {title}.</summary><published>2026-09-02T00:00:00Z</published>"
                     f"{names}</entry>")
    return "".join([*parts, "</feed>"])


def _arxiv_params(token: str) -> dict[str, Any]:
    q = f"au:{token} AND {arxiv._CAT_CLAUSE} AND submittedDate:[202609010000 TO {arxiv.ARXIV_DATE_UPPER}]"
    return {"search_query": q, "start": 0, "max_results": 10, "sortBy": "submittedDate",
            "sortOrder": "ascending"}


def _plan(stage: str, **options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": stage, "prior": ["sources", "coverage"], "options": options}]})


def _view(store: pathlib.Path, chain_id: str) -> list[dict[str, Any]]:
    return read_view(resolve(store, sdir(chain_id))).to_pylist()


@pytest.fixture
def clean_env(monkeypatch: Any) -> None:
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM", "STACK_OPENALEX_FROM",
                "STACK_OPENALEX_FULL", "STACK_OPENALEX_RETRY_FAILED_RUN", "STACK_MAILTO",
                "OPENALEX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def test_arxiv_author_family_filters_disambiguate_names(tmp_path: pathlib.Path, monkeypatch: Any,
                                                        clean_env: None) -> None:
    roster = tmp_path / "roster.yaml"
    roster.write_text('disambiguate: ["Yu Cheng"]\nnames:\n  - "Yu Cheng"\n  - "Ethan Perez"\n'
                      '  - {name: "Ann Other", openalex_ids: [A7]}\n', "utf-8")
    fake = FakeHttp()
    fake.add(ARXIV_API, _arxiv_params("Cheng_Yu"), body=_atom([
        ("2609.00001", "Faster video codecs", ["Yu Cheng"]),                 # namesake: dropped
        ("2609.00002", "Jailbreaks of video models", ["Yu Cheng"]),          # stem: kept
        ("2609.00003", "Faster video codecs II", ["Yu Cheng", "Ethan Perez"])]))  # 2nd author: kept
    fake.add(ARXIV_API, _arxiv_params("Perez_Ethan"), body=_atom([
        ("2609.00004", "Faster video codecs III", ["Ethan Perez"])]))  # not listed: no filter
    fake.add(ARXIV_API, _arxiv_params("Other_Ann"), body=_atom([]))
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    opts = {"roster": str(roster), "from": "2026-09-01", "max_records": 10, "families": ["author"]}
    rec = run_line(tmp_path / "store", _plan("collect.arxiv_sweep", **opts), "collect.arxiv_sweep")
    assert rec.status == "ok", rec.error
    assert (rec.counts["disambiguate_kept"], rec.counts["disambiguate_dropped"]) == (2, 1)
    src = sorted(r["identity"]["external_id"] for r in _view(tmp_path / "store", "sources.arxiv_sweep"))
    assert src == ["2609.00002", "2609.00003", "2609.00004"]
    cov = {r["query"]: r for r in _view(tmp_path / "store", "coverage.arxiv_sweep")}
    assert cov["Cheng_Yu"]["seen"] == 2  # a dropped hit is not in the window's seen

    # disambiguate_by_id: only a listed name WITH ids leaves the arXiv sweep (Yu Cheng has none)
    store2 = tmp_path / "store2"
    roster.write_text('disambiguate: ["Yu Cheng"]\nnames:\n  - {name: "Yu Cheng", openalex_ids: [A5]}\n'
                      '  - "Ethan Perez"\n', "utf-8")
    n = len(fake.calls)
    rec2 = run_line(store2, _plan("collect.arxiv_sweep", disambiguate_by_id=True, **opts),
                     "collect.arxiv_sweep")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["author_queries_by_id"] == 1 and rec2.counts["queries_author"] == 1
    assert ["Cheng_Yu" in c.url for c in fake.calls[n:]] == [False]


# ─────────────────────────────────────────────────────────────────────────────
# (C) the OpenAlex author-id family
# ─────────────────────────────────────────────────────────────────────────────
def _roster(ids_by_name: dict[str, list[str]]) -> Any:
    return parse_roster({"names": [{"name": n, "openalex_ids": ids} for n, ids in ids_by_name.items()]})


def test_author_filter_shape() -> None:
    assert openalex.author_filter(["A1", "A22"], "2026-09-01") == (
        "type:article|preprint|review|book-chapter|conference-paper,authorships.author.id:A1|A22,"
        "from_publication_date:2026-09-01")
    assert openalex.author_filter(["A1"], "2026-09-01", to_date="2026-10-10").endswith(
        "from_publication_date:2026-09-01,to_publication_date:2026-10-10")
    with pytest.raises(ValueError, match="author ids"):
        openalex.author_filter(["A1", "W2"], "2026-09-01")


def test_author_buckets_are_stable_when_an_id_is_added() -> None:
    ids = {f"Name {i}": [f"A{5000 + i}"] for i in range(200)}
    before = {p["query"]: p["ids"] for p in openalex.plan_authors(_roster(ids), 16)}
    after = {p["query"]: p["ids"] for p in openalex.plan_authors(
        _roster({**ids, "New Name": ["A9999"]}), 16)}
    changed = [q for q in after if after[q] != before.get(q)]
    assert len(changed) == 1 and "A9999" in after[changed[0]]
    assert sum(map(len, before.values())) == 200 and all(q.startswith("authors:b") for q in before)
    with pytest.raises(ValueError, match="OR limit"):
        openalex.plan_authors(_roster(ids), 1)  # 200 ids in one bucket > 100


OA_API = "https://api.openalex.org/works"


def test_openalex_author_family_end_to_end(tmp_path: pathlib.Path, monkeypatch: Any,
                                           clean_env: None) -> None:
    roster = tmp_path / "roster.yaml"
    roster.write_text('names:\n  - {name: "Ethan Perez", openalex_ids: [A5091112967]}\n'
                      '  - "Ann Other"\n', "utf-8")
    (item,) = openalex.plan_authors(parse_roster(yaml.safe_load(roster.read_text())))
    label = item["query"]
    filt = openalex.author_filter(["A5091112967"], "2026-09-01")
    work = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.48550/arXiv.2609.00009",
            "title": "Sleeper agents II", "ids": {}, "type": "preprint",
            "publication_date": "2026-09-03",
            "authorships": [{"author": {"display_name": "Ethan Perez"}, "institutions": []}]}
    fake = FakeHttp()
    fake.add(OA_API, {"filter": filt, "per_page": openalex.PAGE_SIZE,
                      "cursor": "*"}, json={"results": [work], "meta": {"next_cursor": None}})
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    store = tmp_path / "store"
    rec = run_line(store, _plan("collect.openalex", author_ids=True, roster=str(roster),
                                 queries=[label], **{"from": "2026-09-01"}), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert (rec.counts["author_buckets_planned"], rec.counts["author_ids_planned"]) == (1, 1)
    assert rec.counts["openalex_keyless"] == 1 and len(fake.calls) == 1
    (src,) = _view(store, "sources.openalex")
    assert src["identity"]["external_id"] == "10.48550/arxiv.2609.00009"
    assert [m["query_id"] for m in src["metadata"]["matched_by"]] == [label]
    (cov,) = _view(store, "coverage.openalex")
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert (openalex.query_label(cov["query"]), cov["status"], cov["seen"],
            cov["window_end_at"].date().isoformat()) == (label, "ok", 1, today)
    # without the option the family is not planned: its label is unknown
    with pytest.raises(Exception, match="not configured query labels"):
        rec = run_line(store, _plan("collect.openalex", roster=str(roster), queries=[label]),
                        "collect.openalex")
        raise RuntimeError(rec.error)
