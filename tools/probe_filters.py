#!/usr/bin/env python3
"""``tools/probe_filters.py`` — how many items each individual filter WOULD collect.

A breadth diagnostic, not a sweep. For every filter a collect workflow would run — each concept
group, each roster author, each forum keyword/tag, each listing surface — this asks the upstream
service for the **count only** and prints it. Nothing is minted, no ``sources`` row is written, no
``coverage`` row is claimed, and the store is never opened. Run it
before a sweep to find the over-general filters that would otherwise dominate the corpus, and
after a config change to see what the change actually widened.

Why counts are cheap here: every backend reports a total alongside its first page — OpenAlex
``meta.count``, arXiv ``opensearch:totalResults``, ForumMagnum ``nbHits`` — so one request per
filter answers "how big is this?" without paginating through it. Listing surfaces (blogs, cards)
have no count endpoint, so those are measured by fetching the ONE listing page a sweep would fetch
and counting the candidates it yields — the same work the real scan does, minus the minting.

The query construction is IMPORTED from the workflows themselves rather than re-expressed here,
so a filter that probes at N is the same filter that will run at N. A tool that built its own
queries would drift and quietly reassure you about something you never ran.

v3's ``--new`` mode (how many of those would be NEW, estimated per family against the ``sources``
table) went with the database at T4.5; a store-backed estimate (against the ``documents`` head's
keys and the collectors' own chains) is not implemented. GitHub-org listing surfaces stay skipped:
their scan walks repo trees and reads a prior watermark.

Usage:
    uv run tools/probe_filters.py                      # every channel
    uv run tools/probe_filters.py --channel openalex   # one family (openalex|s2|arxiv|forums|sites)
    uv run tools/probe_filters.py --limit 40           # first N filters per family
    uv run tools/probe_filters.py --fraction 0.1       # a stable 10% sample (sampling.select)
    uv run tools/probe_filters.py --json out.json      # machine-readable

NOTE on cost: arXiv's 3s politeness delay makes the full 2,269-author roster ~2 hours. The other
families are seconds-to-minutes. Use --fraction / --limit on arxiv unless you mean it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stack import config as config_mod  # noqa: E402
from stack.collect.sampling import select  # noqa: E402
from stack.progress import from_env as progress_from_env  # noqa: E402

FAMILIES = ("openalex", "s2", "arxiv", "forums", "sites")


def _load_wf(name: str) -> Any:
    """Import a collect workflow BY PATH (the runner's own mechanism) for its query builders."""
    spec = importlib.util.spec_from_file_location(
        f"_probe_{name}", REPO / "workflows" / "collect" / name / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _join_note(base: str, extra: str) -> str:
    """Combine a probe's own note with the estimator's note (both may be empty)."""
    if base and extra:
        return f"{base} · {extra}"
    return base or extra


# ─────────────────────────────────────────────────────────────────────────────
# Per-family probes — each returns rows of {channel, filter, count, note}
# ─────────────────────────────────────────────────────────────────────────────
def probe_openalex(cfg: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    """One request per concept group: the same filter expression the sweep builds, ``per_page=1``,
    read ``meta.count``. This is the number of works the group would page through."""
    oa = _load_wf("openalex")
    chan = cfg.channels.channel(oa.CHANNEL)
    floor = (os.environ.get("STACK_OPENALEX_FROM") or chan.from_date or oa.DEFAULT_FROM).strip()
    plan, _ = _sample(oa.plan_groups(cfg), lambda g: f"openalex:{g['id']}", args)
    rows: list[dict[str, Any]] = []
    progress = progress_from_env("probe openalex")
    progress.start(len(plan), unit="groups")
    for item in plan:
        progress.step(item["id"])
        query_label = f"group:{item['id']}"
        filt = oa.group_filter(item["terms"], floor, item["broad"])
        mailto = (os.environ.get("STACK_MAILTO") or "").strip()
        params: dict[str, Any] = {"filter": filt, "per_page": 1, "select": "id"}
        if mailto:
            params["mailto"] = mailto
        status, data = oa.http_get_json(oa.API_BASE, params)
        count = (
            int(((data or {}).get("meta") or {}).get("count") or 0)
            if status == 200 and isinstance(data, dict)
            else -1
        )

        rows.append({
            "channel": "openalex", "filter": query_label, "count": count,
            "note": "broad" if item["broad"] else "",
        })
        time.sleep(oa.DELAY_S)
    progress.finish()

    # The institution-id sweep family (count-only): one `institutions.id:<Iid>` count per
    # configured institution — the number of works that query would page through. Cheap (25 more
    # count calls) and it makes the second family visible alongside the concept groups.
    inst_plan, _ = _sample(oa.plan_institutions(cfg), lambda i: f"openalex:{i['query']}", args)
    if inst_plan:
        progress = progress_from_env("probe openalex institutions")
        progress.start(len(inst_plan), unit="institutions")
        for item in inst_plan:
            progress.step(item["id"])
            filt = oa.institution_filter(item["id"], floor, item["fields"])
            mailto = (os.environ.get("STACK_MAILTO") or "").strip()
            params = {"filter": filt, "per_page": 1, "select": "id"}
            if mailto:
                params["mailto"] = mailto
            status, data = oa.http_get_json(oa.API_BASE, params)
            count = (
                int(((data or {}).get("meta") or {}).get("count") or 0)
                if status == 200 and isinstance(data, dict)
                else -1
            )
            fields_note = f"fields {'|'.join(str(f) for f in item['fields'])}" if item["fields"] else ""
            rows.append({
                "channel": "openalex", "filter": item["query"], "count": count,
                "note": _join_note(f"org:{item['org']}", fields_note),
            })
            time.sleep(oa.DELAY_S)
        progress.finish()
    return rows


def probe_arxiv(cfg: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    """One request per roster author: the sweep's exact ``au:`` + category + date query with
    ``max_results=1``, reading the feed's ``opensearch:totalResults``.

    NOTE this counts what the QUERY returns, before the sweep's own author-list verification —
    which is the point: a name whose query returns thousands is a namesake collision, and seeing
    that here is cheaper than discovering it after a sweep."""
    ax = _load_wf("arxiv_sweep")
    chan = cfg.channels.channel(ax.CHANNEL)
    floor = (os.environ.get("STACK_ARXIV_FROM") or chan.from_date or ax.DEFAULT_FROM).strip()
    endpoint = (chan.location or "https://export.arxiv.org/api/query").replace("http://", "https://", 1)
    roster, _ = _sample(ax.load_roster(cfg), lambda n: f"arxiv:{n}", args)
    rows: list[dict[str, Any]] = []
    progress = progress_from_env("probe arxiv")
    progress.start(len(roster), unit="authors")
    for name in roster:
        progress.step(name)
        query = ax.arxiv_author_query(name)
        if not query:
            continue
        params = {
            "search_query": (
                f"au:{query} AND {ax._CAT_CLAUSE} AND "
                f"submittedDate:[{ax._to_compact(floor)} TO {ax.ARXIV_DATE_UPPER}]"
            ),
            "start": 0, "max_results": 1,
            "sortBy": "submittedDate", "sortOrder": "ascending",
        }
        status, body = ax.http_get(endpoint, params)
        count = _atom_total(body) if status == 200 else -1

        rows.append({
            "channel": "arxiv", "filter": query, "count": count,
            "note": name,
        })
        time.sleep(ax.ARXIV_DELAY_S)
    progress.finish()
    return rows


def _atom_total(body: str) -> int:
    """``opensearch:totalResults`` from an arXiv Atom feed; -1 when the feed is unparseable."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return -1
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "totalResults" and el.text:
            try:
                return int(el.text.strip())
            except ValueError:
                return -1
    return -1


def probe_forums(cfg: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    """One search request per (forum, query) with ``hitsPerPage=1``, reading ``nbHits``.

    This is the PRE-GATE count: the karma floor and the from-date are client-side filters the
    sweep applies afterwards, so the collected number will be lower. The gap between the two is
    itself the useful signal — a query with 5,000 hits and 40 keeps is a bad query, not a rich one."""
    fo = _load_wf("forums")
    rows: list[dict[str, Any]] = []
    plans: list[tuple[str, Any, dict[str, Any]]] = []
    for cid in fo.FORUM_CHANNELS:
        chan = cfg.channels.channel(cid)
        for item in fo.plan_queries(cfg, chan, fo.DEFAULT_TAGS):
            plans.append((cid, chan, item))
    plans, _ = _sample(plans, lambda p: f"{p[0]}:{fo._query_label(p[2])}", args)
    progress = progress_from_env("probe forums")
    progress.start(len(plans), unit="queries")
    for cid, chan, item in plans:
        label = fo._query_label(item)
        progress.step(f"{cid} {label}")
        base_url = (chan.location or "").rstrip("/")
        status, result = fo._search_page(base_url, str(item["term"]), item["tag"], 0, 1)
        count = int((result or {}).get("nbHits") or 0) if status == 200 and result else -1

        rows.append({
            "channel": cid, "filter": label, "count": count,
            "note": "tag" if item["kind"] == "tag" else "",
        })
        time.sleep(fo.LW_DELAY_S)
    progress.finish()
    return rows


def probe_s2(cfg: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    """One bulk-search request per concept group, reading S2's ``total``.

    Query construction is imported from ``collect.semantic_scholar``, so the misuse groups probe
    with their ``ai_context`` intersection applied — probing them bare would report a biosecurity-
    literature-sized number for a query the sweep never sends. The ``misuse`` note marks those rows,
    because they are the recall this channel exists to add and their volume is the decision.

    Requests are UNWINDOWED (no ``publicationDateOrYear``), so ``count`` is the whole floor range
    the first sweep would walk — not what a resumed sweep would add.
    """
    s2 = _load_wf("semantic_scholar")
    headers: dict[str, str] = {}
    key = s2.api_key()
    if key:
        headers["x-api-key"] = key
    plan, _ = _sample(s2.plan_groups(cfg), lambda item: f"semantic_scholar:{item['query']}", args)
    rows: list[dict[str, Any]] = []
    progress = progress_from_env("probe s2")
    progress.start(len(plan), unit="queries")
    for item in plan:
        progress.step(item["query"])
        params = s2.search_params(item["s2_query"], None)
        data = s2._get_page(params, headers)
        count = int((data or {}).get("total") or 0) if data is not None else -1

        rows.append({
            "channel": "semantic_scholar", "filter": item["query"], "count": count,
            "note": "misuse×ai_context" if item["misuse"] else "",
        })
        time.sleep(s2.DELAY_S)
    progress.finish()
    return rows


def probe_sites(cfg: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Blogs + card watchers: fetch each listing surface once and count the candidates it yields.

    No count endpoint exists for a listing page, so this does the same single fetch the real scan
    does — it just discards the result instead of minting sources. GitHub-org watchers are
    skipped: their scan walks repo trees and reads a prior watermark."""
    from stack.collect.listing import resolve_fetcher, scan_surface

    blogs, cards = _load_wf("blogs"), _load_wf("cards")
    surfaces: list[tuple[str, str, Any]] = []
    for bcid, spec_table in blogs.SITE_SPECS.items():
        surfaces.extend(("blogs", bcid, sp) for sp in spec_table["surfaces"])
    for ccid, watch in cards.WATCH_SPECS.items():
        surfaces.extend(("cards", ccid, sp) for sp in watch.surfaces)
    surfaces, _ = _sample(surfaces, lambda t: f"{t[1]}:{t[2].key}", args)
    fetcher = resolve_fetcher("STACK_PROBE_FIXTURES")
    rows: list[dict[str, Any]] = []
    progress = progress_from_env("probe sites")
    progress.start(len(surfaces), unit="surfaces")
    for _family, cid, spec in surfaces:
        progress.step(f"{cid} {spec.key}")
        if spec.method == "github-org":
            rows.append({"channel": cid, "filter": spec.key, "count": -1,
                         "note": "skipped: multi-request repo walk"})
            continue
        scan = scan_surface(spec, cid, None, fetcher, full=False)
        if scan.exhausted == "error":
            rows.append({"channel": cid, "filter": spec.key, "count": -1,
                         "note": f"{spec.method} (read failed)"})
            continue
        rows.append({
            "channel": cid, "filter": spec.key, "count": len(scan.candidates),
            "note": spec.method,
        })
    progress.finish()
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────
def _sample(units: list[Any], key_of: Any, args: argparse.Namespace) -> tuple[list[Any], dict[str, Any]]:
    """The sweep's own scale knobs, so a sampled probe covers the same slice a sampled sweep would
    (stack.collect.sampling — stable hash selection, not a prefix)."""
    env = dict(os.environ)
    if args.fraction is not None:
        env["STACK_COLLECT_FRACTION"] = str(args.fraction)
    if args.limit:
        env["STACK_COLLECT_MAX_QUERIES"] = str(args.limit)
    return select(units, key_of, env=env)


def _print_table(rows: list[dict[str, Any]], top: int) -> None:
    ok = [r for r in rows if r["count"] >= 0]
    failed = [r for r in rows if r["count"] < 0]
    total = sum(r["count"] for r in ok)
    ranked = sorted(ok, key=lambda r: -r["count"])

    print(f"\n{'channel':24} {'filter':40} {'would collect':>13}  note")
    print("─" * 97)
    for r in ranked[:top]:
        print(f"{r['channel']:24} {_clip(r['filter'], 40):40} {r['count']:>13,}  {r['note']}")
    if len(ranked) > top:
        rest = sum(r["count"] for r in ranked[top:])
        print(f"{'…':24} {_clip(f'{len(ranked) - top} more filters', 40):40} {rest:>13,}")
    print("─" * 97)
    print(f"{'TOTAL (with overlap)':65} {total:>13,}")
    if ok:
        share = ranked[0]["count"] / total if total else 0
        print(f"\nbreadth: {len(ok)} filters probed; "
              f"largest is {ranked[0]['channel']}/{ranked[0]['filter']} at {share:.0%} of the total")
        # A filter carrying a large share of the whole is the one to look at first: it is either
        # the umbrella term doing its job, or a filter that stopped being specific.
        heavy = [r for r in ranked if total and r["count"] / total > 0.15]
        if heavy:
            print("over-general candidates (>15% of the probed total):")
            for r in heavy:
                print(f"  {r['channel']}/{r['filter']}: {r['count']:,} ({r['count'] / total:.0%})")
    if failed:
        print(f"\n{len(failed)} filter(s) could not be probed (upstream error or skipped):")
        for r in failed[:10]:
            print(f"  {r['channel']}/{r['filter']}  {r['note']}")


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", choices=FAMILIES, action="append",
                    help="family to probe (repeatable; default: all)")
    ap.add_argument("--limit", type=int, default=0, help="cap filters per family (0 = all)")
    ap.add_argument("--fraction", type=float, default=None, help="stable fraction of filters, (0,1]")
    ap.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    ap.add_argument("--json", type=Path, default=None, help="also write the full rows as JSON")
    args = ap.parse_args()

    cfg = config_mod.load()
    families = args.channel or list(FAMILIES)
    probes = {"openalex": probe_openalex, "s2": probe_s2, "arxiv": probe_arxiv,
              "forums": probe_forums, "sites": probe_sites}
    rows: list[dict[str, Any]] = []
    for family in families:
        rows.extend(probes[family](cfg, args))

    _print_table(rows, args.top)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nwrote {len(rows)} rows to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
