"""Tests for live unit progress (``stack.progress``).

The behaviours that matter are the ones that bite in a long run: a captured (non-TTY) stream must
stay readable, a broken pipe must not kill a sweep mid-flight, and a workflow that reports
progress must behave identically when nothing is listening.
"""

from __future__ import annotations

import io

import pytest

from stack.progress import (
    ENV_DISABLE,
    NullProgress,
    Progress,
    from_env,
    resolve,
)


class _Broken(io.StringIO):
    """A stream that fails on write — a closed pipe, an already-torn-down terminal."""

    def write(self, s: str) -> int:
        raise OSError("broken pipe")


def test_tty_rewrites_one_line_and_ends_with_a_newline() -> None:
    """On a terminal the bar is ONE line that rewrites itself; the final render ends it, so the
    shell prompt does not land mid-bar."""
    out = io.StringIO()
    p = Progress("collect.arxiv_sweep", stream=out, isatty=True)
    p.start(3, unit="authors")
    for name in ("a", "b", "c"):
        p.step(name)
    p.finish()
    text = out.getvalue()
    assert text.count("\n") == 1 and text.endswith("\n")
    assert "\r" in text
    assert "collect.arxiv_sweep" in text and "3/3" in text and "done in" in text


def test_non_tty_emits_whole_lines_and_throttles_by_time() -> None:
    """Captured output (a log file, a background task) gets newline-terminated lines — and NOT one
    per unit: a 2,269-unit sweep must leave a readable trail, not 2,269 lines."""
    out = io.StringIO()
    p = Progress("collect.arxiv_sweep", stream=out, isatty=False)
    p.start(500, unit="authors")
    for i in range(500):
        p.step(f"author-{i}")
    p.finish()
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert "\r" not in out.getvalue()
    # start + finish are forced; the 500 steps are time-throttled and collapse to nothing here
    assert 2 <= len(lines) <= 4, lines
    assert lines[-1].endswith(f"done in {lines[-1].split('done in ')[1]}")


def test_progress_reports_share_and_eta() -> None:
    out = io.StringIO()
    p = Progress("collect.forums", stream=out, isatty=True, min_interval_s=0.0)
    p.start(10, unit="queries")
    for _ in range(5):
        p.step("kw:RLHF")
    body = out.getvalue()
    assert "5/10" in body and "50%" in body and "eta" in body


def test_note_is_truncated_not_wrapped() -> None:
    """A long unit label (a URL) must not wrap the bar onto a second line."""
    out = io.StringIO()
    p = Progress("fetch.http", stream=out, isatty=True, min_interval_s=0.0)
    p.start(2, unit="sources")
    p.step("https://example.org/" + "x" * 200)
    assert max(len(ln) for ln in out.getvalue().split("\r")) < 160


def test_a_broken_stream_does_not_kill_the_run() -> None:
    """Progress is decoration: a dead terminal must never take a two-hour sweep down with it."""
    p = Progress("collect.openalex", stream=_Broken(), isatty=False)
    p.start(3, unit="groups")
    p.step("group:rlhf")     # must not raise
    p.finish()


def test_step_before_start_is_a_no_op() -> None:
    out = io.StringIO()
    p = Progress("x", stream=out, isatty=True)
    p.step("nothing started")
    assert out.getvalue() == ""


def test_zero_total_renders_nothing_until_finish() -> None:
    """An empty plan is a legal state (everything filtered out) and must not divide by zero."""
    out = io.StringIO()
    p = Progress("collect.cards", stream=out, isatty=True)
    p.start(0, unit="surfaces")
    p.finish()
    assert "0/0" in out.getvalue()


@pytest.mark.parametrize("setting", ["0", "off", "false", "no", "OFF"])
def test_env_can_silence_progress(monkeypatch: pytest.MonkeyPatch, setting: str) -> None:
    monkeypatch.setenv(ENV_DISABLE, setting)
    assert isinstance(from_env("collect.forums"), NullProgress)


def test_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent long run is the problem this exists to fix, so reporting is the no-opt-in path."""
    monkeypatch.delenv(ENV_DISABLE, raising=False)
    assert isinstance(from_env("collect.forums", stream=io.StringIO()), Progress)


def test_resolve_tolerates_a_services_without_progress() -> None:
    """Workflows call `resolve(services)`, never the attribute, so a Services built by a test or
    by older code cannot raise AttributeError halfway through a sweep."""

    class Bare:
        pass

    reporter = resolve(Bare())
    assert isinstance(reporter, NullProgress)
    reporter.start(5, unit="units")   # the no-op honours the full protocol
    reporter.step("x")
    reporter.finish()


def test_first_step_renders_immediately_on_a_tty() -> None:
    """The first unit's label must appear when it STARTS. Before this the first step landed in
    start()'s throttle window, so a 25-minute first query showed 0/N the whole way."""
    out = io.StringIO()
    p = Progress("collect.forums", stream=out, isatty=True, min_interval_s=60.0)
    p.start(312, unit="queries")
    p.step("lesswrong term:alignment")
    text = out.getvalue()
    assert "1/312" in text and "lesswrong term:alignment" in text


def test_ticks_show_items_inside_the_unit_in_flight() -> None:
    """Items inside a unit are counted with a rate, and reset when the next unit starts."""
    out = io.StringIO()
    p = Progress("collect.openalex", stream=out, isatty=True, min_interval_s=0.0)
    p.start(2, unit="queries", sub_unit="works")
    p.step("group:a")
    for _ in range(1500):
        p.tick()
    frames = out.getvalue().split("\r")
    assert any("1,500 works (" in f and "/s)" in f for f in frames), frames[-1]
    p.step("group:b")
    assert "works" not in out.getvalue().split("\r")[-1]  # sub-count reset with the new unit
    p.finish()
    assert out.getvalue().split("\r")[-1].rstrip("\n").endswith(out.getvalue().split("done in ")[1].rstrip("\n"))


def test_tick_before_start_is_a_no_op() -> None:
    out = io.StringIO()
    p = Progress("x", stream=out, isatty=True)
    p.tick()
    assert out.getvalue() == ""
    NullProgress().tick()  # the default reporter accepts it too


def test_note_relabels_without_advancing_any_counter() -> None:
    """The network phase of a unit has nothing to count yet: `step` would lie about progress and
    `tick` would double-count the items the processing loop is about to tick."""
    out = io.StringIO()
    p = Progress("collect.arxiv_sweep", stream=out, isatty=True, min_interval_s=0.0)
    p.start(10, unit="authors", sub_unit="entries")
    p.step("Chris Olah")
    p.note("fetching, 400 entries")
    frame = out.getvalue().split("\r")[-1]
    assert "fetching, 400 entries" in frame
    assert "Chris Olah" in frame           # the unit label is kept; the phase is added to it
    assert "1/10" in frame                 # the unit counter did not move
    assert "entries (" not in frame        # and nothing was counted as processed


def test_a_tick_clears_a_stale_fetch_phase() -> None:
    """Once items are being processed the network phase is over. A "fetching…" label left under a
    ticking counter would misreport what the run is doing."""
    out = io.StringIO()
    p = Progress("collect.arxiv_sweep", stream=out, isatty=True, min_interval_s=0.0)
    p.start(10, unit="authors", sub_unit="entries")
    p.step("Chris Olah")
    p.note("fetching, 400 entries")
    p.tick(3)
    frame = out.getvalue().split("\r")[-1]
    assert "fetching" not in frame
    assert "3 entries (" in frame and "Chris Olah" in frame


def test_note_before_start_is_a_no_op() -> None:
    out = io.StringIO()
    Progress("x", stream=out, isatty=True).note("anything")
    assert out.getvalue() == ""
    NullProgress().note("anything")  # the default reporter accepts it too


def test_a_step_replaces_a_note_from_the_previous_unit() -> None:
    """A stale 'fetching' label under the next unit would be a lie about what is happening."""
    out = io.StringIO()
    p = Progress("collect.forums", stream=out, isatty=True, min_interval_s=0.0)
    p.start(2, unit="queries", sub_unit="hits")
    p.step("lesswrong q1")
    p.note("fetching, 900 hits")
    p.step("lesswrong q2")
    last = out.getvalue().split("\r")[-1]
    assert "lesswrong q2" in last and "fetching" not in last
