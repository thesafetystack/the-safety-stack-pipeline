"""``tools/recall_concordia.py`` (the Chinese-coverage work): the Concordia database as a recall benchmark, offline. A synthetic xlsx
in the sheet's layout (title hyperlinked in column A, an Excel-serial date, institutions, authors) builds the
benchmark lines; a documents file and a release corpus, each a small parquet, are checked against it (arXiv key,
the arXiv DataCite DOI, a normalised title, a merged-away row not counting). The repository's benchmark file is the
2025-07 export: 464 rows, 462 with an arXiv id (461 distinct)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import zipfile
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("recall_concordia", REPO / "tools" / "recall_concordia.py")
assert _spec is not None and _spec.loader is not None
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' \
     'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
PKG = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def _cell(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _xlsx(path: pathlib.Path) -> None:
    rows = [
        ["Link", "Research direction", "Dates", "Institution 1", "Institution 2", "Other institutions",
         "Anchor author", "Anchor author", "Other authors"],
        ["SafeBench: A Safety Benchmark for Chinese Large Language Models", "Monitoring (evaluations)", None,
         "Tsinghua University", "", "", "Minlie Huang", "", "Zhexin Zhang"],
        ["Jailbreak Attacks on Multimodal Large Language Models Survey", "Robustness", None, "Fudan University",
         "", "", "Xingjun Ma"],
        ["Value Alignment of Chinese Language Models in Practice Today", "Alignment", None, "Peking University"],
    ]
    xml = []
    for i, r in enumerate(rows, start=1):
        cells = []
        for j, v in enumerate(r):
            ref = f"{chr(65 + j)}{i}"
            if v is None:
                cells.append(f'<c r="{ref}"><v>{45200 + i}</v></c>')  # an Excel serial date
            elif v:
                cells.append(_cell(ref, v))
        xml.append(f'<row r="{i}">{"".join(cells)}</row>')
    sheet = (f'<worksheet {NS}><sheetData>{"".join(xml)}</sheetData><hyperlinks>'
             '<hyperlink ref="A2" r:id="rId1"/><hyperlink ref="A3" r:id="rId2"/></hyperlinks></worksheet>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {NS}><sheets><sheet name="Guide" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Chinese Frontier AI Safety Papers" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships {PKG}><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {NS}><sheetData/></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", sheet)
        z.writestr("xl/worksheets/_rels/sheet2.xml.rels", f'<Relationships {PKG}>'
                   '<Relationship Id="rId1" Target="https://arxiv.org/abs/2309.07045v2"/>'
                   '<Relationship Id="rId2" Target="https://arxiv.org/pdf/2407.01599"/></Relationships>')


def _keys(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"kind": k, "value": v, "source_id": "s1"} for k, v in pairs]


def test_build_reads_the_sheet_layout(tmp_path: pathlib.Path) -> None:
    _xlsx(tmp_path / "db.xlsx")
    rows, links = rc.read_sheet(tmp_path / "db.xlsx")
    items = rc.papers(rows, links)
    assert [p["arxiv"] for p in items] == ["2309.07045", "2407.01599", ""]
    assert items[0]["direction"] == "Monitoring (evaluations)" and items[0]["date"] == "2023-10-03"  # serial 45202


def test_build_writes_only_the_kept_fields(tmp_path: pathlib.Path) -> None:
    """The benchmark keeps what ``check`` reads plus each paper's identity; the sheet's institutions and authors
    (columns D-K) are not written, so a rebuild from a fresh export matches the repository's file."""
    _xlsx(tmp_path / "db.xlsx")
    assert rc.main(["build", "--xlsx", str(tmp_path / "db.xlsx"), "--out", str(tmp_path / "b.jsonl")]) == 0
    text = (tmp_path / "b.jsonl").read_text()
    items = [json.loads(x) for x in text.splitlines()]
    assert len(items) == 3 and all(list(p) == list(rc.FIELDS) for p in items)
    assert "Tsinghua" not in text and "Minlie Huang" not in text and "Zhexin Zhang" not in text


def test_check_finds_by_arxiv_key_doi_and_title(tmp_path: pathlib.Path) -> None:
    _xlsx(tmp_path / "db.xlsx")
    bench = rc.papers(*rc.read_sheet(tmp_path / "db.xlsx"))
    docs: list[dict[str, Any]] = [
        {"doc_id": "d1", "identity": {"keys": _keys(("arxiv", "2309.07045")), "merged_into": None},
         "metadata": {"title": "something else"}},
        {"doc_id": "d2", "identity": {"keys": _keys(("doi", "10.48550/arxiv.2407.01599")), "merged_into": "d9"},
         "metadata": {"title": "x"}},  # merged away: not counted
        {"doc_id": "d3", "identity": {"keys": _keys(("openalex", "W1")), "merged_into": None},
         "metadata": {"title": "Value alignment of Chinese language models in practice today."}},
    ]
    pq.write_table(pa.Table.from_pylist(docs), tmp_path / "docs.parquet")
    assert rc.found_in(bench, str(tmp_path / "docs.parquet")) == [True, False, True]
    corpus = [{"doc_id": "d2", "title": "Jailbreak attacks on MLLMs",
               "identity": {"keys": _keys(("doi", "10.48550/arxiv.2407.01599"))}}]
    pq.write_table(pa.Table.from_pylist(corpus), tmp_path / "corpus.parquet")
    hits = rc.found_in(bench, str(tmp_path / "corpus.parquet"))
    assert hits == [False, True, False]
    s = rc.summary(bench, hits)
    assert s["all"] == {"found": 1, "total": 3, "rate": 0.3333} and s["direction"]["Robustness"]["found"] == 1
    assert rc.main(["check", "--benchmark", str(_write(tmp_path, bench)), "--documents", str(tmp_path / "docs.parquet"),
                    "--misses", str(tmp_path / "m.jsonl")]) == 0
    assert [json.loads(x)["arxiv"] for x in (tmp_path / "m.jsonl").read_text().splitlines()] == ["2407.01599"]


def _write(tmp_path: pathlib.Path, bench: list[dict[str, Any]]) -> pathlib.Path:
    p = tmp_path / "bench.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in bench))
    return p


def test_repo_benchmark_is_the_2025_07_export() -> None:
    items = [json.loads(x) for x in (REPO / "evals" / "recall" / "concordia-2025-07.jsonl").read_text().splitlines()]
    assert len(items) == 464 and sum(bool(p["arxiv"]) for p in items) == 462
    assert len({p["arxiv"] for p in items if p["arxiv"]}) == 461  # one paper is listed twice in the sheet
    assert all(list(p) == list(rc.FIELDS) for p in items)  # what `build` writes, in its order
