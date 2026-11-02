"""R4-2 (round 5, T-RUN B9): the long recheck of terminal fetch roles and ``stack acquire reopen``.

The lane cases (``atom_unknown``, an empty batch, a 200 non-PDF, DNS) are in
``tests/test_acquire_lanes.py``; the backoff/cap/recheck sequence in ``tests/test_acquire_core.py``.
Here: :func:`stack.fetchcycle.role_failed`'s recheck per class, :class:`stack.fetchcycle.Reopen`,
:func:`workflows.acquire.main.reopen_rows`, and the CLI (dry run, then the run that fetches).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stack.cli import app
from stack.fetchcycle import TERMINAL_RECHECK, Reopen, role_failed
from tests.fakes.http import FakeHttp
from tests.synth.records import sdir
from tests.synth.rows import sid
from tests.test_acquire_lanes import (
    DAY,
    NOW,
    aid_of,
    arxiv_plan,
    arxiv_row,
    atom,
    checks_ok,
    empty_atom,
    full_role,
    go,
    pdf_of,
    raws,
    store,
    view,
)
from workflows.acquire import lanes
from workflows.acquire.main import reopen_rows

Row = dict[str, Any]
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def role(status: str = "failed_permanent", error_class: str = "dead", *, lane: str = "arxiv",
         error: str | None = None, last: dt.datetime = T0) -> Row:
    return {"lane": lane, "status": status, "error_class": error_class, "error": error, "attempts": 4,
            "last_attempt_at": last, "retry_after": None, "source_id": "s1", "content_role": "full_text"}


@pytest.mark.parametrize(("status", "cls", "recheck"), [
    ("failed_permanent", "dead", True),
    ("failed_permanent", "blocked", True),
    ("failed_permanent", "no_fetchable_url", True),
    ("failed_permanent", "paywalled", False),
    ("failed_permanent", "too_large", False),
    ("failed_transient", "network", True),  # at the cap: quarantined[cap_reached]
])
def test_terminal_recheck_per_class(status: str, cls: str, recheck: bool) -> None:
    out = role_failed({"attempts": 3}, T0, status=status, error_class=cls, error="x")
    if status == "failed_transient":
        assert (out["status"], out["error_class"], out["error"]) == ("quarantined", "cap_reached", "network: x")
    assert out["retry_after"] == (T0 + TERMINAL_RECHECK if recheck else None)
    assert TERMINAL_RECHECK == dt.timedelta(days=90)


def test_reopen_matches() -> None:
    every = Reopen("arxiv")
    assert every.matches(role()) and every.matches(role("failed_transient", "network"))
    assert every.matches(role("quarantined", "cap_reached", error="http_5xx: down"))
    assert not every.matches(role(lane="oa")) and not every.matches(role("ok", None))  # type: ignore[arg-type]
    assert not every.matches(role("skipped", "not_needed"))
    dead = Reopen("arxiv", "dead")
    assert dead.matches(role()) and not dead.matches(role("failed_transient", "network"))
    five = Reopen("arxiv", "http_5xx")  # the class a quarantine came from
    assert five.matches(role("quarantined", "cap_reached", error="http_5xx: down"))
    assert not five.matches(role("quarantined", "cap_reached", error="network: x"))
    assert Reopen("arxiv", "cap_reached").matches(role("quarantined", "cap_reached", error="network: x"))
    since = Reopen.parse({"lane": "arxiv", "since": "2026-10-01"})
    assert since.since == T0 and since.matches(role(last=T0)) and not since.matches(role(last=T0 - DAY))
    assert Reopen.parse(since.as_option()) == since
    for bad in ({}, {"lane": ""}, {"lane": "arxiv", "since": "yesterday"}, {"lane": "arxiv", "class": "x"}):
        with pytest.raises(ValueError):
            Reopen.parse(bad)


def test_reopen_rows_makes_due_and_copies() -> None:
    rows = [{"doc_id": "d1", "content": {"roles": [role(), role(lane="oa")]}},
            {"doc_id": "d2", "content": None},
            {"doc_id": "d3", "content": {"roles": [role("failed_transient", "network")]}}]
    counts: dict[str, int] = {}
    now = T0 + 5 * DAY
    out = reopen_rows(rows, Reopen("arxiv", "dead"), now, counts)
    assert counts == {"reopen.roles": 1, "reopen.rows": 1}
    assert [r["retry_after"] for r in out[0]["content"]["roles"]] == [now, None]
    assert rows[0]["content"]["roles"][0]["retry_after"] is None  # the input is not modified
    assert out[1] is rows[1] and out[2] is rows[2]
    counts = {}
    assert reopen_rows(rows, Reopen("arxiv"), now, counts, only_ids={"d3"})[0] is rows[0]
    assert counts == {"reopen.roles": 1, "reopen.rows": 1}


# ---------------------------------------------------------------- the CLI over a store
runner = CliRunner()


def _plan_file(tmp_path: Path) -> Path:
    p = tmp_path / "plan.yaml"
    p.write_text(json.dumps(arxiv_plan().model_dump(mode="json")), "utf-8")
    return p


def _cli(root: Path, plan: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--store", str(root), "--plan", str(plan), "acquire", "reopen", "--in", sdir("documents"), *args],
                        env={"STACK_PLAN": None})
    assert res.exit_code == code, res.output
    return res.output


@pytest.mark.slow
def test_reopen_cli_after_an_outage(tmp_path: Path) -> None:
    """Four runs against a feed that lists nothing end the role ``failed_permanent[dead]`` (due in
    90 days). The outage is known to be over: ``--dry-run`` names the role and writes nothing;
    ``--error-class network`` matches nothing; the real reopen (one acquire run) fetches it now and
    the role is ``ok``."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    plan = _plan_file(tmp_path)
    fake = FakeHttp()
    empty_atom(fake, 1)
    at = NOW
    for _ in range(4):
        go(root, fake, at, plan=arxiv_plan())
        at = full_role(view(root)[d])["retry_after"]
    r = full_role(view(root)[d])
    assert (r["status"], r["error_class"], r["attempts"]) == ("failed_permanent", "dead", 4)
    runs_before = sorted(p.name for p in (root / "runs").iterdir())

    out = _cli(root, plan, "--lane", "arxiv", "--dry-run", "--json")
    got = json.loads(out)
    assert (got["roles"], got["rows"], got["by_status"]) == (1, 1, {"failed_permanent[dead]": 1})
    assert json.loads(_cli(root, plan, "--lane", "arxiv", "--error-class", "network", "--dry-run", "--json"))[
        "roles"] == 0
    assert sorted(p.name for p in (root / "runs").iterdir()) == runs_before  # a dry run writes nothing

    atom(fake, {1: 1})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v1", body=pdf_of(1, 1))
    later = r["last_attempt_at"] + 2 * DAY  # long before the 90-day recheck
    with lanes.using(now=later, transport=fake, sleep=lambda s: None):
        out = _cli(root, plan, "--lane", "arxiv", "--error-class", "dead")
    rec = json.loads(out.strip().splitlines()[-1])
    assert (rec["status"], rec["reopen.roles"], rec["reopen.rows"]) == ("ok", 1, 1), rec
    row = view(root)[d]
    r = full_role(row)
    assert (r["status"], r["attempts"], len(raws(row))) == ("ok", 0, 1)
    checks_ok(root)


def test_reopen_cli_refuses_a_bad_since(tmp_path: Path) -> None:
    out = _cli(tmp_path, tmp_path / "none.yaml", "--lane", "arxiv", "--since", "soon", "--dry-run", code=1)
    assert "since" in out
