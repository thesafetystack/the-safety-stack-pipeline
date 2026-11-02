#!/usr/bin/env python3
"""``tools/recall_concordia.py`` — the Concordia AI Chinese Technical AI Safety Database as a recall benchmark.

Concordia AI's public database (a Google Sheet; the 2025-07 export lists 464 rows, 461 distinct arXiv papers, of 2023-05 to 2025-05 by
Chinese groups, nearly all on arXiv, each with a research direction, a date and institutions) is an independent,
hand-curated list of Chinese technical AI-safety work. The Chinese-coverage review measured the 2026-10-27 release
against it (release 76.7 %, collected 80.0 %; Evaluations 61 %, Systemic 31 %); this tool makes that measurement
repeatable. No network: the benchmark is a file in the repository, and the check reads local parquet files.

    # rebuild the benchmark file from a fresh xlsx export of the sheet (File > Download > xlsx)
    uv run --no-sync python tools/recall_concordia.py build --xlsx concordia.xlsx \\
        --out evals/recall/concordia-2025-07.jsonl
    # recall of a store's documents head and, optionally, of a release corpus
    uv run --no-sync python tools/recall_concordia.py check --benchmark evals/recall/concordia-2025-07.jsonl \\
        --documents 'v4/store-run-x/data/documents/batch-import/*.parquet' --release <corpus.parquet> \\
        --misses misses.jsonl

**Build.** The sheet "Chinese Frontier AI Safety Papers" (the first sheet whose name starts with "Chinese Frontier"):
column A the title (hyperlinked to the paper), B the research direction, C the date (an Excel serial), D-F
institutions, G-K authors (two anchor-author columns, then other authors). The xlsx is read with the standard
library (shared strings, the sheet's hyperlink relations or ``HYPERLINK`` formulas for the URL). One JSON line per
paper: ``{title, url, arxiv, direction, date, institutions, authors}``, ``arxiv`` the id when the URL is an arXiv URL
(:func:`stack.urls.arxiv_of_url`, without the version).

**Check.** A benchmark paper is *found* in a parquet file of documents (a ``documents`` family file: unmerged rows,
``metadata.title``) or of a release corpus (``title``) when a row carries its arXiv key (``arxiv``, or the arXiv
DataCite DOI) or its URL key, else has the same normalised title (:func:`stack.identity.norm_title`, titles of
fewer than four tokens skipped). Printed: found / total over all and by direction and year; ``--misses`` writes the
papers not found in the last input given (the release when given).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stack.identity import norm_title  # noqa: E402
from stack.urls import arxiv_of_url, url_key  # noqa: E402

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
SHEET_PREFIX = "Chinese Frontier"
#: Titles shorter than this many normalised tokens are not matched by title (generic or fragmentary).
MIN_TITLE_TOKENS = 4


# ---------------------------------------------------------------- build
def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + ord(ch) - 64
    return n - 1


def _rownum(ref: str) -> int:
    return int(re.sub(r"[A-Z]+", "", ref))


def read_sheet(xlsx: Path, prefix: str = SHEET_PREFIX) -> tuple[list[list[str]], dict[int, str]]:
    """``(rows, row number -> hyperlink URL of column A)`` of the first sheet whose name starts with ``prefix``."""
    z = zipfile.ZipFile(xlsx)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{M}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{M}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheet = next((s for s in wb.iter(f"{M}sheet") if (s.get("name") or "").startswith(prefix)), None)
    if sheet is None:
        raise SystemExit(f"no sheet named {prefix!r}… in {xlsx}")
    target = str(rels[sheet.get(f"{R}id")]).lstrip("/")
    path = target if target.startswith("xl/") else f"xl/{target}"
    root = ET.fromstring(z.read(path))
    rows: list[list[str]] = []
    links: dict[int, str] = {}
    for row in root.iter(f"{M}row"):
        vals: dict[int, str] = {}
        for c in row.findall(f"{M}c"):
            v, t, ref = c.find(f"{M}v"), c.get("t"), c.get("r") or ""
            if t == "s" and v is not None:
                val = shared[int(v.text or 0)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{M}t"))
            else:
                val = (v.text or "") if v is not None else ""
            vals[_col(ref)] = val
            f = c.find(f"{M}f")
            if ref.startswith("A") and f is not None and f.text and "HYPERLINK" in f.text.upper():
                m = re.search(r'"(https?://[^"]+)"', f.text)
                if m:
                    links.setdefault(_rownum(ref), m.group(1))
        if vals:
            rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    rel_path = path.replace("worksheets/", "worksheets/_rels/") + ".rels"
    if rel_path in z.namelist():
        srels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read(rel_path)) if r.tag == f"{PKG}Relationship"}
        for h in root.iter(f"{M}hyperlink"):
            rid, ref = h.get(f"{R}id"), h.get("ref") or ""
            if rid in srels and ref.startswith("A"):
                links[_rownum(ref)] = str(srels[rid])
    return rows, links


def _date(raw: str) -> str:
    try:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=float(raw))).isoformat()
    except ValueError:
        return raw.strip()


def papers(rows: list[list[str]], links: Mapping[int, str]) -> list[dict[str, Any]]:
    """The benchmark items of the sheet's rows (the first row is the header; rows without a title skipped)."""
    out = []
    for i, r in enumerate(rows[1:], start=2):
        r = r + [""] * (11 - len(r))
        if not r[0].strip():
            continue
        url = links.get(i, "")
        ax = arxiv_of_url(url) if url else None
        out.append({"title": " ".join(r[0].split()), "url": url, "arxiv": ax[0] if ax else "",
                    "direction": r[1].strip(), "date": _date(r[2]),
                    "institutions": [x.strip() for x in r[3:6] if x.strip()],
                    "authors": [x.strip() for x in r[6:11] if x.strip()]})
    return out


# ---------------------------------------------------------------- check
def _rows(path: str) -> Iterator[tuple[set[str], str]]:
    """``(keys as kind:value, title)`` per row of a documents file (unmerged rows) or a release corpus."""
    from stack import duck  # noqa: PLC0415

    con = duck.connect()  # D732: spills under the scratch directory
    cols = {r[0] for r in con.sql(f"describe select * from read_parquet('{path}')").fetchall()}
    title = "metadata.title" if "metadata" in cols else "title"
    where = "where identity.merged_into is null" if "metadata" in cols else ""
    for keys, t in con.sql(f"select identity.keys, {title} from read_parquet('{path}') {where}").fetchall():
        yield {f"{k['kind']}:{k['value']}" for k in keys or []}, t or ""


def found_in(bench: list[dict[str, Any]], path: str) -> list[bool]:
    """Per benchmark item: whether ``path`` holds it (module docstring)."""
    keys: set[str] = set()
    titles: set[str] = set()
    for ks, t in _rows(path):
        keys |= ks
        nt = norm_title(t)
        if len(nt.split()) >= MIN_TITLE_TOKENS:
            titles.add(nt)
    out = []
    for p in bench:
        want = set()
        if p.get("arxiv"):
            want |= {f"arxiv:{p['arxiv']}", f"doi:10.48550/arxiv.{p['arxiv'].lower()}"}
        if p.get("url") and not p.get("arxiv"):
            want.add(f"url:{url_key(p['url'])}")
        nt = norm_title(p["title"])
        out.append(bool(want & keys) or (len(nt.split()) >= MIN_TITLE_TOKENS and nt in titles))
    return out


def summary(bench: list[dict[str, Any]], hits: list[bool]) -> dict[str, Any]:
    def rate(sel: Iterable[int]) -> dict[str, Any]:
        idx = list(sel)
        n = sum(hits[i] for i in idx)
        return {"found": n, "total": len(idx), "rate": round(n / len(idx), 4) if idx else None}
    by_dir = collections.defaultdict(list)
    by_year = collections.defaultdict(list)
    for i, p in enumerate(bench):
        by_dir[p.get("direction") or "?"].append(i)
        by_year[(p.get("date") or "?")[:4]].append(i)
    return {"all": rate(range(len(bench))), "direction": {k: rate(v) for k, v in sorted(by_dir.items())},
            "year": {k: rate(v) for k, v in sorted(by_year.items())}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="xlsx export -> benchmark JSONL")
    b.add_argument("--xlsx", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check", help="recall of documents / a release corpus")
    c.add_argument("--benchmark", type=Path, default=REPO / "evals" / "recall" / "concordia-2025-07.jsonl")
    c.add_argument("--documents", help="a documents parquet file or glob (a store's documents head)")
    c.add_argument("--release", help="a release corpus.parquet")
    c.add_argument("--misses", type=Path, help="write the papers the last input lacks as JSON lines")
    c.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        rows, links = read_sheet(args.xlsx)
        items = papers(rows, links)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in items))
        print(f"{len(items)} papers, {sum(bool(p['arxiv']) for p in items)} with an arXiv id -> {args.out}")
        return 0
    bench = [json.loads(line) for line in args.benchmark.read_text().splitlines() if line.strip()]
    inputs = [(n, p) for n, p in (("documents", args.documents), ("release", args.release)) if p]
    if not inputs:
        ap.error("check needs --documents and/or --release")
    report: dict[str, Any] = {"benchmark": str(args.benchmark), "papers": len(bench)}
    hits: list[bool] = []
    for name, path in inputs:
        hits = found_in(bench, path)
        report[name] = summary(bench, hits)
    if args.misses:
        args.misses.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p, h in zip(bench, hits) if not h))
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        for name, _ in inputs:
            s = report[name]
            print(f"{name}: {s['all']['found']}/{s['all']['total']} ({s['all']['rate']:.1%})")
            for k, v in s["direction"].items():
                print(f"  {k:34s} {v['found']:4d}/{v['total']:<4d} {v['rate']:.1%}")
            for k, v in s["year"].items():
                print(f"  year {k:29s} {v['found']:4d}/{v['total']:<4d} {v['rate']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
