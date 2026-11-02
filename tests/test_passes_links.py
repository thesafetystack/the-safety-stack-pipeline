"""Passes for ``collect.links`` (round 4, D221/D226 harness ``tests/test_passes_collect.py``; track L's
stage, D222-D225): two passes of the newsletter part of the collect phase, ``collect.feeds``
(Zvi's feed, the frozen fixture of ``tests/test_collect_feeds.py``), ``gather``, ``collect.links``
(the fake selector of ``tests/test_collect_links.py`` on the LLM transport seam), ``gather``,
through the real runner and stage modules. Pass 2, with pass 1's ``sources``/``coverage`` as
priors and the new ``documents`` head as input (as ``stack build --passes 2`` gives it), makes no
request and no LLM call: every link was decided in pass 1, so nothing is re-asked, no
``sources``/``coverage`` row is written and gather touches no row. A separate file from the
collector harness because its world (feed bodies, an LLM) is not the harness's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import tests.fakes.llm as fake_llm
from stack.llm import LlmRequest, LlmResponse
from stack.plan import Plan
from stack.store.chain import read_view, resolve
from tests.synth.records import sdir
from tests.test_collect_feeds import FEED_URLS, fixture_bodies
from tests.test_collect_links import FakeSelector
from tests.test_passes_collect import World, _rows, _touched, run_pass, world  # noqa: F401 (fixture)

#: One pass of the newsletter path: the feed, a gather (collect.links reads the documents head),
#: the link selector, a gather (the link targets become documents).
PASS = ("collect.feeds", "gather", "collect.links", "gather")


class FeedWorld(World):
    """:class:`World` serving the frozen Zvi feed at its channel URL (every other URL as World)."""

    def __init__(self) -> None:
        super().__init__()
        self.feeds = fixture_bodies()

    def __call__(self, method: str, url: str, **kw: Any) -> Any:
        if url == FEED_URLS["zvi_substack"]:
            self.calls.append((method, url, url))
            return self._text(url, self.feeds[url].decode("utf-8"), "application/rss+xml")
        return super().__call__(method, url, **kw)


def links_plan() -> Plan:
    """links-scratch's newsletter lines (``configs/plans/links-scratch.yaml``), Zvi only."""
    return Plan.model_validate({
        "plan": "passes-links", "schema_version": 2, "series": "main",
        "prompts": {"collect.links": {"name": "link-select-v1", "model": "openai/gpt-6-luna"}},
        "stages": [
            {"stage": "collect.feeds", "prior": ["sources", "coverage"],
             "options": {"only": ["zvi_substack"], "since": "2026-09-12"}},
            {"stage": "collect.links", "prior": ["sources", "coverage"], "miss_cap": 1,
             "budget": {"max_usd": 1.0}, "options": {"since": "2026-09-12"}},
            {"stage": "gather"},
        ]})


@pytest.fixture
def feed_world(world: World, monkeypatch: pytest.MonkeyPatch) -> FeedWorld:  # noqa: F811 (env, sleep, lookup seams)
    w = FeedWorld()
    monkeypatch.setattr("stack.collect.base.urllib_send", w)
    return w


class SelectAll(FakeSelector):
    """:class:`FakeSelector` that selects every item it is asked (the fixture feed's links match
    none of its :data:`~tests.test_collect_links.SELECT` substrings), so pass 1 writes link rows."""

    def post(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        lines = [json.loads(ln) for ln in fake_llm.prompt_text(request).splitlines() if ln.startswith('{"i":')]
        return fake_llm.ok({"items": [{"i": it["i"], "decision": "select", "kind": "other",
                                       "reason": f"fake: {it['target']}"} for it in lines]}, cost=0.0005)


@pytest.fixture
def selector(monkeypatch: pytest.MonkeyPatch) -> FakeSelector:
    f = SelectAll()
    monkeypatch.setattr(fake_llm, "INSTALLED", f)
    return f


@pytest.mark.slow  # real store + runner, 8 runs
def test_second_pass_of_links_asks_nothing(tmp_path: Path, feed_world: FeedWorld,
                                           selector: FakeSelector) -> None:
    store, plan = tmp_path / "store", links_plan()
    p1 = run_pass(store, plan, feed_world, PASS)
    calls1, req1 = len(selector.requests), len(feed_world.calls)
    links1 = p1["collect.links"]["rec"]
    print(f"pass 1: requests {req1}, LLM calls {calls1}, links counts {dict(links1.counts)}")
    # pass 1 did real work: the feed was read, the selector asked, link rows written and gathered
    assert p1["collect.feeds"]["requests"] == 1 and calls1 > 0
    assert links1.counts["selector_calls"] == calls1 and _rows(links1) > 0
    assert _touched(p1["gather#2"]["rec"]) > 0
    n_links = len(read_view(resolve(store, sdir("sources.links"))).to_pylist())

    p2 = run_pass(store, plan, feed_world, PASS)
    links2 = p2["collect.links"]["rec"]
    print(f"pass 2: requests {len(feed_world.calls) - req1}, LLM calls {len(selector.requests) - calls1}, "
          f"links counts {dict(links2.counts)}")
    assert len(feed_world.calls) == req1  # the feed was read within relist_after_h (D226 (b))
    assert len(selector.requests) == calls1  # 0 LLM calls: every link was decided in pass 1
    assert links2.counts.get("selector_calls", 0) == 0 and links2.counts.get("llm_calls", 0) == 0
    assert links2.counts["selector_cached"] == links1.counts["selector_items_asked"]
    assert _rows(links2) == 0 and links2.outputs == {}
    assert {s: _rows(p2[s]["rec"]) for s in ("collect.feeds", "collect.links")} == {
        "collect.feeds": 0, "collect.links": 0}
    assert _touched(p2["gather"]["rec"]) == 0 and _touched(p2["gather#2"]["rec"]) == 0
    assert len(read_view(resolve(store, sdir("sources.links"))).to_pylist()) == n_links
