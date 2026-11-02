"""T5.3: ``stack diff`` between two builds (renamed from ``test_diff_gc.py``: ``stack gc`` is gone,
D356, D365; ``stack status`` is tested in ``tests/test_cli.py`` and ``tests/test_store_remote.py``).

The diff scenario runs two real builds (the stand-in stages and plan of
``tests/test_scenario_m5.py``): build 1 leaves one ``screen`` row ``not_attempted``
(``max_rows``), build 2 decides it, so exactly that row changes in ``screen`` and in ``corpus``.
The ``--limit`` case uses the check fixture store (``tests/fixtures/checks``) with hand-written
build records (A.7 shape).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import zstandard
from typer.testing import CliRunner

from stack.build import run_build
from stack.cli import app
from stack.cli.diff import comparison, diff_builds
from stack.runs import (
    build_record_path,
)
from stack.stage_runner import WORKFLOWS_ROOT_ENV
from stack.store.parquet import read_family
from tests.fixtures.checks import FileSpec, clean_world, fid, write_world
from tests.synth.rows import (
    AT,
    dg,
    mint,
    rid,
    screen_row,
)
from tests.synth.world import build_world
from tests.test_scenario_m5 import PLAN_PATH, STAND_INS, plan_with

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
runner = CliRunner()


def cli(store: Path, *args: str, plan: Path = DEV_PLAN, env: dict[str, str] | None = None) -> str:
    res = runner.invoke(app, ["--store", str(store), "--plan", str(plan), *args], env=env)
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    print(f"$ stack {' '.join(args)}  -> {res.exit_code}\n{res.output}")
    assert res.exit_code == 0, res.output
    return res.output


# ---------------------------------------------------------------- diff
@pytest.mark.slow
def test_diff_of_two_builds_lists_exactly_the_changed_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    store = tmp_path / "store"
    build_world(store)
    b1, b2 = run_build(store, plan_with()), run_build(store, plan_with())
    assert (b1.status, b2.status) == ("ok", "ok")
    # ground truth straight from the two screen files: rows whose (status, result_digest) differ
    s1, s2 = ({(r["doc_id"], r["item_key"]): (r["status"], r["result_digest"])
               for r in read_family(store / b.files["screen"]["path"])[0].to_pylist()} for b in (b1, b2))
    truth = sorted(k for k in s1.keys() & s2.keys() if s1[k] != s2[k])
    assert len(truth) == 1 and s1[truth[0]][0] == "not_attempted" and s2[truth[0]][0] == "ok"
    left = truth[0][0]

    d = diff_builds(store, b1.data, b2.data, limit=0)
    screen = d["units"]["screen"]
    assert screen["status"] == "compared" and screen["file_a"] != screen["file_b"]
    assert [tuple(c["key"]) for c in screen["changed"]] == truth
    assert screen["changed"][0]["fields"]["status"] == ["not_attempted", "ok"]
    assert screen["counts"] == {"added": 0, "removed": 0, "changed": 1, "unchanged": len(s1) - 1}
    # the folded corpus shows the same document, through annotations.screen
    corpus = d["units"]["corpus"]
    assert [c["key"] for c in corpus["changed"]] == [[left]]
    assert corpus["changed"][0]["fields"]["annotations.screen"][0][0] == "not_attempted"
    # the comparison never reads a stamp: a row only re-stamped compares equal
    row = read_family(store / b2.files["corpus"]["path"])[0].to_pylist()[0]
    stamped = {**row, "run_id": rid(99), "updated_at": AT, "content": {**row["content"], "run_id": rid(99)},
               "identity": {**row["identity"], "updated_at": AT}}
    assert comparison("corpus", stamped) == comparison("corpus", row)
    # documents: build 2 compacted and re-wrote rows, but no compared value moved
    assert d["units"]["documents"]["counts"]["changed"] == 0
    assert d["units"]["documents"]["file_a"] != d["units"]["documents"]["file_b"]
    # a family only build 1 wrote (nothing left to triage in build 2) is only_a, all removed
    tri = d["units"]["triage_evidence"]
    assert tri["status"] == "only_a" and tri["counts"]["removed"] > 0 and tri["counts"]["added"] == 0
    # a build against itself: nothing changed anywhere
    same = diff_builds(store, b2.data, b2.data)
    assert all(u["counts"]["changed"] + u["counts"]["added"] + u["counts"]["removed"] == 0
               for u in same["units"].values())

    # the CLI: ids, `latest`, --family, text and JSON
    out = cli(store, "diff", b1.build_id, "latest", "--family", "screen", plan=PLAN_PATH)
    assert f"diff {b1.build_id} -> {b2.build_id}" in out
    assert "screen: +0 -0 ~1" in out and f"  ~ {left} | ''  status not_attempted -> ok; result_digest NULL -> " in out
    assert "corpus" not in out and "screen_evidence" not in out
    j = json.loads(cli(store, "diff", str(b1.path), b2.build_id, "--family", "corpus", "--json", plan=PLAN_PATH))
    assert list(j["units"]) == ["corpus"] and j["units"]["corpus"]["counts"]["changed"] == 1


# ---------------------------------------------------------------- hand-written builds
def B(k: int) -> dt.datetime:
    """A hand-written build's clock: build ``k`` starts ``10 k`` minutes after ``AT`` (in the past,
    after every fixture header's ``finished_at = AT``)."""
    return AT + dt.timedelta(minutes=10 * k)


def _build(store: Path, n: int, k: int, run: int, status: str, documents: str) -> str:
    """Build ``w<n>`` (A.7 shape), started at ``B(k)``, naming ``documents`` and run ``run``'s
    screen files."""
    at = B(k)
    rec = {"build_id": mint("w", n), "plan": "dev-v2", "plan_digest": dg("plan"), "gate_digest": dg("gate"),
           "status": status, "started_at": at.isoformat(), "finished_at": (at + dt.timedelta(minutes=5)).isoformat(),
           "inputs_pinned_at": at.isoformat(), "version_chain": [documents],
           "files": {"documents": {"file_id": documents},
                     "screen": {"file_id": fid(run, "screen"), "path": "data/x"},
                     "screen_evidence": {"file_id": fid(run, "screen_evidence")}},
           "runs": [rid(run)], "verify": {"status": "ok", "checks": []}, "require_complete": [], "counts": {}}
    path = build_record_path(store, rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(rec).encode()))
    return rec["build_id"]


# ---------------------------------------------------------------- status: areas, no ledger
# ---------------------------------------------------------------- diff --limit (T5.5 note (3))
def test_diff_limit_lists_n_keys_per_category_and_counts_the_rest(tmp_path: Path) -> None:
    """``--limit N`` lists at most N keys per unit and category and prints how many it left out;
    ``--limit 0`` lists every key; counts are complete either way. Two hand-written builds whose
    ``screen`` files hold 3 and 25 rows (the default limit is 20)."""
    w = clean_world()
    w.build = None
    for run, n in ((31, 3), (32, 25)):
        w.files.append(FileSpec(f"screen{run}", "screen", run, {"stage": "annotate.screen", "basis_kind": "text"},
                                [screen_row(i, run) for i in range(n)]))
    root = write_world(w, tmp_path / "store")
    a, b = _build(root, 31, 1, 31, "ok", fid(5)), _build(root, 32, 2, 32, "ok", fid(5))
    j = json.loads(cli(root, "diff", a, b, "--family", "screen", "--json", "--limit", "0"))["units"]["screen"]
    c = j["counts"]
    assert c["added"] == 22 and c["removed"] == 0 and c["changed"] + c["unchanged"] == 3
    assert len(j["added"]) == 22 and len(j["changed"]) == c["changed"]
    every = cli(root, "diff", a, b, "--family", "screen", "--limit", "0")
    assert every.count("\n  + ") == 22 and "more (--limit)" not in every
    default = cli(root, "diff", a, b, "--family", "screen")
    assert default.count("\n  + ") == 20
    assert f"  … {22 - 20} more (--limit)" in default
    two = cli(root, "diff", a, b, "--family", "screen", "--limit", "2")
    assert two.count("\n  + ") == 2 and two.count("\n  ~ ") == min(2, c["changed"])
    assert f"  … {22 + c['changed'] - 2 - min(2, c['changed'])} more (--limit)" in two
    assert f"screen: +22 -0 ~{c['changed']} ={c['unchanged']}" in two  # counts never truncated
