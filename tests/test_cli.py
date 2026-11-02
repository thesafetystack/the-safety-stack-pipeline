"""The operator verbs (T1.6) through typer's ``CliRunner`` (no subprocess) over the clean check
store of ``tests/fixtures/checks`` (a ``data/documents/main/`` series F1-F5 with a compaction,
collector series, a ``screen`` file, a ``corpus`` file, ``ok`` run records and a build record), with dead
runs (a ``running`` json with a stale ``-running.json`` marker) and A.7 build records planted by
hand.

``stack gc`` and ``stack chain unlock`` are gone (T-STORE A2: no locks, D356; C2 retires gc), and
so are their tests; so are ``stack latest`` and ``stack chain show`` with chain ids and prior
builds (D373, D375: ``stack head <dir>`` and ``stack ls <dir>``).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stack.checks import CHECK_TABLE
from stack.cli import app
from stack.runs import (
    RunRecord,
    read_run_record,
    read_run_records,
    write_build_record,
    write_final_record,
    write_running_record,
)
from stack.store.chain import resolve
from stack.store.index import DEAD_AFTER
from stack.store.layout import DATA, RUNS
from stack.store.parquet import read_header
from tests.fixtures.checks import build_store, fid
from tests.synth.rows import AT, dg, mint, rid

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
VERBS = {"run": "--in", "fold": "--filter", "verify": "--names", "head": "--json", "status": "--json",
         "ls": "--json", "show": "--record", "chain": "orphan", "build": "--no-upload", "store": "push", "release": "--in"}
DOCS = "data/documents/main/"
runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    build_store(root)
    return root


def cli(store: Path, *args: str, code: int | None = 0, env: dict[str, str] | None = None) -> str:
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), *args], env=env)
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    out = res.output
    print(f"$ stack {' '.join(args)}  -> {res.exit_code}\n{out}")
    if code is not None:
        assert res.exit_code == code, out
    return out


def plant_dead(store: Path, n: int, inflight: list[str]) -> RunRecord:
    """A ``running`` json whose marker is older than ``DEAD_AFTER``: dead to the store index."""
    rec = RunRecord(run_id=rid(n), stage="acquire", writer="acquire", plan="dev-v2", status="running",
                    started_at=AT, heartbeat_at=AT, code_digest=dg("code"), config_digest=dg("config"),
                    inflight=inflight)
    p = write_running_record(store, rec)
    marker = p.with_name(p.name.removesuffix(".json.zst") + "-running.json")
    marker.write_text('{"tick": "x"}')
    old = (dt.datetime.now(dt.UTC) - DEAD_AFTER - dt.timedelta(minutes=1)).timestamp()
    os.utime(marker, (old, old))
    return rec


def key(store: Path, n: int, family_dir: str = "documents") -> str:
    """The store key of run ``rid(n)``'s file in ``data/<family_dir>/main/``."""
    (p,) = (store / DATA / family_dir / "main").glob(f"*_{rid(n)}.*.parquet")
    return p.relative_to(store).as_posix()


def name(store: Path, n: int, family_dir: str = "documents") -> str:
    return key(store, n, family_dir).rsplit("/", 1)[1]


def corpus_files(store: Path, family_dir: str = "corpus") -> list[Path]:
    return sorted((store / DATA / family_dir).rglob("*.parquet"))


# ---------------------------------------------------------------- help
@pytest.mark.slow
def test_help_lists_every_verb_and_documents_flags(store: Path) -> None:
    top = cli(store, "--help")
    for verb in VERBS:
        assert verb in top
    for verb, flag in VERBS.items():
        assert flag in cli(store, verb, "--help"), verb
    run_help = cli(store, "run", "--help")
    for flag in ("--no-upload", "--out", "--series", "--part", "--full", "--restart", "--note"):
        assert flag in run_help, flag
    assert "--prior" not in cli(store, "build", "--help")
    assert "--out" in cli(store, "fold", "--help") and "--in" in cli(store, "verify", "--help")
    orphan = cli(store, "chain", "orphan", "--help")
    assert "PATH" in orphan and "--note" in orphan and "--force" in orphan


# ---------------------------------------------------------------- head
# test_latest removed with `stack latest` and prior-build selection (D373): stack head <dir>
def test_head_prints_the_directorys_last_file_and_its_version(store: Path) -> None:
    out = cli(store, "head", DOCS)
    assert out.splitlines() == [f"head\t{key(store, 5)}", f"version\tfull\t{name(store, 4)}",
                                f"version\tpart\t{name(store, 5)}"]
    j = json.loads(cli(store, "head", DOCS, "--json"))
    assert (j["path"], [v["name"] for v in j["version"]]) == (key(store, 5), [name(store, 4), name(store, 5)])
    assert "no published file" in cli(store, "head", "data/documents/other/", code=1)


# ---------------------------------------------------------------- status
def _marker(p: Path, age: dt.timedelta) -> None:
    m = p.with_name(p.name.removesuffix(".json.zst") + "-running.json")
    m.write_text('{"tick": "x"}')
    at = (dt.datetime.now(dt.UTC) - age).timestamp()
    os.utime(m, (at, at))


def test_status_jobs_liveness_spend_repeated_inflight(tmp_path: Path, store: Path) -> None:
    """D365: one line per job (builds and standalone runs, newest first) from the index alone:
    state with liveness and the last tick's age, spend from the records; a build lists its
    unfinished runs; X11's repeated inflight keys; nothing about ledgers, locks or sizes."""
    clean = cli(store, "status")
    assert "jobs: 9 (0 live, 0 dead, 0 unknown, 9 finished)" in clean, clean
    assert clean.splitlines()[2].split()[1:4] == ["build", mint("b", 1), "dev-v2"]  # newest first
    assert "lock" not in clean and "ledger" not in clean and "s3" not in clean  # a local-only store
    plant_dead(store, 50, ["doc:k1", "doc:k2"])
    plant_dead(store, 51, ["doc:k1"])
    spend = {"usd_micro": {"openrouter/m/a": 1_250_000, "x": 250_000}, "reserved_open_usd_micro": 2_000_000,
             "batch_ids": ["b-1"], "abandoned_batch_ids": []}
    live = RunRecord(run_id=rid(52), stage="annotate.screen", writer="annotate.screen", plan="dev-v2",
                     status="running", started_at=AT + dt.timedelta(days=1), heartbeat_at=AT,
                     code_digest=dg("code"), config_digest=dg("config"), spend=spend)
    _marker(write_running_record(store, live), dt.timedelta(seconds=40))
    unknown = RunRecord(run_id=rid(53), stage="gather", writer="gather", plan="dev-v2", status="running",
                        started_at=AT + dt.timedelta(days=2), heartbeat_at=AT, code_digest=dg("code"),
                        config_digest=dg("config"))
    write_running_record(store, unknown)
    b = {"build_id": mint("w", 9), "plan": "dev-v2", "status": "running",
         "started_at": (AT + dt.timedelta(days=3)).isoformat(), "upload": False}
    bp = write_build_record(store, b)
    (bp.parent / "build-running.json").write_text("{}")
    in_build = RunRecord(run_id=rid(54), stage="acquire", writer="acquire", plan="dev-v2", status="running",
                         started_at=AT + dt.timedelta(days=3), heartbeat_at=AT, code_digest=dg("code"),
                         config_digest=dg("config"), build_id=mint("w", 9), spend=spend)
    _marker(write_running_record(store, in_build, build=bp.parent.name), dt.timedelta(seconds=5))
    done = RunRecord(run_id=rid(55), stage="gather", writer="gather", plan="dev-v2", status="ok",
                     started_at=AT + dt.timedelta(days=3), heartbeat_at=AT, finished_at=AT + dt.timedelta(days=3),
                     code_digest=dg("code"), config_digest=dg("config"), build_id=mint("w", 9), spend=spend)
    write_final_record(store, done, build=bp.parent.name)  # finished: counted in the spend, not listed

    out = cli(store, "status")
    lines = out.splitlines()
    assert "jobs: 14 (2 live, 2 dead, 1 unknown, 9 finished)" in lines[1], out  # rid(55) is the build's
    assert lines[2].split()[1:5] == ["build", mint("w", 9), "dev-v2", "running,"] and "live" in lines[2]
    assert lines[3].split()[1:4] == ["run", rid(54), "acquire"] and "$1.50 +$2.00 open" in lines[3]  # the build's run
    assert "$3.00 +$4.00 open" in lines[2]  # the build's spend sums its runs
    assert lines[4].split()[2:6] == [rid(53), "gather", "running,", "unknown"]
    assert lines[5].split()[2:6] == [rid(52), "annotate.screen", "running,", "live"]
    assert re.fullmatch(r"(4\d|5\d)s", lines[5].split()[6]), lines[5]  # the marker's age: 40 s plus the test's time
    assert "$1.50 +$2.00 open" in lines[5]
    dead = [ln for ln in lines if "DEAD" in ln]
    assert [ln.split()[2] for ln in dead] == [rid(51), rid(50)] and all(" 6m" in ln for ln in dead), dead
    assert f"warning: repeated_inflight (X11): key 'doc:k1' inflight in dead runs {rid(50)}, {rid(51)}" in out
    assert "doc:k2" not in out and "free disk" in out
    assert len(cli(store, "status", "--limit", "3").splitlines()) == 2 + 3 + 1 + 1 + 1  # +1 build run, warning, disk
    s = json.loads(cli(store, "status", "--json"))
    assert s["jobs_total"] == 14 and not s["mirror"] and s["by_liveness"]["dead"] == 2
    j52 = next(j for j in s["jobs"] if j["id"] == rid(52))
    assert (j52["liveness"], j52["spend_usd_micro"], j52["open_usd_micro"], j52["s3"]) == ("live", 1_500_000,
                                                                                         2_000_000, None)
    assert s["repeated_inflight"] == [{"key": "doc:k1", "runs": [rid(50), rid(51)]}]
    assert not {"heartbeat_timeout", "locks", "size_bytes", "lost", "unrecorded"} & set(s)


def test_removed_verbs_are_gone(store: Path) -> None:
    """D356, D365: no gc, no chain unlock, no lock or ledger options; no ``stack latest`` (prior
    builds, D373: ``stack head <dir>``), no ``chain show`` (chain ids: ``stack ls <dir>``).
    The one table of removed verbs and options (S25-97)."""
    for args in (["gc"], ["gc", "--apply"], ["chain", "unlock", "documents"], ["status", "--ledger"],
                 ["gc", "--finalise-dead"], ["latest"], ["chain", "show", "documents"]):
        res = runner.invoke(app, ["--store", str(store), *args])
        assert res.exit_code == 2 and ("No such command" in res.output or "No such option" in res.output), (
            args, res.output)
    top = cli(store, "--help")
    assert " gc " not in top and "unlock" not in cli(store, "chain", "--help")


# ---------------------------------------------------------------- ls
def test_ls_shows_series_dirs_heads_and_files(store: Path) -> None:
    out = cli(store, "ls")
    assert f"{DOCS}: 5 file(s), 2 in version; head {name(store, 5)}" in out
    assert "data/sources.arxiv/main/: 1 file(s)" in out and "data/corpus/main/: 1 file(s)" in out
    one = cli(store, "ls", DOCS)
    assert f"* {name(store, 4)}  full  base {name(store, 3)}" in one and f"  {name(store, 1)}  full  base -" in one
    only = json.loads(cli(store, "ls", DOCS, "--json"))
    assert only["dir"] == DOCS.rstrip("/")
    assert [f["name"] for f in only["files"]] == [name(store, i) for i in range(1, 6)]
    assert [f["in_version"] for f in only["files"]] == [False, False, False, True, True]


# ---------------------------------------------------------------- verify helpers
def checks(store: Path, *args: str, code: int = 0) -> dict[str, tuple[str, str | None]]:
    """``stack verify --json [args]`` as ``name -> (status, error_class)``."""
    out = json.loads(cli(store, "verify", "--json", *args, code=code))
    return {c["name"]: (c["status"], c["error_class"]) for c in out["checks"]}


def failed(results: dict[str, tuple[str, str | None]]) -> set[str]:
    return {n for n, (status, _) in results.items() if status == "failed"}


def doc_files(store: Path) -> list[str]:
    return sorted(read_header(p)["file_id"] for p in (store / DOCS).glob("*.parquet"))


# ---------------------------------------------------------------- chain
# chain show is gone with chain ids (D375): test_ls_shows_series_dirs_heads_and_files lists a directory
def test_chain_orphan_by_path_with_a_note(store: Path) -> None:
    p5 = key(store, 5)
    out = cli(store, "chain", "orphan", p5, "--note", "a lost tail")
    (excl,) = (store / RUNS).glob("*.chain.orphan.json.zst")  # D345: an excludes record, nothing moved
    assert f"orphaned {p5}: excluded by {excl.relative_to(store).as_posix()}" in out
    rec = read_run_record(excl)
    assert rec.excludes == [fid(5)] and rec.note == "a lost tail" and fid(5) in doc_files(store)
    assert resolve(store, DOCS).head == fid(4)
    assert "excluded already" in cli(store, "chain", "orphan", p5, code=1)


# ---------------------------------------------------------------- fold
@pytest.mark.slow
def test_fold_verb_and_fold_stage_write_into_the_corpus_series(store: Path) -> None:
    before = corpus_files(store)
    last = name(store, 8, "corpus")
    out = json.loads(cli(store, "fold", "corpus", "--input", f"documents={DOCS}", "--out", "data/corpus/main/"))
    assert out["status"] == "ok"
    new = [p for p in corpus_files(store) if p not in before]
    assert len(new) == 1 and new[0].parent == store / "data/corpus/main"
    h = read_header(new[0])
    assert (h["chain_id"], h["series"], h["kind"], h["base"], h["after"], h["writer"], h["fold_config"],
            h["stage"]) == ("corpus", "main", "full", "", last, "fold", "corpus", "fold.corpus")
    assert json.loads(h["inputs"]) == {"documents": fid(5)}
    rec = next(r for r in read_run_records(store) if r.run_id == out["run_id"])
    assert rec.fold is not None and rec.fold["config"] == "corpus" and rec.writer == "fold"
    assert rec.outputs["corpus"]["file_id"] == h["file_id"]

    out2 = json.loads(cli(store, "run", "fold.corpus", "--in", DOCS, "--out", "data/corpus/main/").splitlines()[-1])
    assert out2["status"] == "ok"
    new2 = [p for p in corpus_files(store) if p not in before and p not in new]
    assert len(new2) == 1
    h2 = read_header(new2[0])
    assert (h2["chain_id"], h2["writer"], h2["fold_config"], h2["filter"], h2["after"]) == (
        "corpus", "fold", "corpus", h["filter"], new[0].name)
    assert h2["row_count"] == h["row_count"] and int(h["row_count"]) > 0
    rec2 = next(r for r in read_run_records(store) if r.run_id == out2["run_id"])
    assert rec2.fold == rec.fold and rec2.config_digest == rec.config_digest

    # a filter on an exact (older) file, and bad build/release calls refused before any run exists
    out3 = json.loads(cli(store, "fold", "corpus", "--input", f"documents={key(store, 4)}", "--out",
                          "data/corpus/main/", "--filter", "everything"))
    rec3 = next(r for r in read_run_records(store) if r.run_id == out3["run_id"])
    assert rec3.fold is not None and rec3.fold["filter"] == "true" and rec3.inputs == {"documents": fid(4)}
    n_records = len(read_run_records(store))
    build_out = ("--out", "data/corpus.build/main/")
    assert "corpus=x: no ok run record names that file id" in cli(store, "fold", "build", "--input", "corpus=x",
                                                                  *build_out, code=1)
    assert "takes --input corpus" in cli(store, "fold", "build", "--input", f"documents={DOCS}", *build_out, code=1)
    assert "not a stage" in cli(store, "run", "fold.release", "--in", "data/corpus/main/", code=1)
    assert "takes --input documents" in cli(store, "fold", "corpus", "--input", "corpus=x", "--out",
                                            "data/corpus/main/", code=1)
    assert len(read_run_records(store)) == n_records
    # (v4.1's "a pinned head that moved is a failed run" is gone: an exact file pins its own version,
    # and a moved directory is the publish rule's head_moved, D372)
    # fold.compact through the runner: a documents full file on the directory's head (V12 checks it)
    out5 = json.loads(cli(store, "run", "fold.compact", "--in", DOCS).splitlines()[-1])
    h5 = read_header(store / out5["outputs"]["documents"])
    assert resolve(store, DOCS).version_chain == (h5["file_id"],)
    assert (h5["kind"], h5["base"], h5["writer"], h5["fold_config"], h5["row_count"]) == (
        "full", name(store, 5), "fold", "compact", "7")
    assert "failed" not in cli(store, "verify").splitlines()[-2]  # fold runs are runs to X1


# ---------------------------------------------------------------- verify
@pytest.mark.slow
def test_verify_table_summary_and_exit_code(store: Path) -> None:
    assert cli(store, "verify").strip().splitlines()[-1].startswith("mode: files+build_set (")  # the default
    out = cli(store, "verify", "--full")
    lines = out.strip().splitlines()
    assert lines[0].split() == ["code", "name", "level", "status", "error_class", "n", "items"]
    n = len(CHECK_TABLE)
    assert len(lines) == 1 + n + 2  # the table, the summary, the mode line
    assert lines[-2] == f"{n} checks: {n} ok"  # every check is implemented (X16 was the last, T4.2)
    assert lines[-1].startswith("mode: full (")
    assert lines[1].split()[:4] == ["S1", "schema_equal", "hard", "ok"]
    j = json.loads(cli(store, "verify", "--full", "--json", "--names", "finished_files,schema_equal"))
    assert [c["code"] for c in j["checks"]] == ["S1", "X1"]
    assert "unknown check 'nope'" in cli(store, "verify", "--names", "nope", code=1)
    # break the store: a data/ file of an ok record is gone (lost tail, missed part file)
    (store / key(store, 2)).unlink()
    out = cli(store, "verify", "--structural", code=1)  # X1 is a structural check
    x1 = next(line for line in out.splitlines() if line.startswith("X1 "))
    assert "finished_files" in x1 and "failed" in x1 and fid(2) in x1
    assert "failed" in out.splitlines()[-2]
    assert cli(store, "verify", "--full", "--in", "data/screen/main/", code=1)  # X1 runs over all of data/ whatever --in says


@pytest.mark.slow
def test_verify_in_checks_only_the_named_series_directories(tmp_path: Path) -> None:
    """D379: ``--in`` restricts the file and chain checks to its series directories; a family
    directory without a series is refused (no implicit selection)."""
    root = tmp_path / "store"
    build_store(root, "id_formats")  # the fixture's bad id is in data/screen/main/'s file
    assert "id_formats" in failed(checks(root, "--full", code=1))
    assert failed(checks(root, "--full", "--in", "data/screen/main/", code=1)) == {"id_formats"}
    assert failed(checks(root, "--full", "--in", DOCS, "--in", "data/corpus/main/")) == set()
    assert "without a series" in cli(root, "verify", "--in", "data/documents/", code=1)


# ---------------------------------------------------------------- release
@pytest.mark.slow
def test_release_in_releases_the_directorys_last_file(store: Path) -> None:
    """``stack release --in data/corpus.build/<series>/`` releases the directory's last file (an older
    file only when named exactly); stderr says which."""
    corpus = key(store, 8, "corpus")
    built = []
    for comment in ("first", "second"):
        out = json.loads(cli(store, "fold", "build", "--input", f"corpus={corpus}", "--out", "data/corpus.build/main/",
                             "--comment", comment))
        built.append(out["outputs"]["corpus"])
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), "release", "--in",
                              "data/corpus.build/main/", "--tag", "t-last", "--allow-unbuilt"])  # no build (D494)
    assert res.exit_code == 0, res.output
    assert f"release of {built[1]}: its directory's last file" in res.stderr
    assert "no build names it (--allow-unbuilt)" in res.stderr  # D494: the naming build's status
    rel = next(r for r in read_run_records(store) if r.run_id == json.loads(res.stdout.splitlines()[-1])["run_id"])
    assert rel.inputs == {"corpus": read_header(store / built[1])["file_id"]}
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), "release", "--in", built[0],
                              "--tag", "t-older", "--allow-unbuilt"])
    assert res.exit_code == 0 and f"release of {built[0]}: NOT its directory's last file" in res.stderr, res.output
    assert "has no published file" in cli(store, "release", "--in", "data/corpus.build/other/", code=1)


# ---------------------------------------------------------------- relative --store (T2.x-f2)
def test_relative_store_path_works_for_every_verb(tmp_path: Path, store: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative ``--store`` (global or per verb) is resolved once at CLI entry: a run whose input
    head is a recorded file used to read ``<store>/<store>/data/…`` (refuse_unrecorded_head →
    has_ok_record joined a cwd-relative head path onto the store root)."""
    monkeypatch.chdir(tmp_path)
    rel = Path(store.name)
    assert not rel.is_absolute() and (tmp_path / rel / DATA).is_dir()
    out = json.loads(cli(rel, "run", "fold.corpus", "--in", DOCS, "--out", "data/corpus/main/").splitlines()[-1])  # stderr's ledger line is mixed in
    assert out["status"] == "ok"
    rec = next(r for r in read_run_records(store) if r.run_id == out["run_id"])
    assert (store / rec.outputs["corpus"]["path"]).is_file()
    assert "failed" not in cli(rel, "verify").splitlines()[-1]
    assert f"head\t{rec.outputs['corpus']['path']}" in cli(rel, "head", "data/corpus/main/")
    # the same verbs with the per-verb option, and the store path printed absolute
    out2 = json.loads(cli(tmp_path / "unused", "run", "fold.corpus", "--in", DOCS, "--out", "data/corpus/main/",
                          "--store", str(rel)).splitlines()[-1])
    assert out2["status"] == "ok"
    res = runner.invoke(app, ["--store", str(rel), "head", "data/nope/main/"], env={"STACK_PLAN": None})
    assert res.exit_code == 1 and str(store) in res.output


# ---------------------------------------------------------------- R6-5 defaults (round 5)
NO_ENV = {"STACK_PLAN": None, "STACK_STORE": None}


def bare(*args: str) -> object:
    """``stack <args>`` with no global option and neither STACK_PLAN nor STACK_STORE."""
    return runner.invoke(app, list(args), env=NO_ENV)


def test_a_missing_store_is_never_created_silently(tmp_path: Path) -> None:
    """R6-5 (a): a non-dry build, run, fold, release and migrate refuse a store root that does not
    exist; ``--create`` makes it (a directory; no ``store.json`` since D321); a dry run and the
    read-only verbs never create one."""
    gone = tmp_path / "no-store"
    plan = str(DEV_PLAN)
    for args in (["build", plan, "--store", str(gone)],
                 ["run", "echo_test", "--store", str(gone), "--plan", plan],
                 ["fold", "corpus", "--input", f"documents={DOCS}", "--out", "data/corpus/main/", "--store", str(gone),
                  "--plan", plan],
                 ["release", "--in", "data/corpus.build/main/", "--store", str(gone), "--plan", plan],
                 ["migrate", "upcast", "--family", "documents", "--store", str(gone)],
                 ["migrate", "rekey", "--store", str(gone)],
                 ["build", plan, "--dry-run", "--store", str(gone)],
                 ["head", DOCS, "--store", str(gone)], ["verify", "--store", str(gone)],
                 ["status", "--store", str(gone)]):
        res = bare(*args)
        writes = "--dry-run" not in args and args[0] in ("build", "run", "fold", "release", "migrate")
        assert res.exit_code != 0 or not writes, (args, res.output)
        assert not gone.exists(), args
    assert "no store at" in bare("run", "echo_test", "--store", str(gone), "--plan", plan).output
    res = bare("verify", "--store", str(gone))  # S25-51: was exit 0, "41 ok, 6 skipped"
    assert res.exit_code == 1 and "no store at" in res.output and not gone.exists(), res.output
    assert "--create" in bare("build", plan, "--store", str(gone)).output
    res = bare("run", "echo_test", "--store", str(gone), "--plan", plan, "--create")
    assert gone.is_dir() and not (gone / "store.json").exists(), res.output
    assert "created store" in res.output


def test_build_refuses_a_global_plan_that_is_not_its_plan(store: Path) -> None:
    """S25-51: ``--plan X build Y`` used to build Y silently; a command-line ``--plan`` naming another
    plan file is refused before anything runs. The same plan (by name or path) is no conflict, and
    ``STACK_PLAN`` from the environment is a default that the positional PLAN overrides."""
    other = REPO / "configs" / "plans" / "std-v3.yaml"
    res = bare("--store", str(store), "--plan", str(other), "build", str(DEV_PLAN), "--dry-run")
    assert res.exit_code == 1 and "name different plans" in res.output, res.output
    for args, env in ((["--plan", str(DEV_PLAN), "build", "dev-v2"], None),
                      (["build", str(DEV_PLAN)], {"STACK_PLAN": str(other)})):
        res = runner.invoke(app, ["--store", str(store), *args, "--dry-run", "--json"], env=env)
        assert "name different plans" not in res.output and json.loads(res.stdout)["plan"] == "dev-v2", res.output


def test_the_default_store_is_the_repositorys_whatever_the_cwd(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """R6-5 (b): without --store/STACK_STORE the store is ``v4/store-dev`` under the repository,
    not under the shell's cwd. Hermetic (S25-97): the store the CLI resolves is recorded and
    swapped for the one under a temporary repo-shaped root, so whatever sits in the working
    tree's real ``v4/store-dev`` (an untracked dev store, maybe in an old layout) is never read."""
    from stack.cli import common
    from stack.cli.common import DEFAULT_STORE

    assert DEFAULT_STORE == REPO / "v4" / "store-dev" and DEFAULT_STORE.is_absolute()
    fake_store = tmp_path / "repo" / "v4" / "store-dev"
    seen: list[str] = []
    real_resolve = common.resolve_store

    def resolve(arg: str, **kw: Any) -> Path:
        seen.append(arg)
        return real_resolve(str(fake_store) if arg == str(DEFAULT_STORE) else arg, **kw)

    monkeypatch.setattr(common, "resolve_store", resolve)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    res = bare("head", "data/documents/no-such-series/")
    assert seen == [str(REPO / "v4" / "store-dev")], seen  # the repository's store, not the cwd's
    assert res.exit_code == 1 and f"no published file in {fake_store}" in res.output, res.output
    assert list(cwd.iterdir()) == [] and not fake_store.exists()


def test_no_default_plan(store: Path) -> None:
    """R6-5 (c): run, release and fold refuse without --plan/STACK_PLAN; status and head need no plan
    (liveness is the store index's markers, no heartbeat timeout)."""
    for args in (["run", "echo_test"], ["release", "--in", "data/corpus.build/main/"],
                 ["fold", "corpus", "--input", f"documents={DOCS}", "--out", "data/corpus/main/"]):
        res = bare("--store", str(store), *args)
        assert res.exit_code == 1 and "no plan: pass --plan" in res.output, (args, res.output)
    res = bare("--store", str(store), "status", "--json")
    assert res.exit_code == 0 and json.loads(res.stdout)["by_liveness"]["finished"] == 9, res.output
    assert bare("--store", str(store), "head", DOCS).exit_code == 0
    # with STACK_PLAN the plan is read from the environment
    res = runner.invoke(app, ["--store", str(store), "fold", "corpus", "--input", f"documents={DOCS}", "--out",
                              "data/corpus/main/"], env={"STACK_PLAN": str(DEV_PLAN)})
    assert res.exit_code == 0 and json.loads(res.stdout)["status"] == "ok", res.output


@pytest.mark.slow
def test_env_limits_refuse_a_build_and_the_dry_run_says_so(store: Path) -> None:
    """D553: ``stack build`` with a collector cap variable set exits 1 before any record and names it;
    ``--dry-run`` prints the verdict on its ``env_limits`` line, ``allowed`` under ``--allow-env-limits``;
    ``stack run`` of a collector is refused the same way."""
    env = {**NO_ENV, "STACK_COLLECT_LIMIT": "7"}
    builds = sorted(store.glob("builds/*"))
    res = runner.invoke(app, ["build", str(DEV_PLAN), "--store", str(store)], env=env)
    assert res.exit_code == 1 and "build refused [env_limits]" in res.output, res.output
    assert "STACK_COLLECT_LIMIT=7" in res.output and sorted(store.glob("builds/*")) == builds
    res = runner.invoke(app, ["build", str(DEV_PLAN), "--dry-run", "--store", str(store)], env=env)
    assert res.exit_code == 0 and "env_limits\terror: refused: " in res.output, res.output
    res = runner.invoke(app, ["build", str(DEV_PLAN), "--dry-run", "--allow-env-limits", "--store", str(store)],
                        env=env)
    assert "env_limits\tallowed by --allow-env-limits: " in res.output, res.output
    res = runner.invoke(app, ["build", str(DEV_PLAN), "--dry-run", "--store", str(store)], env=NO_ENV)
    assert "env_limits\t-" in res.output, res.output
    res = runner.invoke(app, ["run", "collect.arxiv_sweep", "--store", str(store), "--plan", str(DEV_PLAN),
                              "--in", "data/sources.arxiv_sweep/main/", "--in", "data/coverage.arxiv_sweep/main/"],
                        env=env)
    assert res.exit_code == 1 and "--allow-env-limits" in res.output, res.output
