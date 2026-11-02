"""``tools/probe_collect.py`` without network: its adapter table against the stage modules it loads.

The probe imports each ``workflows/collect/<name>/main.py`` by path and calls module attributes by
name, so a renamed workflow helper breaks it only when someone runs it live (F31-4: the forums
adapter called ``_tags_from_env`` for months after it became ``_tags(c)``). These tests pin that
every name an adapter reads resolves, that every adapter builds its unit list offline, that
the forums adapter builds its query (plan, tags, request line) with the HTTP seam refusing, and
that the four sweeps whose seam needs a run's transport (F31-27) get the stage's own transport
and send through it (to a fake sender that refuses every request).
"""

from __future__ import annotations

import argparse
import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

import probe_collect  # tests/conftest.py puts tools/ on sys.path

pytestmark = pytest.mark.tools

#: Adapter function name -> the families (``workflows/collect/<family>``) it serves.
ADAPTER_FAMILIES = {
    "_adapt_openalex": ("openalex",),
    "_adapt_s2": ("semantic_scholar",),
    "_adapt_arxiv": ("arxiv_sweep",),
    "_adapt_forums": ("forums",),
    "_adapt_af": ("af_graphql",),
    "_adapt_listing": ("blogs", "cards"),
}


def _args(limit: int = 3) -> argparse.Namespace:
    return argparse.Namespace(limit=limit)


def _names_read(fn_name: str) -> set[str]:
    """``mod.<name>`` reads and ``_wrap_json/_wrap_text(mod, "<name>", ...)`` seams in one adapter."""
    tree = ast.parse(Path(inspect.getsourcefile(probe_collect) or "").read_text("utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    names = {n.attr for n in ast.walk(fn)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "mod"}
    names |= {n.args[1].value for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id in ("_wrap_json", "_wrap_text") and isinstance(n.args[1], ast.Constant)}
    return names


def test_the_adapter_table_covers_every_workflow_choice() -> None:
    assert set(probe_collect.ADAPTERS) == set(probe_collect.FAMILIES.values())
    assert {f for fams in ADAPTER_FAMILIES.values() for f in fams} == set(probe_collect.ADAPTERS)


@pytest.mark.parametrize("adapter", sorted(ADAPTER_FAMILIES))
def test_every_name_an_adapter_reads_resolves_on_its_modules(adapter: str) -> None:
    """A single-family adapter's names all resolve on its module; the shared listing adapter picks
    per family (``SITE_SPECS``/``external_id_for`` for blogs, ``WATCH_SPECS`` for cards), so each
    of its names resolves on at least one of the two."""
    names = _names_read(adapter)
    assert names, adapter
    mods = {f: probe_collect._load_wf(f) for f in ADAPTER_FAMILIES[adapter]}
    if len(mods) == 1:
        (mod,) = mods.values()
        assert sorted(n for n in names if not hasattr(mod, n)) == []
    else:
        assert sorted(n for n in names if not any(hasattr(m, n) for m in mods.values())) == []
        assert {"SITE_SPECS", "external_id_for"} <= set(dir(mods["blogs"]))
        assert hasattr(mods["cards"], "WATCH_SPECS")


@pytest.mark.parametrize("workflow", sorted(probe_collect.FAMILIES))
def test_every_adapter_builds_its_units_offline(workflow: str, config: Any, tmp_path: Path) -> None:
    family = probe_collect.FAMILIES[workflow]
    mod = probe_collect._load_wf(family)
    rec = probe_collect.Recorder(None, None)
    units, run = probe_collect.ADAPTERS[family](mod, config, _args(), rec)
    assert units and callable(run)  # labels may repeat: a term in two concept groups is two units


def test_the_forums_adapter_builds_its_query_without_network(
    config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = probe_collect._load_wf("forums")

    def refuse(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("network")

    monkeypatch.setattr(mod, "http_post", refuse)
    monkeypatch.delenv("STACK_FORUMS_TAGS", raising=False)
    rec = probe_collect.Recorder(None, None)
    units, run = probe_collect.ADAPTERS["forums"](mod, config, _args(limit=2), rec)
    labels = [label for label, _ in units]
    for cid in mod.FORUM_CHANNELS:  # no plan line: the module's default tags, on both forums
        assert [lb for lb in labels if lb.startswith(f"{cid}/tag:")] == [
            f"{cid}/tag:{t}" for t in mod.DEFAULT_TAGS]
        assert f"{cid}/kw:{mod.EXTRA_KEYWORDS[0]}" in labels

    seen: list[tuple[Any, ...]] = []

    def fake_fetch(base_url: str, query: str, tag: str | None, limit: int, *a: Any) -> Any:
        seen.append((base_url, query, tag, limit))
        return [{"_id": "p1", "title": "A post", "postedAt": "2025-01-02T00:00:00Z"}], "complete"

    monkeypatch.setattr(mod, "fetch_forum", fake_fetch)
    label, payload = next(u for u in units if u[0].endswith(f"tag:{mod.DEFAULT_TAGS[0]}"))
    probe = run(payload)
    chan = config.channels.channel(label.split("/")[0])
    assert seen == [(chan.location, "", mod.DEFAULT_TAGS[0], 2)]
    assert probe.request == f"POST {chan.location} q='' tag={mod.DEFAULT_TAGS[0]!r}"
    assert probe.exhausted == "complete"
    assert [(i["external_id"], i["date"]) for i in probe.items] == [("p1", "2025-01-02")]

    monkeypatch.setenv("STACK_FORUMS_TAGS", "rlhf, ai-control")  # the stage's env override
    units, _ = probe_collect.ADAPTERS["forums"](mod, config, _args(), rec)
    assert [lb for lb, _ in units if lb.startswith(f"{mod.FORUM_CHANNELS[0]}/tag:")] == [
        f"{mod.FORUM_CHANNELS[0]}/tag:rlhf", f"{mod.FORUM_CHANNELS[0]}/tag:ai-control"]


#: The sweeps whose ``http_*`` seam raises "no transport outside run(ctx)" without the probe's
#: transport, with the host their first request goes to.
TRANSPORT_FAMILIES = {"semantic_scholar": "api.semanticscholar.org", "arxiv_sweep": "export.arxiv.org",
                      "forums": "www.lesswrong.com", "af_graphql": "www.alignmentforum.org"}


@pytest.mark.parametrize("family", [pytest.param(f, marks=pytest.mark.slow) if f in ("arxiv_sweep", "semantic_scholar") else f
                                    for f in sorted(TRANSPORT_FAMILIES)])
def test_the_probe_gives_run_only_sweeps_the_stage_transport(
    family: str, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.parse

    from stack.collect import base
    from stack.http import HttpResponse

    sent: list[tuple[str, str]] = []

    def refuse(method: str, url: str, **_: Any) -> HttpResponse:  # the network, refused
        sent.append((method, url))
        return HttpResponse(None, {}, b"", url, error="connection")

    monkeypatch.setattr(base, "urllib_send", refuse)
    mod = probe_collect._load_wf(family)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    assert mod._T is None
    units, run = probe_collect.ADAPTERS[family](mod, config, _args(limit=2), probe_collect.Recorder(None, None))
    assert isinstance(mod._T, base.Transport) and mod._T.pacing is mod.PACING
    probe = run(units[0][1])  # without the transport: RuntimeError("... no transport outside run(ctx)")
    assert probe.exhausted == "error" and probe.items == []
    assert sent and {urllib.parse.urlsplit(u).hostname for _, u in sent} == {TRANSPORT_FAMILIES[family]}
