#!/usr/bin/env python3
"""Probe ONE collect query end-to-end against live upstreams — read-only, no store writes.

    uv run --env-file .env.local python tools/probe_collect.py --workflow collect.openalex \
        --query superalignment --limit 5
    uv run --env-file .env.local python tools/probe_collect.py --workflow collect.cards \
        --query anthropic --dump ./probe-out
    uv run --env-file .env.local python tools/probe_collect.py --workflow collect.semantic_scholar \
        --query rlhf --record ./probe-cassette

WHY THIS EXISTS, given ``tools/probe_filters.py`` already probes six of the seven ``--workflow``
choices here (all but ``collect.af_graphql``; it takes blogs and cards together as ``sites``).
``probe_filters`` answers "how BIG is this filter" — one request per filter, ``per_page=1``, read
the count. It is a breadth diagnostic and it deliberately never looks at an item. When a query
misbehaves (the repeatable ``group:superalignment`` error, arXiv's 429s, GitHub's proxy block,
DeepMind's two-era sitemap) a count tells you nothing: you need the REQUEST, the BYTES, and the
IDENTITY the sweep would have minted from them. That is this tool.

Three properties it holds onto, in descending order of how much damage their absence does:

1. **No writes, ever.** No ``sources`` row, no ``coverage`` row, no run record; the store is never
   opened. (v3's ``--known`` flag, a read-only SQL lookup of each minted identity in ``sources``,
   went with the database at T4.5; a lookup against the store's ``documents`` keys is not
   implemented.)

2. **It runs the sweep's own code.** Every query string, pagination loop, and identity rule is
   imported from ``workflows/collect/<name>/main.py`` by path — the runner's own mechanism, via
   ``probe_filters._load_wf``'s pattern. A probe that re-implemented ``group_filter`` would drift
   from the sweep and start lying exactly when you most need it not to. The corollary is that this
   file holds NO parsing logic of its own; it is a harness and a printer.

3. **What it fetches, it can record.** ``--record <dir>`` writes ``responses.json`` in the format
   ``stack.collect.listing.ReplayFetcher`` already reads, so one polite live pass becomes an
   offline cassette. Recording wraps each workflow's module-level ``http_*`` function — the same
   seam the test suite monkeypatches — so it works for the five API sweeps today, without waiting
   on a shared fetcher.

The four sweeps whose ``http_*`` seam sends only through the run's transport (semantic_scholar,
arxiv_sweep, forums, af_graphql: "no transport outside run(ctx)") get that transport here: the
stage's own :class:`stack.collect.base.Transport` at the module's ``PACING`` (pacing, the provider
Gate, 429 handling), set as the module's ``_T`` as ``run(ctx)`` sets it, on :class:`_ProbeRun`
instead of a run's ``Collector`` (no store, no run record; its counts are discarded). openalex
keeps its own outside-a-run path.

Exit status is 0 when every probed unit came back ``complete`` or ``truncated``, 1 when any came
back ``error`` — so this is usable as a pre-sweep upstream check in a script.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stack import config as config_mod  # noqa: E402
from stack.collect.base import RunHalted, Transport  # noqa: E402

FAMILIES = {
    "collect.openalex": "openalex",
    "collect.semantic_scholar": "semantic_scholar",
    "collect.arxiv_sweep": "arxiv_sweep",
    "collect.forums": "forums",
    "collect.af_graphql": "af_graphql",
    "collect.blogs": "blogs",
    "collect.cards": "cards",
}


def _load_wf(name: str) -> Any:
    """Import a collect workflow BY PATH (the runner's own mechanism) for its query builders."""
    spec = importlib.util.spec_from_file_location(
        f"_probe_collect_{name}", REPO / "workflows" / "collect" / name / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# Recording: wrap a module's http_* seam and bank every exchange
# ─────────────────────────────────────────────────────────────────────────────
class Recorder:
    """Captures every HTTP exchange a probed workflow makes.

    Keyed on the params-folded URL exactly as ``listing._url_with_params`` builds it, because that
    is the key ``ReplayFetcher`` looks up. A POST (forums, AF GraphQL) has no such URL, so its body
    is folded into the key as a sorted-JSON ``__body`` parameter — deterministic across runs, and
    distinguishable from a GET at a glance when you read the cassette.
    """

    def __init__(self, dump_dir: Path | None, record_dir: Path | None) -> None:
        self.dump_dir = dump_dir
        self.record_dir = record_dir
        self.responses: dict[str, dict[str, Any]] = {}
        self.exchanges: list[dict[str, Any]] = []
        self._n = 0

    @staticmethod
    def _key(url: str, params: dict[str, Any] | None, body: Any | None) -> str:
        parts: list[tuple[str, str]] = []
        if params:
            parts += [(k, str(v)) for k, v in params.items()]
        if body is not None:
            parts.append(("__body", json.dumps(body, sort_keys=True, separators=(",", ":"))))
        if not parts:
            return url
        qs = urllib.parse.urlencode(sorted(parts))
        sep = "&" if urllib.parse.urlsplit(url).query else "?"
        return f"{url}{sep}{qs}"

    def bank(
        self, url: str, params: dict[str, Any] | None, body: Any | None,
        status: int, text: str, elapsed: float,
    ) -> None:
        key = self._key(url, params, body)
        self.responses[key] = {"status": status, "text": text}
        self._n += 1
        self.exchanges.append(
            {"n": self._n, "key": key, "status": status, "bytes": len(text), "elapsed_s": elapsed}
        )
        if self.dump_dir is not None:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            (self.dump_dir / f"{self._n:03d}.txt").write_text(
                f"# {key}\n# status={status} bytes={len(text)} elapsed={elapsed:.2f}s\n\n{text}",
                encoding="utf-8",
            )

    def flush(self) -> None:
        if self.record_dir is None:
            return
        self.record_dir.mkdir(parents=True, exist_ok=True)
        path = self.record_dir / "responses.json"
        merged: dict[str, Any] = {}
        if path.exists():  # additive: a second channel's pass must not erase the first
            merged = json.loads(path.read_text(encoding="utf-8"))
        merged.update({k: _redact_value(v) for k, v in self.responses.items()})
        merged = {_redact_key(k): v for k, v in merged.items()}
        path.write_text(json.dumps(merged, indent=1, sort_keys=True), encoding="utf-8")
        print(f"\nrecorded {len(self.responses)} response(s) → {path}")


# Secrets that must never reach a committed cassette. Values come from the environment rather
# than a pattern, so a key format we have never seen is still caught.
_SECRET_ENV = ("SEMANTIC_SCHOLAR_API_KEY", "STACK_S2_API_KEY", "CORE_API_KEY", "STACK_MAILTO")


def _secrets() -> list[str]:
    return [v for name in _SECRET_ENV if (v := (os.environ.get(name) or "").strip())]


def _redact_key(key: str) -> str:
    for s in _secrets():
        key = key.replace(s, "REDACTED")
        key = key.replace(urllib.parse.quote(s, safe=""), "REDACTED")
    return key


def _redact_value(val: dict[str, Any]) -> dict[str, Any]:
    text = val.get("text") or ""
    for s in _secrets():
        text = text.replace(s, "REDACTED")
    return {**val, "text": text}


def _wrap_json(mod: Any, attr: str, rec: Recorder) -> None:
    """Wrap ``mod.<attr>`` — an ``http_get_json`` / ``http_post`` returning ``(status, data)``."""
    inner = getattr(mod, attr)

    def wrapped(url: str, payload: Any, *a: Any, **kw: Any) -> tuple[int, Any]:
        t0 = time.monotonic()
        status, data = inner(url, payload, *a, **kw)
        params = payload if attr.endswith("get_json") else None
        body = None if attr.endswith("get_json") else payload
        rec.bank(url, params, body, status, json.dumps(data), time.monotonic() - t0)
        return status, data

    setattr(mod, attr, wrapped)


def _wrap_text(mod: Any, attr: str, rec: Recorder) -> None:
    """Wrap ``mod.<attr>`` — an ``http_get`` returning ``(status, text)`` (arXiv's Atom)."""
    inner = getattr(mod, attr)

    def wrapped(url: str, params: dict[str, Any], *a: Any, **kw: Any) -> tuple[int, str]:
        t0 = time.monotonic()
        status, text = inner(url, params, *a, **kw)
        rec.bank(url, params, None, status, text, time.monotonic() - t0)
        return status, text

    setattr(mod, attr, wrapped)


# ─────────────────────────────────────────────────────────────────────────────
# Per-family adapters
#
# Each returns (units, run) where `units` is [(label, payload)] and `run(payload)` produces one
# Probe. Everything substantive — the query string, the pagination, the identity — comes from the
# workflow module; the adapter only chooses arguments and reshapes the result for printing.
# ─────────────────────────────────────────────────────────────────────────────
class Probe:
    def __init__(
        self, label: str, request: str, exhausted: str,
        items: list[dict[str, Any]], note: str = "",
    ) -> None:
        self.label = label
        self.request = request
        self.exhausted = exhausted
        self.items = items
        self.note = note


def _adapt_openalex(mod: Any, cfg: Any, args: argparse.Namespace, rec: Recorder) -> Any:
    _wrap_json(mod, "http_get_json", rec)
    chan = cfg.channels.channel(mod.CHANNEL)
    floor = (os.environ.get("STACK_OPENALEX_FROM") or chan.from_date or mod.DEFAULT_FROM).strip()
    units = [(f"group:{g['id']}", g) for g in mod.plan_groups(cfg)]

    def run(g: dict[str, Any]) -> Probe:
        filt = mod.group_filter(g["terms"], floor, g["broad"])
        works, exhausted = mod.fetch_group(filt, args.limit)
        items = []
        for w in works:
            ident = mod.work_identity(w)
            if ident is None:
                items.append({"external_id": None, "note": "no identity — would be SKIPPED"})
                continue
            channel, ext = ident
            items.append({
                "channel": channel, "external_id": ext,
                "url": mod.work_url(w, channel, ext),
                "title": str(w.get("title") or w.get("display_name") or "")[:90],
                "date": mod.work_pub_date(w) or "", "doc_type": mod.doc_type_of(w),
            })
        return Probe(f"group:{g['id']}", f"GET {mod.API_BASE}?filter={filt}", exhausted, items)

    return units, run


def _adapt_s2(mod: Any, cfg: Any, args: argparse.Namespace, rec: Recorder) -> Any:
    _give_transport(mod, _ProbeRun())
    _wrap_json(mod, "http_get_json", rec)
    chan = cfg.channels.channel(mod.CHANNEL)
    floor = (os.environ.get("STACK_S2_FROM") or chan.from_date or mod.DEFAULT_FROM).strip()
    key = mod.api_key()
    headers = {"x-api-key": key} if key else {}
    units = [(f"group:{g['id']}", g) for g in mod.plan_groups(cfg)]

    def run(g: dict[str, Any]) -> Probe:
        papers, exhausted = mod.fetch_group(g["s2_query"], floor, args.limit, headers)
        items = []
        for p in papers:
            ident = mod.paper_identity(p)
            if ident is None:
                items.append({"external_id": None, "note": "no identity — would be SKIPPED"})
                continue
            channel, ext = ident
            items.append({
                "channel": channel, "external_id": ext,
                "url": mod.paper_url(p, channel, ext),
                "title": str(p.get("title") or "")[:90],
                "date": mod.paper_pub_date(p) or "", "doc_type": mod.doc_type_of(p),
            })
        note = "keyed" if key else "KEYLESS (throttled pool)"
        return Probe(f"group:{g['id']}", f"GET {mod.API_BASE}?query={g['s2_query']}",
                     exhausted, items, note)

    return units, run


def _adapt_arxiv(mod: Any, cfg: Any, args: argparse.Namespace, rec: Recorder) -> Any:
    _give_transport(mod, _ProbeRun())
    _wrap_text(mod, "http_get", rec)
    chan = cfg.channels.channel(mod.CHANNEL)
    floor = (os.environ.get("STACK_ARXIV_FROM") or chan.from_date or mod.DEFAULT_FROM).strip()
    units = [(f"author:{n}", n) for n in mod.load_roster(cfg)]

    def run(name: str) -> Probe:
        query = mod.arxiv_author_query(name)
        entries, exhausted = mod.fetch_author(query, mod._to_compact(floor), args.limit)
        items = [{
            "channel": mod.CHANNEL, "external_id": e.get("arxiv_id"),
            "url": f"https://arxiv.org/abs/{e.get('arxiv_id')}",
            "title": str(e.get("title") or "")[:90],
            "date": e.get("pub_date") or "",
        } for e in entries]
        return Probe(f"author:{name}", f"GET arxiv search_query={query}", exhausted, items)

    return units, run


class _ProbeRun:
    """Stands in for the stage's ``Collector``: a workflow helper reads ``option`` (the probe has
    no plan line, so every option is its default; the helper's env override still wins), and
    :class:`Transport` reports to ``counts``/``beat``/``halt`` (kept in memory, never written)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.halted: RunHalted | None = None

    def option(self, name: str, default: Any, **_: Any) -> Any:
        return default

    def beat(self, **_: Any) -> None:
        pass

    def halt(self, retry_after_s: float, reason: str) -> RunHalted:
        if self.halted is None:
            self.halted = RunHalted(retry_after_s, reason)
        return self.halted


def _give_transport(mod: Any, run: _ProbeRun) -> None:
    """Set ``mod._T`` to the stage's own paced transport, as the module's ``run(ctx)`` does."""
    mod._T = Transport(run, mod.PACING)  # type: ignore[arg-type]  # duck-typed Collector


def _adapt_forums(mod: Any, cfg: Any, args: argparse.Namespace, rec: Recorder) -> Any:
    probe_run = _ProbeRun()
    _give_transport(mod, probe_run)
    _wrap_json(mod, "http_post", rec)
    tags = mod._tags(probe_run)
    units: list[tuple[str, Any]] = []
    for cid in mod.FORUM_CHANNELS:
        chan = cfg.channels.channel(cid)
        for item in mod.plan_queries(cfg, chan, tags):
            units.append((f"{cid}/{mod._query_label(item)}", (cid, chan, item)))

    def run(payload: tuple[str, Any, dict[str, Any]]) -> Probe:
        cid, chan, item = payload
        hits, exhausted = mod.fetch_forum(
            chan.location, item["term"], item["tag"], args.limit, None
        )
        items = [{
            "channel": cid, "external_id": h.get("_id") or h.get("objectID"),
            "url": f"{chan.location.rstrip('/')}/posts/{h.get('_id') or h.get('objectID')}",
            "title": str(h.get("title") or "")[:90], "date": mod._posted_date(h) or "",
        } for h in hits]
        return Probe(f"{cid}/{mod._query_label(item)}",
                     f"POST {chan.location} q={item['term']!r} tag={item['tag']!r}",
                     exhausted, items)

    return units, run


def _adapt_af(mod: Any, cfg: Any, args: argparse.Namespace, rec: Recorder) -> Any:
    _give_transport(mod, _ProbeRun())
    _wrap_json(mod, "http_post", rec)
    chan = cfg.channels.channel(mod.CHANNEL)
    floor = (os.environ.get("STACK_AF_FROM") or chan.from_date or mod.DEFAULT_FROM).strip()

    def run(_: Any) -> Probe:
        posts, exhausted = mod.fetch_af(chan.location, floor, args.limit)
        items = [{
            "channel": mod.CHANNEL, "external_id": p.get("_id"),
            "url": f"{chan.location.rstrip('/')}/posts/{p.get('_id')}",
            "title": str(p.get("title") or "")[:90],
            "date": mod._posted_date(p) or "",
        } for p in posts]
        return Probe("af:all", f"POST {chan.location} graphql after={floor}", exhausted, items)

    return [("af:all", None)], run


def _adapt_listing(family: str) -> Any:
    """blogs / cards — both ride ``stack.collect.listing``, so one adapter covers them.

    The listing engine already owns a recording-shaped seam (``HttpFetcher`` behind
    ``resolve_fetcher``), so instead of wrapping a module function we hand ``scan_surface`` a
    fetcher that banks what it serves.
    """
    def build(mod: Any, cfg: Any, args: argparse.Namespace, rec: Recorder) -> Any:
        from stack.collect.listing import HttpFetcher, scan_surface

        specs = mod.SITE_SPECS if family == "blogs" else mod.WATCH_SPECS
        kinds = {"blogs": "scrape", "cards": "watch"}[family]

        class _RecordingFetcher(HttpFetcher):
            def get(self, url: str, *, headers: Any = None, params: Any = None) -> Any:
                t0 = time.monotonic()
                res = super().get(url, headers=headers, params=params)
                rec.bank(url, params, None, res.status, res.text or "", time.monotonic() - t0)
                return res

        fetcher = _RecordingFetcher()
        units: list[tuple[str, Any]] = []
        for c in cfg.channels.channels:
            if c.kind != kinds or c.id not in specs:
                continue
            entry = specs[c.id]
            surfaces = entry["surfaces"] if isinstance(entry, dict) else entry.surfaces
            for sp in surfaces:
                units.append((f"{c.id}/{sp.key}", (c.id, sp)))

        def run(payload: tuple[str, Any]) -> Probe:
            cid, sp = payload
            # conn=None is safe: only the github-org method reads it (for its watermark), and
            # `full=True` takes the bootstrap branch that never calls prior_watermark.
            scan = scan_surface(sp, cid, None, fetcher, full=True)
            items = []
            for cand in scan.candidates[: args.limit]:
                if family == "blogs":
                    ext = mod.external_id_for(cand, specs[cid])
                else:
                    ext = cand.slug
                items.append({
                    "channel": cid, "external_id": ext, "url": cand.url,
                    "title": (cand.text or "")[:90], "date": cand.date_hint,
                })
            note = f"{len(scan.candidates)} candidate(s); {json.dumps(scan.note)}"
            return Probe(f"{cid}/{sp.key}", f"{sp.method} {', '.join(sp.urls)}",
                         scan.exhausted, items, note)

        return units, run

    return build


ADAPTERS = {
    "openalex": _adapt_openalex,
    "semantic_scholar": _adapt_s2,
    "arxiv_sweep": _adapt_arxiv,
    "forums": _adapt_forums,
    "af_graphql": _adapt_af,
    "blogs": _adapt_listing("blogs"),
    "cards": _adapt_listing("cards"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Printing
# ─────────────────────────────────────────────────────────────────────────────
_MARK = {"complete": "ok", "truncated": "cap", "error": "ERR"}


def _print_probe(p: Probe, rec: Recorder) -> None:
    print(f"\n── {p.label}  [{_MARK.get(p.exhausted, p.exhausted)}: {p.exhausted}]")
    print(f"   request: {p.request}")
    if p.note:
        print(f"   note:    {p.note}")
    for ex in rec.exchanges:
        print(f"   http:    #{ex['n']} status={ex['status']} bytes={ex['bytes']} "
              f"{ex['elapsed_s']:.2f}s")
    rec.exchanges.clear()
    if not p.items:
        print("   items:   none")
        return
    print(f"   items:   {len(p.items)} (would be minted as)")
    for it in p.items:
        if not it.get("external_id"):
            print(f"     - {it.get('note', 'no identity')}")
            continue
        print(f"     - {it['channel']}/{it['external_id']}")
        print(f"       {it.get('date') or '????-??-??'}  {it.get('title') or '(no title)'}")
        print(f"       {it.get('url') or ''}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--workflow", required=True, choices=sorted(FAMILIES),
                    help="collect workflow id to probe")
    ap.add_argument("--query", action="append", default=[],
                    help="substring of the unit label (repeatable; default: the first --max)")
    ap.add_argument("--limit", type=int, default=5, help="items per query (default 5)")
    ap.add_argument("--max", type=int, default=1,
                    help="max units to probe when --query is not given (default 1)")
    ap.add_argument("--list", action="store_true",
                    help="list the unit labels this workflow would run and exit (no network)")
    ap.add_argument("--dump", type=Path, default=None, help="write raw responses here")
    ap.add_argument("--record", type=Path, default=None,
                    help="write a ReplayFetcher cassette (responses.json) here")
    args = ap.parse_args()

    family = FAMILIES[args.workflow]
    cfg = config_mod.load(REPO / "configs")
    mod = _load_wf(family)
    rec = Recorder(args.dump, args.record)
    units, run = ADAPTERS[family](mod, cfg, args, rec)

    if args.list:
        for label, _ in units:
            print(label)
        print(f"\n{len(units)} unit(s) in {args.workflow}'s plan", file=sys.stderr)
        return

    if args.query:
        wanted = [(lbl, p) for lbl, p in units
                  if any(q.lower() in lbl.lower() for q in args.query)]
        if not wanted:
            print(f"error: no unit label matched {args.query!r}. "
                  f"Try --list.", file=sys.stderr)
            raise SystemExit(2)
    else:
        wanted = units[: args.max]

    print(f"probing {args.workflow}: {len(wanted)} unit(s), limit={args.limit} — NO WRITES")

    errors = 0
    try:
        for label, payload in wanted:
            try:
                probe = run(payload)
            except Exception as exc:  # noqa: BLE001 — a probe must report, never abort the sweep
                errors += 1
                print(f"\n── {label}  [ERR: raised]")
                print(f"   {type(exc).__name__}: {exc}")
                continue
            if probe.exhausted == "error":
                errors += 1
            _print_probe(probe, rec)
    finally:
        rec.flush()

    print(f"\n{len(wanted)} probed, {errors} error(s)")
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
