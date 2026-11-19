"""tools/scheduled_run.py (the scheduled cycle, docs/DEPLOY-railway.md): the build verdicts, the cycle's --max-usd
arithmetic, the ops lock on a fake S3, the read-only guard of the dry run, the preflight's forbidden variables,
the bucket scan and the settings. The cycle itself runs only against a real store (its dry run: the DEPLOY doc)."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from stack.store.s3config import ReadOnlyViolation
from tests.fakes.s3 import FAKE_ENV, FAKE_ENV_OLD, FakeS3

pytestmark = pytest.mark.tools

_spec = importlib.util.spec_from_file_location("scheduled_run", Path(__file__).parents[1] / "tools" / "scheduled_run.py")
assert _spec and _spec.loader
sr = importlib.util.module_from_spec(_spec)
sys.modules["scheduled_run"] = sr
_spec.loader.exec_module(sr)

URI, OPS = "s3://stack-data/stack-store", "s3://stack-data/stack-store-ops"


@pytest.fixture(autouse=True)
def no_memlog(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cycle tests run without the memory log process (D783); its own tests turn it on."""
    monkeypatch.setenv("STACK_SCHED_MEMLOG_S", "0")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    monkeypatch.setenv("STACK_STORE", URI)


def rec(status: str, *inc: tuple[str, list[str]], **more: Any) -> dict[str, Any]:
    return {"status": status, "incomplete": [{"stage": s, "run_id": "r", "reasons": r} for s, r in inc], **more}


@pytest.mark.parametrize(("r", "want"), [
    (rec("ok"), "ok"),
    (rec("failed", error_class="s3_transient"), "retry"),
    (rec("failed", error_class="tick_failed"), "retry"),
    (rec("failed", error_class="code_bug"), "failed"),
    (rec("cancelled", error_class="operator"), "failed"),
    (rec("incomplete", ("acquire", ["time", "pending"])), "tail"),      # acquire's retry tail: releasable
    (rec("incomplete", ("acquire", ["halted"])), "tail"),
    (rec("incomplete", ("acquire", ["halted"]), ("collect.cards", ["limit"])), "tail"),
    (rec("incomplete", ("acquire", ["halted"]), ("triage", ["budget"])), "again"),
    (rec("incomplete", ("acquire", ["limit"])), "again"),
    (rec("incomplete", ("acquire", ["halted", "unspecified"])), "again"),
    (rec("incomplete", ("annotate.screen", ["tier2_allowance"]), ("collect.cards", ["limit"])), "again"),
    (rec("incomplete", ("annotate.technique_staged", ["budget"])), "again"),
    (rec("incomplete", ("collect.forums", ["halted"])), "collectors"),
    (rec("incomplete", ("triage", ["unspecified"])), "again"),           # WARN; the next build retries it
    (rec("incomplete", ("acquire", ["time"]), ("gather", ["limit"])), "tail"),
    (rec("incomplete", ("acquire", [])), "again"),
    (rec("incomplete", ("gather", ["unspecified"])), "collectors"),
    (rec("incomplete", ("acquire", ["time"]), stopped={"by": "signal"}), "stopped"),
    (rec("cancelled", error_class="operator", stopped={"by": "cycle"}), "stopped"),  # D781: drained, then cancelled
])
def test_classify_build(r: dict[str, Any], want: str) -> None:
    assert sr.classify_build(r)[0] == want


def test_unexpected_reasons_are_marked_for_a_warning() -> None:
    _v, notes = sr.classify_build(rec("incomplete", ("triage", ["unspecified"]), ("acquire", ["time"])))
    assert notes == ["unexpected incomplete: triage (unspecified)", "incomplete: acquire (time)"]


def test_max_usd_args_lower_only_what_the_cycle_cannot_afford() -> None:
    caps = {"annotate.screen": 40.0, "annotate.technique_staged": 350.0}
    assert sr.max_usd_args(caps, 500.0) == []
    assert sr.max_usd_args(caps, 100.0) == ["--max-usd", "annotate.technique_staged=100.00"]
    assert sr.max_usd_args(caps, 12.5) == ["--max-usd", "annotate.screen=12.50",
                                           "--max-usd", "annotate.technique_staged=12.50"]
    assert sr.max_usd_args(caps, -3.0) == ["--max-usd", "annotate.screen=0.01",
                                           "--max-usd", "annotate.technique_staged=0.01"]


def test_lock_one_holder_heartbeat_and_release(env: None) -> None:
    fake = FakeS3()
    ops = sr.Ops(OPS, read_only=False, client=fake)
    a, b = sr.Lock(ops, "cycle-a"), sr.Lock(ops, "cycle-b")
    assert a.acquire({"code": "x"}) is None
    held = b.acquire({"code": "y"})
    assert held is not None and held["holder"] == "cycle-a"
    assert a.heartbeat() and not b.heartbeat()
    b.release()  # not ours: left in place
    assert ops.get_json(sr.Lock.KEY)["holder"] == "cycle-a"
    a.release()
    assert ops.get_json(sr.Lock.KEY) is None
    assert b.acquire({}) is None
    assert {k for k in fake.keys()} == {"stack-store-ops/lock.json"}


def test_dry_run_client_refuses_writes_before_sending(env: None) -> None:
    ops = sr.Ops(OPS, read_only=True)  # a real boto3 client on a fake endpoint: the guard fires before any request
    with pytest.raises(ReadOnlyViolation):
        ops.put_json("lock.json", {"holder": "x"})
    with pytest.raises(ReadOnlyViolation):
        ops.b.delete("lock.json")


def test_preflight_refuses_cap_and_test_hook_variables(env: None, monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    for k in ("OPENROUTER_API_KEY", "STACK_MAILTO"):
        monkeypatch.setenv(k, "set")
    monkeypatch.setenv("STACK_MIRROR", str(tmp_path))
    monkeypatch.setenv("STACK_SCHED_MIN_FREE_GB", "0")
    monkeypatch.setenv("STACK_SCHED_RELEASE", "off")
    s = sr.Settings.from_env()
    base, _ = sr.preflight(s, "d" * 64)
    monkeypatch.setenv("STACK_COLLECT_LIMIT", "10")
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", "tests/synth/stages")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("STACK_SCHED_EXPECT_PLAN_DIGEST", "e" * 64)
    refuse, _ = sr.preflight(sr.Settings.from_env(), "d" * 64)
    new = [r for r in refuse if r not in base]
    assert any("STACK_COLLECT_LIMIT" in r and "STACK_WORKFLOWS_ROOT" in r for r in new)
    assert any("OPENROUTER_API_KEY" in r for r in new)
    assert any("plan digest" in r for r in new)
    assert not any("STACK_POOL_MAX_THREADS" in r for r in new)
    monkeypatch.setenv("STACK_POOL_MAX_THREADS", "600")  # D786: the ceiling is allowed, and checked
    assert not any("STACK_POOL_MAX_THREADS" in r for r in sr.preflight(sr.Settings.from_env(), "d" * 64)[0])
    monkeypatch.setenv("STACK_POOL_MAX_THREADS", "six hundred")
    assert any("STACK_POOL_MAX_THREADS must be a positive integer" in r
               for r in sr.preflight(sr.Settings.from_env(), "d" * 64)[0])


def test_preflight_accepts_old_s3_names_and_names_the_new_ones(env: None, monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    """D726: each S3 setting is required under its new name or its older one, in the environment or the
    STACK_ENV_FILE file; a missing one is named by its new name with the old one beside it."""
    for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "STACK_MAILTO"):
        monkeypatch.setenv(k, "set")
    monkeypatch.setenv("STACK_MIRROR", str(tmp_path))
    monkeypatch.setenv("STACK_SCHED_MIN_FREE_GB", "0")

    def missing() -> list[str]:
        return [r for r in sr.preflight(sr.Settings.from_env(), "d" * 64)[0] if r.startswith("missing settings")]

    assert missing() == []
    for k in FAKE_ENV:
        monkeypatch.delenv(k)
    for k, v in FAKE_ENV_OLD.items():
        monkeypatch.setenv(k, v)
    assert missing() == []
    for k in FAKE_ENV_OLD:
        monkeypatch.delenv(k)
    assert missing() == [("missing settings: STACK_S3_ACCESS_KEY_ID (or AWS_ACCESS_KEY_ID), STACK_S3_SECRET_ACCESS_KEY "
                          "(or AWS_SECRET_ACCESS_KEY), STACK_S3_ENDPOINT_URL (or STACK_S3_ENDPOINT)")]
    f = tmp_path / "keys.env"
    f.write_text("".join(f"{k}={v}\n" for k, v in FAKE_ENV_OLD.items()))
    monkeypatch.setenv("STACK_ENV_FILE", str(f))
    assert missing() == []
    assert sr.key_present("STACK_S3_ENDPOINT_URL") and not sr.key_present("STACK_S3_NOT_A_SETTING")


def test_settings_keep_the_ops_prefix_outside_the_store(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sr.Settings.from_env().ops_uri == OPS
    monkeypatch.setenv("STACK_SCHED_OPS_URI", URI + "/ops")
    with pytest.raises(SystemExit, match="outside"):
        sr.Settings.from_env()
    monkeypatch.setenv("STACK_SCHED_OPS_URI", OPS)
    monkeypatch.setenv("STACK_STORE", "/a/local/store")
    with pytest.raises(SystemExit, match="s3://"):
        sr.Settings.from_env()


def test_scan_for_finds_a_needle_across_chunks(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * ((1 << 20) - 3) + b"needle" + b"y")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"clean")
    assert sr.scan_for(tmp_path, b"needle") == ["a"]


def test_json_doc_skips_lines_that_only_look_like_json() -> None:
    r = sr.Result(0, 'note {not json}\n{"broken": \n{"build_id": "b1",\n "status": "ok"}', [], 0.0)
    assert r.json_doc() == {"build_id": "b1", "status": "ok"}
    assert sr.Result(0, '{"run_id": "r1", "status": "ok"}\nother', [], 0.0).json_line('{"run_id"') == \
        {"run_id": "r1", "status": "ok"}


REAL_CHECK_STAGED = sr.check_staged


class FakeHf:
    """huggingface_hub.HfApi stand-in: repo privacy, trees, single commits with adds and deletes, tags. Changing a
    repo's visibility is never allowed (the pipeline never changes a repo's visibility)."""

    private: dict[str, bool] = {}
    uploads: list[str] = []
    publish_on_upload: set[str] = set()
    trees: dict[str, set[str]] = {}
    tags: dict[str, dict[str, str]] = {}
    commits: list[dict[str, Any]] = []

    def __init__(self, token: str) -> None:
        assert token == "hf-test"

    def repo_info(self, repo: str, repo_type: str) -> Any:
        return type("Info", (), {"private": self.private[repo], "sha": f"head-{len(self.commits)}"})()

    def list_repo_files(self, repo: str, repo_type: str, revision: str | None = None) -> list[str]:
        return sorted(self.trees.setdefault(repo, {".gitattributes", "old.parquet", "release.json"}))

    def create_commit(self, repo: str, operations: list[Any], *, commit_message: str, repo_type: str,
                      parent_commit: str, commit_description: str = "") -> Any:
        assert parent_commit == f"head-{len(self.commits)}"
        tree = self.trees.setdefault(repo, {".gitattributes", "old.parquet", "release.json"})
        for op in operations:
            if type(op).__name__ == "CommitOperationDelete":
                tree.discard(op.path_in_repo)
            else:
                tree.add(op.path_in_repo)
        self.uploads.append(repo)
        self.commits.append({"repo": repo, "n": len(operations)})
        if repo in self.publish_on_upload:
            self.private[repo] = False
        return type("Commit", (), {"oid": f"c{len(self.commits)}"})()

    def create_tag(self, repo: str, *, tag: str, revision: str, repo_type: str, tag_message: str) -> None:
        if tag in self.tags.setdefault(repo, {}):
            raise ValueError("tag exists")
        self.tags[repo][tag] = revision

    def update_repo_settings(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("the job must never change a repo's visibility")


def _staged(ops: Any, rid: str, ns: str = "", public_policy: Any = 2) -> None:
    """A staged release of one release.json per variant; the public one names quote policy ``public_policy``
    (D808; ``...`` leaves the key out)."""
    files = {}
    for v in sr.VARIANTS:
        doc: dict[str, Any] = {"variant": v}
        if v == "public" and public_policy is not ...:
            doc["policy"] = {"quote_policy": {"version": public_policy}}
        data = json.dumps(doc).encode()
        ops.b.put(f"{ns}releases/{rid}/cyc/{v}/release.json", data)
        files[v] = [{"path": "release.json", "bytes": len(data), "sha256": sr.hashlib.sha256(data).hexdigest()}]
    ops.put_json(f"{ns}releases/{rid}/manifest.json", {
        "release_id": rid, "prefix": f"{ns}releases/{rid}/cyc",
        "variants": {v: {"hf_repo": f"org/{v}", "rows": 1, "files": files[v]} for v in sr.VARIANTS}})


@pytest.fixture
def hf(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("HF_TOKEN", "hf-test")
    monkeypatch.setenv("STACK_SCHED_WORK", str(tmp_path))
    FakeHf.private, FakeHf.uploads, FakeHf.publish_on_upload = {"org/public": True, "org/internal": True}, [], set()
    FakeHf.trees, FakeHf.tags, FakeHf.commits = {}, {}, []
    fake_hub = type(sys)("huggingface_hub")
    fake_hub.HfApi = FakeHf  # type: ignore[attr-defined]
    fake_hub.CommitOperationAdd = type("CommitOperationAdd", (), {  # type: ignore[attr-defined]
        "__init__": lambda self, path_in_repo, path_or_fileobj: setattr(self, "path_in_repo", path_in_repo)})
    fake_hub.CommitOperationDelete = type("CommitOperationDelete", (), {  # type: ignore[attr-defined]
        "__init__": lambda self, path_in_repo, is_folder: setattr(self, "path_in_repo", path_in_repo)})
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    # the staged fakes are one release.json each, not releases: the checker is stubbed here and run for real in
    # test_upload_runs_the_releases_own_checker
    monkeypatch.setattr(sr, "check_staged", lambda d: [])
    return FakeHf


def test_upload_makes_each_tree_the_release_in_one_tagged_commit(hf: Any) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_OK
    assert hf.uploads == ["org/internal", "org/public"] and len(hf.commits) == 2
    assert hf.trees["org/internal"] == {".gitattributes", "release.json"}  # old.parquet dropped in the same commit
    assert hf.tags == {"org/internal": {"release-2026-11-01": "c1"}, "org/public": {"release-2026-11-01": "c2"}}
    done = ops.get_json("releases/2026-11-01/uploaded-internal.json")
    assert done["private_after"] is True and done["removed_files"] == ["old.parquet"] and done["tag_status"] == "created"


def test_upload_again_keeps_the_existing_tag_and_warns(hf: Any) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_OK
    seen: list[str] = []
    finds = type("F", (), {"warn": lambda self, m: seen.append(m)})()
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False, findings=finds) == sr.EXIT_OK
    assert hf.tags["org/public"] == {"release-2026-11-01": "c2"} and len(seen) == 2 and "not created" in seen[0]


def test_upload_goes_to_a_public_public_repo_as_it_is(hf: Any) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    hf.private["org/public"] = False  # Tomáš made it public: upload to it, change nothing
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_OK
    assert hf.uploads == ["org/internal", "org/public"] and hf.private["org/public"] is False


def test_upload_refuses_when_the_private_repo_is_public(hf: Any) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    hf.private["org/internal"] = False
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_FAILED
    assert hf.uploads == [] and hf.private["org/internal"] is False  # nothing uploaded, visibility untouched


def test_upload_fails_when_the_private_repo_is_public_afterwards(hf: Any) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    hf.publish_on_upload = {"org/internal"}
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_FAILED
    assert hf.uploads == ["org/internal"] and hf.private["org/internal"] is False  # public not uploaded


def test_upload_refuses_a_file_changed_after_staging(hf: Any) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    ops.b.put("releases/2026-11-01/cyc/internal/release.json", b"tampered")
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_FAILED
    assert hf.uploads == []


def test_upload_refuses_a_release_its_checker_refuses(hf: Any, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    checked: list[str] = []

    def check(d: Path) -> list[str]:
        checked.append(d.name)
        return ["exit 1", "[rows] data/corpus.parquet doc_id: 1 row(s)"] if d.name == "public" else []

    monkeypatch.setattr(sr, "check_staged", check)
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_FAILED
    assert checked == ["internal", "public"] and hf.uploads == []  # both checked before anything goes up
    assert "ERROR: public: the release checker (data-repo/tools/release-validator.py --rows all) refuses it" in \
        capsys.readouterr().out


@pytest.mark.parametrize("version", [..., None, 1, 3, "2", 2.0])
def test_upload_refuses_a_public_release_without_a_supported_quote_policy(hf: Any, version: Any,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """D808: checked during validation, before either commit; the internal variant is not checked for it."""
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01", public_policy=version)
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-01", dry=False) == sr.EXIT_FAILED
    assert hf.uploads == [] and hf.commits == []
    assert "ERROR: public: release.json policy.quote_policy.version is" in capsys.readouterr().out


def _stage_dir(ops: Any, rid: str, dirs: dict[str, Path]) -> None:
    """Stage real release directories as the cycle does (files under the prefix, the manifest last)."""
    files = {v: [{"path": p.relative_to(d).as_posix(), "bytes": p.stat().st_size, "sha256": sr.sha256_file(p)}
                 for p in sorted(d.rglob("*")) if p.is_file()] for v, d in dirs.items()}
    for v, d in dirs.items():
        for f in files[v]:
            ops.b.put(f"releases/{rid}/cyc/{v}/{f['path']}", (d / f["path"]).read_bytes())
    ops.put_json(f"releases/{rid}/manifest.json", {
        "release_id": rid, "prefix": f"releases/{rid}/cyc",
        "variants": {v: {"hf_repo": f"org/{v}", "rows": 1, "files": files[v]} for v in dirs}})


@pytest.mark.slow
def test_upload_runs_the_releases_own_checker(hf: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The real check_staged (the shipped tools/release-validator.py, every row) over synthetic exports: valid ones
    upload; a variant with a row its schema refuses (re-staged with matching sha256s) uploads nothing."""
    from tests.test_release_format import _rehash, _rewrite_corpus, export

    monkeypatch.setattr(sr, "check_staged", REAL_CHECK_STAGED)
    pub, _s = export(tmp_path / "p")
    intern, _s = export(tmp_path / "i", "internal", n=4)
    assert sr.check_staged(pub) == [] and sr.check_staged(intern) == []
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _stage_dir(ops, "2026-11-03", {"public": pub, "internal": intern})
    assert sr.upload(sr.Settings.from_env(), ops, "2026-11-03", dry=False) == sr.EXIT_OK
    assert hf.uploads == ["org/internal", "org/public"]

    def bad(rows: list[dict[str, Any]]) -> None:
        rows[0]["release"]["abstract"] = "leaked"  # a closed list's unknown value

    _rewrite_corpus(pub, bad)
    _rehash(pub)
    got = sr.check_staged(pub)
    assert got[0] == "exit 1" and any("release.abstract" in x for x in got), got
    hf.uploads.clear()
    ops2 = sr.Ops(OPS, read_only=False, client=FakeS3())
    _stage_dir(ops2, "2026-11-04", {"public": pub, "internal": intern})
    assert sr.upload(sr.Settings.from_env(), ops2, "2026-11-04", dry=False) == sr.EXIT_FAILED
    assert hf.uploads == []


def test_check_staged_never_runs_the_staged_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged checker that is not the image's is refused unread (a writer of the staging prefix could run code with
    the job's keys, or ship a checker that passes everything); the image's runs isolated, without the keys."""
    marker = tmp_path / "ran"
    (tmp_path / "rel" / "tools").mkdir(parents=True)
    (tmp_path / "rel" / "tools" / "release-validator.py").write_text(
        f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text(os.environ.get('HF_TOKEN', ''))\n")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    assert REAL_CHECK_STAGED(tmp_path / "rel") == [
        "tools/release-validator.py: not this image's data-repo/tools/release-validator.py (sha256 differs); not run"]
    assert not marker.exists()
    shutil.copyfile(sr.OWN_CHECKER, tmp_path / "rel" / "tools" / "release-validator.py")
    seen: dict[str, Any] = {}

    def run(cmd: list[str], **kw: Any) -> Any:
        seen.update(cmd=cmd, env=kw["env"])
        return sr.subprocess.CompletedProcess(cmd, 0, "VALID", "")

    monkeypatch.setattr(sr.subprocess, "run", run)
    assert REAL_CHECK_STAGED(tmp_path / "rel") == []
    assert seen["cmd"][:3] == [sys.executable, "-I", str(sr.OWN_CHECKER)] and seen["cmd"][-2:] == ["--rows", "all"]
    assert set(seen["env"]) <= set(sr.CHECK_ENV) and "HF_TOKEN" not in seen["env"]


def test_check_staged_refuses_a_release_without_its_checker(tmp_path: Path) -> None:
    (tmp_path / "release.json").write_text('{"release_id": "2026-10-30", "format": "2026-10-30"}')
    assert sr.check_staged(tmp_path) == ["tools/release-validator.py: missing (a release from before format 1.0.0?)"]
    (tmp_path / "tools").mkdir()
    tool = tmp_path / "tools" / "release-validator.py"
    shutil.copyfile(sr.OWN_CHECKER, tool)
    got = sr.check_staged(tmp_path)  # a file release.json does not list: invalid, whatever the version
    assert got[0] == "exit 1" and got[-1] == "INVALID", got
    listed = {"path": "tools/release-validator.py", "bytes": tool.stat().st_size, "sha256": sr.sha256_file(tool)}
    (tmp_path / "release.json").write_text(json.dumps({"release_id": "2026-10-30", "format": "2026-10-30",
                                                       "files": [listed]}))
    got = sr.check_staged(tmp_path)  # a release from before format 1.0.0: unsupported
    assert got[0] == "exit 3" and got[-1] == "UNSUPPORTED FORMAT VERSION", got


def test_upload_refused_in_test_mode(hf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ops = sr.Ops(OPS, read_only=False, client=FakeS3())
    _staged(ops, "2026-11-01")
    assert sr.upload(sr.Settings.from_env(test=True), ops, "2026-11-01", dry=False) == sr.EXIT_REFUSED
    assert hf.uploads == []


def test_pause_refuses_a_new_cycle_and_report_reads_the_summary(env: None, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeS3()
    real_ops = sr.Ops

    def ops_on_fake(uri: str, *, read_only: bool, client: Any = None) -> Any:
        return real_ops(uri, read_only=read_only, client=fake)

    monkeypatch.setattr(sr, "Ops", ops_on_fake)
    assert sr.main(["pause", "--note", "plan change"]) == sr.EXIT_OK
    monkeypatch.setattr(sr, "plan_info", lambda _p: ("d" * 64, "batch-import", {}))
    monkeypatch.setattr(sr, "preflight", lambda _s, _d: ([], []))
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    monkeypatch.setattr(sr, "spend_since", lambda *_a, **_k: 0.0)
    assert cyc.run() == sr.EXIT_REFUSED and cyc.outcome == "refused"
    assert "stack-store-ops/lock.json" not in fake.keys()  # refused before the lock
    capsys.readouterr()
    assert sr.main(["report"]) == sr.EXIT_OK
    assert '"outcome": "refused"' in capsys.readouterr().out
    assert sr.main(["resume"]) == sr.EXIT_OK
    assert "stack-store-ops/hold.json" not in fake.keys()


def _canned_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, builds: list[tuple[str, list[Any]]],
                  runs: list[dict[str, Any]] | None = None) -> Any:
    """Patch the cycle onto a fake S3 and canned `stack` results (``runs``: what `stack show runs` lists for every
    build); returns (fake S3, the calls made)."""
    import json as _json

    fake = FakeS3()
    real_ops = sr.Ops
    monkeypatch.setattr(sr, "Ops", lambda uri, *, read_only, client=None: real_ops(uri, read_only=read_only,
                                                                                    client=fake))
    monkeypatch.setattr(sr, "plan_info", lambda _p: ("d" * 64, "batch-import", {"annotate.screen": 40.0}))
    monkeypatch.setattr(sr, "preflight", lambda _s, _d: ([], []))
    monkeypatch.setattr(sr, "status_json", lambda: {"jobs": []})
    monkeypatch.setattr(sr, "last_openalex_full", lambda *_a: sr.utcnow())
    monkeypatch.setattr(sr, "check_balances", lambda _s: [("openrouter", "OK", "$80 left")])
    monkeypatch.setattr(sr, "openalex_budget_stops", lambda *_a: [])
    monkeypatch.setattr(sr, "s2_collect_note", lambda *_a: None)
    monkeypatch.setenv("STACK_SCHED_WORK", str(tmp_path))
    it = iter(builds)
    calls: list[list[str]] = []

    def fake_stack(args: list[str], **_kw: Any) -> Any:
        calls.append(args)
        if args[0] == "run":
            return sr.Result(0, _json.dumps({"run_id": "r" + args[1][-4:], "status": "ok", "scope_complete": True}),
                             [], 1.0)
        if args[0] == "build":
            st, inc = next(it)
            doc = {"build_id": f"b{len(calls)}", "status": st, "incomplete": inc,
                   "files": {"corpus": {"path": "data/corpus.build/batch-import/x.full.parquet"}}}
            return sr.Result(0 if st == "ok" else 3, _json.dumps(doc), [], 1.0)
        if args[0] == "show":
            return sr.Result(0, _json.dumps({"runs": runs if runs is not None else [
                {"stage": "acquire", "counts": {"rows_written": 12, "outcome.ok": 12}},
                {"stage": "triage", "counts": {"asked_pairs": 5}}]}), [], 1.0)
        if args[0] == "export":
            out = Path(args[args.index("--out") + 1])
            (out / "data").mkdir(parents=True)
            v = args[args.index("--config") + 1].removeprefix("dataset-")
            q = {"judge_eval": {"build": "bJ", "scored_build": "b7",
                                "coverage": {"screen": {"items": 100, "scored": 98, "dropped": {"missing": 2}}}},
                 "triage_vs_screen": {"overall": {"n": 10, "tp": 4, "precision": 0.5, "recall": 0.8, "f1": 0.6154},
                                      "by_kind": {"signal": {"n": 6, "precision": 0.4, "recall": 0.75, "f1": 0.52}}}}
            pol = {"quote_policy": {"version": 2}} if v == "public" else {}  # D808: the upload checks it
            (out / "release.json").write_text(_json.dumps({"hf_repo": f"org/{v}", "rows": {"out": 7}, "quality": q,
                                                           "policy": pol}))
            (out / "data" / "corpus.parquet").write_bytes(b"rows")
            return sr.Result(0, "", [], 1.0)
        raise AssertionError(args)

    monkeypatch.setattr(sr, "run_stack", fake_stack)
    return fake, calls


def test_a_failed_openalex_resweep_warns_and_the_cycle_goes_on(hf: Any, monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    """D789: the due re-sweep failing (the 2026-11-01 production cycle: `failed[s3_transient]` after 110 min) is a
    WARN; the standalone collects, the builds (whose collect.openalex is the week's incremental pass), the export and
    the upload follow, exit 0. Before, the cycle halted there (ERROR, exit 1, no build)."""
    import json as _json

    fake, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("ok", [])])
    monkeypatch.setattr(sr, "last_openalex_full", lambda *_a: None)  # never: due
    canned = sr.run_stack

    def failing_resweep(args: list[str], **kw: Any) -> Any:
        if args[:2] == ["run", "collect.openalex"]:
            assert (kw.get("env") or {}).get("STACK_OPENALEX_FULL") == "1"
            calls.append(args)
            doc = {"run_id": "r048", "status": "failed", "error_class": "s3_transient", "scope_complete": True,
                   "outputs": {}}
            return sr.Result(1, _json.dumps(doc), ["ClientError[SlowDown]"], 6606.0)
        return canned(args, **kw)

    monkeypatch.setattr(sr, "run_stack", failing_resweep)
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_OK and cyc.outcome == "ok", cyc.errors
    assert [c[1] if c[0] == "run" else c[0] for c in calls] == [
        "collect.openalex", "collect.semantic_scholar", "collect.arxiv_sweep", "build", "collect.s2-lookup", "build",
        "export", "export"]
    assert cyc.errors == [] and len(cyc.warnings) == 1, (cyc.errors, cyc.warnings)
    assert cyc.warnings[0].startswith("OpenAlex re-sweep failed (run r048 failed[s3_transient], rc=1)")
    assert cyc.steps[0]["step"] == "openalex-full" and cyc.steps[0]["tail"] == ["ClientError[SlowDown]"]
    assert hf.uploads == ["org/internal", "org/public"]


def test_s2_collect_failed_permanent_and_broken_bounds_are_a_warn(hf: Any, monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    """D802: S2 runs standalone (D554), so the build's ``failed_permanent_note`` never sees its run; the cycle reads
    the run record and WARNs on ``windows_failed_permanent.*`` and the broken upper bounds, once per S2 run (the
    second run too). The cycle goes on (exit 0)."""
    import json as _json

    asked: list[str] = []

    def cycle(note: Any) -> Any:
        _fake, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("ok", [])])
        canned = sr.run_stack

        def s2_incomplete(args: list[str], **kw: Any) -> Any:  # the first S2 run scope-incomplete: a second runs
            if args[:2] == ["run", "collect.semantic_scholar"]:
                calls.append(args)
                n = sum(c[:2] == ["run", "collect.semantic_scholar"] for c in calls)
                doc = {"run_id": f"rs2{n}", "status": "ok", "scope_complete": n > 1, "outputs": {}}
                return sr.Result(0, _json.dumps(doc), [], 1.0)
            return canned(args, **kw)

        monkeypatch.setattr(sr, "run_stack", s2_incomplete)
        monkeypatch.setattr(sr, "s2_collect_note", note)
        cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
        assert cyc.run() == sr.EXIT_OK and cyc.outcome == "ok", cyc.errors
        return cyc

    def note(store: str, run_id: str) -> str | None:
        asked.append(run_id)
        return f"collect.semantic_scholar run {run_id}: 1 coverage window(s) failed_permanent" if run_id == "rs21" \
            else None

    cyc = cycle(note)
    assert asked == ["rs21", "rs22"]
    assert cyc.warnings == ["collect.semantic_scholar run rs21: 1 coverage window(s) failed_permanent"]
    # a note that cannot be read is a WARN, never the cycle's crash
    cyc = cycle(lambda *_a: 1 / 0)  # (the same day's second release adds the fake hub's tag WARNs)
    assert [w for w in cyc.warnings if w.startswith("s2-")] == ["s2-collect: run rs21's counts not read: ZeroDivisionError",
                            "s2-collect-2: run rs22's counts not read: ZeroDivisionError"]


def test_s2_collect_note_reads_the_run_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """D802: the note names the failed_permanent windows (the build's text) and the broken upper bounds; a run
    without either gives none; a run the index does not list is said."""
    import types

    import stack.cli.common
    import stack.store.index
    import stack.store.remote

    rec = lambda rid, counts: types.SimpleNamespace(record=types.SimpleNamespace(
        stage="collect.semantic_scholar", run_id=rid, counts=counts))
    runs = [rec("rbad", {"windows_failed_permanent": 1, "windows_failed_permanent.query_invalid": 1,
                         "upper_bound_ignored": 2, "upper_bound_fallback": 1}),
            rec("rok", {"windows_ok": 5, "upper_bound_ignored": 0})]
    monkeypatch.setattr(stack.cli.common, "mirror_base", lambda: None)
    monkeypatch.setattr(stack.store.remote, "resolve_store", lambda store, **_k: store)
    monkeypatch.setattr(stack.store.index, "store_index", lambda root, **_k: types.SimpleNamespace(runs=runs))
    note = sr.s2_collect_note("store", "rbad")
    assert note is not None
    assert "1 coverage window(s) failed_permanent (query_invalid x1)" in note
    assert note.endswith("S2 ignored a year span's upper bound x2, the span re-asked open-ended x1 (D802)")
    assert sr.s2_collect_note("store", "rok") is None
    assert sr.s2_collect_note("store", "rgone") == ("S2 collect run rgone: no run record in the store index (its "
                                                    "counts were not checked)")


def test_cycle_sequence_resume_stage_and_publish(hf: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The whole cycle on canned `stack` results: the standalone runs, build 1, the s2-lookup, an incomplete build
    (acquire's clock) that made progress, an ok build, both exports checked and staged with the manifest last,
    then both uploaded (no approval step)."""
    import json as _json

    fake, calls = _canned_cycle(monkeypatch, tmp_path, [
        ("ok", []), ("incomplete", [{"stage": "acquire", "run_id": "a", "reasons": ["time"]}]), ("ok", [])])
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.s.max_builds == 5 and cyc.s.openalex_full_days == 6
    assert cyc.run() == sr.EXIT_OK and cyc.outcome == "ok"
    assert [c[1] if c[0] == "run" else c[0] for c in calls] == [
        "collect.semantic_scholar", "collect.arxiv_sweep", "build", "collect.s2-lookup", "build", "show", "build",
        "export", "export"]
    assert "--skip" in calls[2] and "triage" in calls[2] and "triage" not in calls[4]
    rid = f"{sr.utcnow():%Y-%m-%d}"
    m = _json.loads(fake.objects[("stack-data", f"stack-store-ops/releases/{rid}/manifest.json")])
    assert m["build_id"] == "b7" and m["variants"]["internal"]["hf_repo"] == "org/internal"
    assert {f["path"] for f in m["variants"]["public"]["files"]} == {"release.json", "data/corpus.parquet"}
    assert hf.uploads == ["org/internal", "org/public"] and cyc.release["uploaded"] is True
    q = cyc.release["quality"]  # D728: the summary carries the release's quality lines
    assert q["judge_eval"] == {"labels_build": "bJ", "scored_build": "b7", "scored": {"screen": "98/100"}}
    assert q["triage_vs_screen"]["overall"] == {"n": 10, "precision": 0.5, "recall": 0.8, "f1": 0.6154}
    assert q["triage_vs_screen"]["kind:signal"]["n"] == 6 and "not accuracy" in q["triage_vs_screen"]["note"]
    assert ("stack-data", "stack-store-ops/lock.json") not in fake.objects  # released
    assert _json.loads(fake.objects[("stack-data", "stack-store-ops/cycles/latest.json")])["outcome"] == "ok"


#: `stack show runs` of build wtwv0wpnr8hfv (2026-10-27), acquire's last two runs reduced to the counts the cycle reads
#: (`scope.fetch` 72,935 in each of its six acquire runs: the tail's share D733's limit reads)
INCIDENT_RUNS = [
    {"stage": "triage", "run_id": "re5v", "counts": {"asked_pairs": 0}},
    {"stage": "acquire", "run_id": "r5gx", "counts": {"rows_written": 404, "outcome.ok": 26, "scope.due": 362,
                                                      "scope.fetch": 72935}},
    {"stage": "acquire", "run_id": "rsd5", "counts": {"rows_written": 16, "scope.due": 678, "scope.fetch": 72935,
                                                      "halted": 1, "halted.oa": 1, "stopped.halted": 1,
                                                      "outcome.failed_transient": 30, "refresh_pending": 31}}]


def test_a_build_short_only_on_acquires_halted_tail_is_released_and_ends_the_builds(
        hf: Any, mailbox: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The 2026-10-27 test cycle's case: build 2.1 short only on acquire, `halted` (the OA lane's lane-wide 429): no
    further build although 4 more are allowed; the build is released (both exports with --allow-incomplete,
    uploaded), exit 0, one WARN naming the tail's size and the halted lane."""
    import json as _json

    fake, calls = _canned_cycle(monkeypatch, tmp_path, [
        ("ok", []), ("incomplete", [{"stage": "acquire", "run_id": "rsd5", "reasons": ["halted"]}]),
        ("ok", [])], runs=INCIDENT_RUNS)
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.s.max_builds == 5
    assert cyc.run() == sr.EXIT_OK and cyc.outcome == "ok" and cyc.errors == []
    assert [c[0] for c in calls if c[0] != "run"] == ["build", "build", "show", "export", "export"]
    assert all("--allow-incomplete" in c for c in calls if c[0] == "export")
    rid = f"{sr.utcnow():%Y-%m-%d}"
    m = _json.loads(fake.objects[("stack-data", f"stack-store-ops/releases/{rid}/manifest.json")])
    assert m["build_id"] == "b5" and hf.uploads == ["org/internal", "org/public"]
    assert len(cyc.warnings) == 1, cyc.warnings
    w = cyc.warnings[0]
    assert w.startswith("build b5 short only on acquire's retry tail (halted; last acquire run rsd5: 678 rows due, "
                        "0 roles ok, 30 roles failed_transient, 31 waiting on a retry_after, halted lanes: oa; 26 roles ok over "
                        "the build's 2 acquire runs); the tail is 678 rows due, 0.93% of the 72935 rows acquire covers "
                        "(scope.fetch; STACK_SCHED_TAIL_MAX_PCT 5)") and w.endswith("released, the next cycle retries the tail")
    assert mailbox[-1]["subject"].startswith("WARN: ") and "acquire's retry tail" in mailbox[-1]["text"]


def test_a_halted_tail_ends_the_builds_even_when_its_last_run_made_progress(
        hf: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """D733's halted stop on its own: the tail is `halted` but the last acquire run still completed 12 roles ok (at
    least HALTED_REPEAT_MIN_OK), so D734's progress rule alone would build again; the halt ends the builds and that
    build is released. The incident test above has 0 roles ok, where both rules stop, so it stays green with the
    halted stop removed (a review's mutation M2); this one does not."""
    halted = [{"stage": "acquire", "run_id": "a", "reasons": ["halted"]}]
    runs = [{"stage": "triage", "run_id": "t", "counts": {"asked_pairs": 5}},
            {"stage": "acquire", "run_id": "a", "counts": {"rows_written": 393, "outcome.ok": 12, "scope.due": 400,
                                                           "scope.fetch": 72935, "stopped.budget": 1}}]
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("incomplete", halted), ("ok", [])], runs=runs)
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_OK and cyc.release["build_id"] == "b5"
    assert [c[0] for c in calls if c[0] != "run"] == ["build", "build", "show", "export", "export"]


def test_an_acquire_tail_on_the_clock_goes_on_and_the_newest_tail_build_is_released(
        hf: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Short only on acquire's clock (`time`, no halt) with progress (the last acquire run completed at least
    HALTED_REPEAT_MIN_OK roles; the incident's run 2: 393 rows written, 12 roles ok): the next build goes on (a
    catch-up); when no build ends ok within the builds, the newest tail build is released (WARN). The same rows with
    1 role ok: no further build, that build is released. With STACK_SCHED_RELEASE_ALLOW_INCOMPLETE=0 none is (exit 3,
    ERROR), and a build short on triage is never released."""
    tail = [{"stage": "acquire", "run_id": "a", "reasons": ["time"]}]

    def run2(ok: int) -> list[dict[str, Any]]:
        return [{"stage": "triage", "run_id": "t", "counts": {"asked_pairs": 5}},
                {"stage": "acquire", "run_id": "a0", "counts": {"rows_written": 404, "outcome.ok": 26}},
                {"stage": "acquire", "run_id": "a", "counts": {"rows_written": 393, "outcome.ok": ok, "scope.due": 400,
                                                               "scope.fetch": 72935, "outcome.failed_transient": 730,
                                                               "stopped.budget": 1}}]
    assert sr.last_run_ok(run2(12), "a") == 12 and sr.last_run_ok(run2(12), None) == 12
    monkeypatch.setenv("STACK_SCHED_MAX_BUILDS", "2")
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("incomplete", tail), ("incomplete", tail)],
                              runs=run2(12))
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_OK and cyc.release["build_id"] == "b7"  # the second of the two tail builds
    assert [c[0] for c in calls if c[0] != "run"] == ["build", "build", "show", "build", "show", "export", "export"]
    assert len(cyc.warnings) == 1 and cyc.warnings[0].startswith(
        "build b7 short only on acquire's retry tail (time; last acquire run a: 400 rows due, 12 roles ok, 730 roles "
        "failed_transient, 0 waiting on a retry_after, halted lanes: -; 38 roles ok over the build's 2 acquire runs)")
    # the same rows written, ~0 roles ok: rows are no progress; no further build, the tail build is released
    monkeypatch.setenv("STACK_SCHED_RELEASE_ID", "2026-12-23")
    _f, calls = _canned_cycle(monkeypatch, tmp_path / "1", [("ok", []), ("incomplete", tail), ("incomplete", tail)],
                              runs=run2(1))
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_OK and cyc.release["build_id"] == "b5"
    assert [c[0] for c in calls if c[0] != "run"] == ["build", "build", "show", "export", "export"]
    # a tail build, then one short on triage: the tail build is the release
    _f, calls = _canned_cycle(monkeypatch, tmp_path / "2", [
        ("ok", []), ("incomplete", tail), ("incomplete", [{"stage": "triage", "run_id": "t", "reasons": ["budget"]}])],
        runs=run2(12))
    monkeypatch.setenv("STACK_SCHED_RELEASE_ID", "2026-12-24")
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_OK and cyc.release["build_id"] == "b5"
    # release of incomplete builds off: no release, the cycle ends incomplete
    monkeypatch.setenv("STACK_SCHED_RELEASE_ALLOW_INCOMPLETE", "0")
    _f, calls = _canned_cycle(monkeypatch, tmp_path / "3", [("ok", []), ("incomplete", tail), ("incomplete", tail)],
                              runs=run2(12))
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_INCOMPLETE and cyc.outcome == "incomplete"
    assert not any(c[0] == "export" for c in calls) and "no releasable build" in cyc.errors[-1]


def _tail_runs(ok: int = 12, due: int = 400, covered: int | None = 72935) -> list[dict[str, Any]]:
    """`stack show runs` of a clock-tail build: the last acquire run's roles ok, `scope.due` and `scope.fetch`."""
    counts: dict[str, Any] = {"rows_written": 393, "outcome.ok": ok, "scope.due": due, "stopped.budget": 1}
    if covered is not None:
        counts["scope.fetch"] = covered
    return [{"stage": "triage", "run_id": "t", "counts": {"asked_pairs": 5}},
            {"stage": "acquire", "run_id": "a", "counts": counts}]


@pytest.mark.parametrize("stop", ["cap", "cap-drains-catch-up", "hold"])
def test_a_tail_build_is_released_after_a_cap_or_stop_before_the_catch_up(
        hf: Any, mailbox: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop: str) -> None:
    """2026-10-30: a clock tail with progress (12 roles ok: the next build would catch up), then the wall cap
    before the catch-up build starts (or the cap drains the catch-up build, or the operator's hold.json): the tail
    build is released as after a halted acquire (both exports, uploaded), exit 0, WARN naming the stop, no ERROR.
    Before, D733 released nothing after a stop (exit 3, ERROR)."""
    tail = [{"stage": "acquire", "run_id": "a", "reasons": ["time"]}]
    catch_up = ("incomplete", tail) if stop == "cap-drains-catch-up" else ("ok", [])
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("incomplete", tail), catch_up], runs=_tail_runs())
    monkeypatch.setattr(sr.children, "stop_reason", None)  # module-global: restored after the test
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    real_build, n = cyc.build, {"builds": 0}

    def build(label: str, skips: list[str]) -> Any:
        n["builds"] += 1
        rec, r = real_build(label, skips)
        if stop == "cap-drains-catch-up" and n["builds"] == 3:  # the cap came during build 2.2: drained
            sr.children.request_stop("wall 20.1 h >= STACK_SCHED_MAX_WALL_H 20.0")
            rec = {**rec, "stopped": {"by": "signal"}}
        return rec, r

    monkeypatch.setattr(cyc, "build", build)
    if stop == "cap":
        monkeypatch.setattr(cyc, "over_caps", lambda: "wall 20.1 h >= STACK_SCHED_MAX_WALL_H 20.0"
                            if n["builds"] >= 2 else None)
    elif stop == "hold":
        monkeypatch.setattr(cyc, "held", lambda: "hold.json present (now: deploy)" if n["builds"] >= 2 else None)
    rc = cyc.run()
    assert rc == sr.EXIT_OK and cyc.outcome == "ok" and cyc.errors == [], cyc.errors
    assert sum(c[0] == "build" for c in calls) == (3 if stop == "cap-drains-catch-up" else 2)
    assert cyc.release["build_id"] == "b5" and hf.uploads == ["org/internal", "org/public"]
    assert all("--allow-incomplete" in c for c in calls if c[0] == "export")
    assert len(cyc.warnings) == 1 and f"released after the cycle stopped ({sr.children.stop_reason})" in cyc.warnings[0]
    assert mailbox[-1]["subject"].startswith("WARN: ")


def _cancel_build(monkeypatch: pytest.MonkeyPatch, nth: int, *, stop: str | None) -> None:
    """The ``nth`` `stack build` of the canned cycle ends as the 2026-10-31 test cycle's build w3hp9zbfm9dfa did: the
    cap (``stop``) drains it, its final verify outlives the drain grace, the second SIGTERM cancels it: exit 130,
    no record on stdout, the ``started`` line on stderr. ``stop`` None: a cancel the cycle did not cause."""
    canned, n = sr.run_stack, {"builds": 0}

    def run_stack(args: list[str], **kw: Any) -> Any:
        if args[0] == "build":
            n["builds"] += 1
            if n["builds"] == nth:
                if stop is not None:
                    sr.children.request_stop(stop)
                return sr.Result(130, "", ["build w3hp9zbfm9dfa (batch-2026-10a): cancelled[operator]"], 1.0,
                                 ["build w3hp9zbfm9dfa (batch-2026-10a): started, upload true"])
        return canned(args, **kw)

    monkeypatch.setattr(sr, "run_stack", run_stack)


def test_a_build_the_cap_drained_and_the_grace_cancelled_is_stopped_not_failed(
        hf: Any, mailbox: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """D781, the 2026-10-31 test cycle 20261031T0617Z-89d9: the wall cap drained build 2.1 during acquire and the
    drain grace ran out in its final verify, so `stack build` was cancelled (exit 130) and printed no record. Before:
    `halted: build None failed: status None[None]`, exit 1. Now the cycle reads it as stopped by its own cap: no
    releasable build, outcome `stopped`, exit 3, the build named from its `started` line."""
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", [])])
    monkeypatch.setattr(sr.children, "stop_reason", None)
    _cancel_build(monkeypatch, 2, stop="wall 6.1 h >= STACK_SCHED_MAX_WALL_H 6.0")
    cyc = sr.Cycle(sr.Settings.from_env(test=True), dry=False)
    assert cyc.run() == sr.EXIT_INCOMPLETE and cyc.outcome == "stopped"
    step = cyc.steps[-1]
    assert step["build_id"] == "w3hp9zbfm9dfa" and step["status"] == "cancelled" and step["rc"] == 130
    assert len(cyc.errors) == 1 and cyc.errors[0].startswith("no releasable build (wall 6.1 h"), cyc.errors
    assert "then cancelled[operator]" in cyc.errors[0] and not any("halted" in e for e in cyc.errors)
    assert not any(c[0] == "export" for c in calls) and mailbox[-1]["subject"].startswith("[test] ERROR: ")


def test_a_tail_build_is_released_when_the_cap_cancels_the_catch_up_build(
        hf: Any, mailbox: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """D769 with D781: a clock tail build, then the cap drains the catch-up build and the grace cancels it in its
    final verify (no record): the tail build is still released, exit 0, WARN. Before: `halted: build None failed`,
    exit 1, nothing released."""
    tail = [{"stage": "acquire", "run_id": "a", "reasons": ["time"]}]
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("incomplete", tail)], runs=_tail_runs())
    monkeypatch.setattr(sr.children, "stop_reason", None)
    _cancel_build(monkeypatch, 3, stop="wall 20.1 h >= STACK_SCHED_MAX_WALL_H 20.0")
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_OK and cyc.outcome == "ok" and cyc.errors == [], cyc.errors
    assert sum(c[0] == "build" for c in calls) == 2  # the canned builds; the third is the cancelled one
    assert cyc.release["build_id"] == "b5" and hf.uploads == ["org/internal", "org/public"]
    assert len(cyc.warnings) == 1 and "released after the cycle stopped (wall 20.1 h" in cyc.warnings[0]


def test_a_cancel_the_cycle_did_not_cause_still_halts(hf: Any, mailbox: list[dict[str, Any]],
                                                      monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A build cancelled without the cycle's stop (an operator's `stack stop --now`) prints no record and halts the
    cycle as before (exit 1): only the cycle's own stop makes a missing record a stopped build."""
    _canned_cycle(monkeypatch, tmp_path, [("ok", [])])
    monkeypatch.setattr(sr.children, "stop_reason", None)
    _cancel_build(monkeypatch, 2, stop=None)
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.run() == sr.EXIT_FAILED and cyc.outcome == "halted"
    assert cyc.errors == ["halted: build None failed: status None[None]"]


def test_the_drain_grace_covers_a_builds_final_verify(env: None) -> None:
    """D781: the cancel comes 60 min after the drain (was 30), longer than a drained build-2's final verify on
    Railway (29-38 min on the 2026-10-27 and 2026-10-31 builds), so the build ends `incomplete` with its record."""
    assert sr.Settings.from_env().drain_grace_s == 3600 == sr.Settings.from_env(test=True).drain_grace_s


@pytest.mark.parametrize("build", ["ok", "collectors", "tail-after-cap"])
@pytest.mark.parametrize("how", ["watchdog", "taken-over", "unreadable"])
def test_nothing_is_released_after_the_lock_was_lost(hf: Any, mailbox: list[dict[str, Any]],
                                                     monkeypatch: pytest.MonkeyPatch, tmp_path: Path, build: str,
                                                     how: str) -> None:
    """D777 (amends D773): after a lost lock nothing is released, an ok build and one short only on collectors
    included: no export, no upload, no release WARN, ERROR `halted: lock lost …`, exit 1. Lost three ways: the
    watchdog's heartbeat flagged it (`lock_lost`), lock.json is another cycle's when read again just before the
    export (the watchdog's last heartbeat can be up to 5 min old), or lock.json cannot be read then."""
    tail = [{"stage": "acquire", "run_id": "a", "reasons": ["time"]}]
    last = {"ok": ("ok", []), "collectors": ("incomplete", [{"stage": "collect.cards", "run_id": "c",
                                                             "reasons": ["limit"]}]),
            "tail-after-cap": ("incomplete", tail)}[build]
    builds = [("ok", []), last] + ([("ok", [])] if build == "tail-after-cap" else [])
    _f, calls = _canned_cycle(monkeypatch, tmp_path, builds, runs=_tail_runs())
    monkeypatch.setattr(sr.children, "stop_reason", None)
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    real_build, n = cyc.build, {"builds": 0}

    def lost() -> None:
        if how == "watchdog":
            cyc.lock_lost = True
            sr.children.request_stop("lock lost")
        elif how == "taken-over":
            cyc.ops.put_json(sr.Lock.KEY, {"holder": "another-cycle"})
        else:
            def unreadable() -> bool:
                raise OSError("read timed out")
            monkeypatch.setattr(cyc.lock, "ours", unreadable)

    def build_(label: str, skips: list[str]) -> Any:
        n["builds"] += 1
        out = real_build(label, skips)
        if n["builds"] == 2:
            if build == "tail-after-cap":
                sr.children.request_stop("wall 20.1 h >= STACK_SCHED_MAX_WALL_H 20.0")
            lost()
        return out

    monkeypatch.setattr(cyc, "build", build_)
    assert cyc.run() == sr.EXIT_FAILED and cyc.outcome == "halted" and cyc.release == {}
    assert not any(c[0] == "export" for c in calls) and hf.uploads == [] and cyc.warnings == []
    assert len(cyc.errors) == 1 and cyc.errors[0].startswith("halted: lock")
    assert cyc.errors[0].endswith("build b5 not released (another cycle may hold the store)")
    assert ("not readable" in cyc.errors[0]) is (how == "unreadable")
    assert mailbox[-1]["subject"].startswith("ERROR: ")


def test_the_lock_is_read_again_before_the_upload(hf: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """D777: lock.json taken over by another cycle while the export ran: the release stays staged, nothing is
    uploaded, ERROR, exit 1. With the lock still ours both variants are uploaded (the canned cycle's own case)."""
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("ok", [])])
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    real_export = cyc.export

    def export(rec: dict[str, Any]) -> bool:
        ok = real_export(rec)
        cyc.ops.put_json(sr.Lock.KEY, {"holder": "another-cycle"})
        return ok

    monkeypatch.setattr(cyc, "export", export)
    assert cyc.run() == sr.EXIT_FAILED and cyc.outcome == "halted"
    assert sum(c[0] == "export" for c in calls) == 2 and hf.uploads == [] and cyc.release["uploaded"] is False
    assert cyc.errors[-1].startswith("halted: lock lost (lock.json no longer this cycle's when read again before the "
                                     "release): release ") and cyc.errors[-1].endswith("staged, not uploaded "
                                                                                        "(another cycle may hold the "
                                                                                        "store)")


def test_the_watchdog_marks_a_lost_lock(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A heartbeat that finds the lock no longer this cycle's sets `lock_lost` (what keeps any build from release,
    D777) and stops the cycle; a heartbeat that holds it sets nothing."""
    _canned_cycle(monkeypatch, tmp_path, [])
    clock = {"t": 0.0}

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

    class Done:
        def wait(self, s: float) -> bool:
            clock["t"] += s
            return clock["t"] > 400  # one heartbeat (heartbeat_s 300), then the loop ends

    class FakeLock:
        def __init__(self, ours: bool) -> None:
            self.ours = ours

        def heartbeat(self) -> bool:
            return self.ours

    monkeypatch.setattr(sr, "time", FakeTime)
    monkeypatch.setattr(sr, "spend_since", lambda *_a: 0.0)
    monkeypatch.setattr(sr.children, "current", None)
    for ours in (True, False):
        clock["t"] = 0.0
        monkeypatch.setattr(sr.children, "stop_reason", None)
        cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
        monkeypatch.setattr(cyc, "held", lambda: None)
        monkeypatch.setattr(cyc, "over_caps", lambda *_a: None)
        cyc.watchdog(FakeLock(ours), Done())  # type: ignore[arg-type]
        assert cyc.lock_lost is (not ours) and sr.children.stop_reason == (None if ours else "lock lost")


def _spend_jobs(since: str, build_liveness: str, finished_settled: float = 4.66) -> dict[str, Any]:
    """A status read of a cycle's jobs: a finished build and a second build with $8.00 open (an annotate run's
    reservations across its workers, 20261102T0059Z-729d), and an older job outside the cycle."""
    def job(i: str, liveness: str, settled: float, open_: float, at: str = since) -> dict[str, Any]:
        return {"kind": "build", "id": i, "liveness": liveness, "started_at": at,
                "spend_usd_micro": round(settled * 1e6), "open_usd_micro": round(open_ * 1e6)}
    return {"jobs": [job("b2", build_liveness, 0.0, 8.0), job("b1", "finished", finished_settled, 0.0),
                     job("old", "dead", 50.0, 50.0, "2000-01-01T00:00:00+00:00")]}


@pytest.mark.parametrize(("liveness", "settled", "want"), [
    ("live", 4.66, None),                                                  # a live run's open is its own --max-usd's
    ("dead", 4.66, "cycle spend $12.66 >= STACK_SCHED_MAX_USD 10.0"),     # a dead job's open is part of its spend
    ("unknown", 4.66, "cycle spend $12.66 >= STACK_SCHED_MAX_USD 10.0"),
    ("finished", 4.66, "cycle spend $12.66 >= STACK_SCHED_MAX_USD 10.0"),
    ("live", 10.5, "cycle spend $10.50 >= STACK_SCHED_MAX_USD 10.0"),     # settled alone over the cap
])
def test_the_cycle_cap_leaves_out_the_open_reservations_of_live_runs(
        env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, liveness: str, settled: float,
        want: str | None) -> None:
    """D797: the cycle's spend cap counts settled spend plus the open reservations of jobs that are not live."""
    _canned_cycle(monkeypatch, tmp_path, [])
    monkeypatch.setenv("STACK_SCHED_MAX_USD", "10")
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    st = _spend_jobs(cyc.t0iso, liveness, settled)
    monkeypatch.setattr(sr, "status_json", lambda: st)
    assert cyc.over_caps() == want
    assert cyc.over_caps(st) == want
    assert sr.spend_since(cyc.t0iso, st) == pytest.approx(settled + 8.0)  # the period total keeps every open amount


def test_the_live_line_and_the_cap_read_the_status_once(env: None, monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    """One status read per cap check serves the cap and the LIVE line, so they cannot disagree; LIVE shows what the
    cap counts and, apart, what is open in live runs. Each read here returns a different spend: a second read would
    show in the line or stop the cycle."""
    _canned_cycle(monkeypatch, tmp_path, [])
    monkeypatch.setenv("STACK_SCHED_MAX_USD", "10")
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    reads = [_spend_jobs(cyc.t0iso, "live"), _spend_jobs(cyc.t0iso, "live", 10.5)]
    calls: list[int] = []

    def status_json() -> dict[str, Any]:
        calls.append(1)
        return reads[len(calls) - 1]

    clock = {"t": 0.0}

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

    class Done:
        def wait(self, s: float) -> bool:
            clock["t"] += s
            return clock["t"] > cyc.s.watch_s + 60  # one cap check

    class FakeLock:
        def heartbeat(self) -> bool:
            return True

    lines: list[str] = []
    monkeypatch.setattr(sr, "status_json", status_json)
    monkeypatch.setattr(sr, "time", FakeTime)
    monkeypatch.setattr(sr, "log", lines.append)
    monkeypatch.setattr(sr, "mem_note", lambda _pid: "")
    monkeypatch.setattr(sr.children, "current", None)
    monkeypatch.setattr(sr.children, "stop_reason", None)
    monkeypatch.setattr(cyc, "held", lambda: None)
    cyc.watchdog(FakeLock(), Done())  # type: ignore[arg-type]
    assert len(calls) == 1 and sr.children.stop_reason is None
    live = [ln for ln in lines if ln.startswith("LIVE: ")]
    assert len(live) == 1 and live[0].startswith(f"LIVE: cycle {cyc.id}, spend $4.66 (+$8.00 open in live runs), "
                                                 "wall ")


def test_tail_share_reads_the_last_acquire_runs_due_and_covered_rows() -> None:
    assert sr.tail_share(INCIDENT_RUNS, "rsd5") == (678, 72935)
    assert sr.tail_share(INCIDENT_RUNS, None) == (678, 72935)  # no run id: the build's last acquire run
    assert sr.tail_share(_tail_runs(covered=None), "a") is None
    assert sr.tail_share(_tail_runs(covered=0), "a") is None
    assert sr.tail_share([], None) is None
    assert sr.tail_words((50, 1000), 5.0)[0] and not sr.tail_words((51, 1000), 5.0)[0]  # at most 5%: inclusive
    assert sr.tail_words((0, 1000), 0.0)[0] and not sr.tail_words((1, 1000), 0.0)[0]
    assert not sr.tail_words(None, 100.0)[0]
    assert sr.tail_words((678, 72935), 5.0) == (True, ("678 rows due, 0.93% of the 72935 rows acquire covers "
                                                       "(scope.fetch; STACK_SCHED_TAIL_MAX_PCT 5)"))


def test_a_retry_tail_over_its_limit_is_not_released(hf: Any, mailbox: list[dict[str, Any]],
                                                     monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """2026-10-30: a build short only on acquire's retry tail is released only while the tail (the last acquire
    run's `scope.due`) is at most STACK_SCHED_TAIL_MAX_PCT (5) percent of the rows acquire covers (`scope.fetch`).
    A backlog of 20,000 of 72,935 (27%) on the clock, every build making progress: no release, exit 3, ERROR naming
    the limit, as before D733. 5% exactly is released, one row more is not; the setting moves the limit; a tail of
    unknown size (no `scope.fetch`) is not released."""
    tail = [{"stage": "acquire", "run_id": "a", "reasons": ["time"]}]
    monkeypatch.setenv("STACK_SCHED_MAX_BUILDS", "2")
    _f, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("incomplete", tail), ("incomplete", tail)],
                              runs=_tail_runs(ok=500, due=20000))
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    assert cyc.s.tail_max_pct == 5.0
    assert cyc.run() == sr.EXIT_INCOMPLETE and cyc.outcome == "incomplete" and cyc.release == {}
    assert sum(c[0] == "build" for c in calls) == 3 and not any(c[0] == "export" for c in calls)  # builds went on
    assert hf.uploads == [] and cyc.warnings == []
    assert cyc.errors == [("no releasable build (build b7 short on acquire's retry tail over its limit: 20000 rows "
                           "due, 27.42% of the 72935 rows acquire covers (scope.fetch; STACK_SCHED_TAIL_MAX_PCT 5); 2 "
                           "builds ran); last build: incomplete: acquire (time); the next cycle resumes")]
    assert mailbox[-1]["subject"].startswith("ERROR: ")
    for k, (due, covered, pct, released) in enumerate([(50, 1000, None, True), (51, 1000, None, False),
                                                       (20000, 72935, "30", True), (400, None, None, False)]):
        if pct is None:
            monkeypatch.delenv("STACK_SCHED_TAIL_MAX_PCT", raising=False)
        else:
            monkeypatch.setenv("STACK_SCHED_TAIL_MAX_PCT", pct)
        monkeypatch.setenv("STACK_SCHED_RELEASE_ID", f"2026-12-{10 + k}")
        _f, calls = _canned_cycle(monkeypatch, tmp_path / str(k), [("ok", []), ("incomplete", tail),
                                                                   ("incomplete", tail)],
                                  runs=_tail_runs(due=due, covered=covered))
        cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
        assert cyc.run() == (sr.EXIT_OK if released else sr.EXIT_INCOMPLETE), (due, covered, pct, cyc.errors)
        assert any(c[0] == "export" for c in calls) is released
    monkeypatch.setenv("STACK_SCHED_TAIL_MAX_PCT", "101")
    with pytest.raises(SystemExit, match="STACK_SCHED_TAIL_MAX_PCT"):
        sr.Settings.from_env()


class _Child:
    """A running `stack` child for the stop tests: never exits, records its signals."""

    def __init__(self) -> None:
        self.signals: list[int] = []

    def poll(self) -> None:
        return None

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)


def test_a_stop_signals_the_running_child_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first stop drains the running child; the watchdog's later requests (the cap still reached every
    watch_s) do not signal again, so the cancel comes after the drain grace and a release export started after the
    stop is never signalled (before: one SIGTERM per cap check, a cancel after 10 min and a release cut short)."""
    build, export = _Child(), _Child()
    monkeypatch.setattr(sr.children, "stop_reason", None)
    monkeypatch.setattr(sr.children, "current", build)
    monkeypatch.setattr(sr.children, "terms", 0)
    sr.children.request_stop("wall 20.0 h >= STACK_SCHED_MAX_WALL_H 20.0")
    sr.children.request_stop("wall 20.2 h >= STACK_SCHED_MAX_WALL_H 20.0")
    assert build.signals == [sr.signal.SIGTERM] and sr.children.terms == 1
    sr.children.current, sr.children.terms = export, 0
    sr.children.request_stop("wall 20.4 h >= STACK_SCHED_MAX_WALL_H 20.0")
    assert export.signals == [] and sr.children.stop_reason.startswith("wall 20.0 h")


def test_the_watchdog_gives_a_child_started_after_the_stop_its_own_clock(
        env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """After a stop the watchdog cancels the drained child after the drain grace and exits when a child is still
    running 2 x the grace after the stop. A child started after the stop (the tail build's release export) gets that
    clock from its own start: with the stop's clock, an export starting 40 min after the stop had 20 min."""
    _canned_cycle(monkeypatch, tmp_path, [])
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    grace = cyc.s.drain_grace_s
    build, export = _Child(), _Child()
    clock = {"t": 0.0}

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

    class Done:
        def wait(self, s: float) -> bool:
            clock["t"] += s
            t = clock["t"]
            # the build drains from t=0, is cancelled after the grace, ends at grace + 5 min; the export starts at
            # grace + 10 min and is still running when the test ends
            sr.children.current = build if t < grace + 300 else None if t < grace + 600 else export
            if sr.children.current is export and not started:
                started.append(t)
                sr.children.terms = 0  # run_stack's reset for a new child
            return t > grace + 600 + 2 * grace + 120

    class Exited(Exception):
        pass

    started: list[float] = []
    exits: list[float] = []

    def exit_(_code: int) -> None:
        exits.append(clock["t"])
        raise Exited

    monkeypatch.setattr(sr, "time", FakeTime)
    monkeypatch.setattr(sr.os, "_exit", exit_)
    monkeypatch.setattr(cyc, "finish", lambda write=True: None)
    monkeypatch.setattr(cyc, "held", lambda: None)
    monkeypatch.setattr(cyc, "over_caps", lambda *_a: None)
    monkeypatch.setattr(sr, "spend_since", lambda *_a: 0.0)
    monkeypatch.setattr(sr.children, "stop_reason", "wall 20.0 h >= STACK_SCHED_MAX_WALL_H 20.0")
    monkeypatch.setattr(sr.children, "current", build)
    monkeypatch.setattr(sr.children, "terms", 1)  # drained at the stop

    class FakeLock:
        def heartbeat(self) -> bool:
            return True

    with pytest.raises(Exited):
        cyc.watchdog(FakeLock(), Done())  # type: ignore[arg-type]
    assert build.signals == [sr.signal.SIGTERM]  # the cancel after the grace
    assert export.signals == []
    assert len(exits) == 1 and exits[0] > started[0] + 2 * grace and exits[0] <= started[0] + 2 * grace + 60


def test_test_cycle_publishes_nothing_and_keeps_to_test_keys(hf: Any, monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    fake, calls = _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("ok", [])])
    cyc = sr.Cycle(sr.Settings.from_env(test=True), dry=False)
    assert (cyc.s.max_builds, cyc.s.max_usd, cyc.s.max_wall_h, cyc.s.openalex_full) == (1, 20.0, 6.0, "0")
    assert cyc.run() == sr.EXIT_OK
    assert hf.uploads == []
    keys = {k for _b, k in fake.objects}
    assert any(k.startswith("stack-store-ops/test/releases/") for k in keys)
    assert "stack-store-ops/test/cycles/latest.json" in keys and "stack-store-ops/cycles/latest.json" not in keys
    assert not any(k.startswith("stack-store-ops/releases/") for k in keys)


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Resend configured; urlopen captured (no network)."""
    import json as _json
    import urllib.request

    sent: list[dict[str, Any]] = []

    class Resp:
        status = 200

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    def urlopen(req: Any, timeout: float) -> Any:
        assert req.full_url == "https://api.resend.com/emails" and req.get_header("Authorization") == "Bearer re-test"
        assert req.get_header("User-agent")
        sent.append(_json.loads(req.data))
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    for k, v in {"STACK_SCHED_ALERT": "resend", "RESEND_API_KEY": "re-test", "STACK_SCHED_ALERT_TO": "a@x.org, b@x.org",
                 "STACK_SCHED_ALERT_FROM": "Stack <alerts@x.org>"}.items():
        monkeypatch.setenv(k, v)
    return sent


def test_failed_cycle_mails_an_alert_and_ok_one_does_not(hf: Any, mailbox: list[dict[str, Any]],
                                                        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hf.private["org/internal"] = False  # the private repo is public: fail and alert
    _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("ok", [])])
    assert sr.Cycle(sr.Settings.from_env(), dry=False).run() == sr.EXIT_FAILED
    assert len(mailbox) == 1 and mailbox[0]["to"] == ["a@x.org", "b@x.org"]
    assert mailbox[0]["subject"].startswith("ERROR: Stack weekly cycle upload_failed")
    assert "is not private" in mailbox[0]["text"] and "ERRORS (1):" in mailbox[0]["text"]
    hf.private["org/internal"] = True
    _canned_cycle(monkeypatch, tmp_path / "2", [("ok", []), ("ok", [])])
    assert sr.Cycle(sr.Settings.from_env(), dry=False).run() == sr.EXIT_OK
    assert len(mailbox) == 2 and mailbox[1]["subject"].startswith("OK: Stack weekly cycle ok")  # one mail per cycle
    # short only on collectors: released, WARN; an unexpected reason in between: WARN, retried
    _canned_cycle(monkeypatch, tmp_path / "3", [
        ("ok", []), ("incomplete", [{"stage": "triage", "run_id": "t", "reasons": ["unspecified"]}]),
        ("incomplete", [{"stage": "collect.cards", "run_id": "c", "reasons": ["limit"]}])])
    monkeypatch.setenv("STACK_SCHED_RELEASE_ID", "2026-12-25")
    assert sr.Cycle(sr.Settings.from_env(), dry=False).run() == sr.EXIT_OK
    assert mailbox[2]["subject"].startswith("WARN: ") and "WARNINGS (2):" in mailbox[2]["text"]
    assert "unexpected incomplete: triage (unspecified)" in mailbox[2]["text"]


def test_test_mode_mail_is_tagged_and_balances_shape_severity(hf: Any, mailbox: list[dict[str, Any]],
                                                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _canned_cycle(monkeypatch, tmp_path, [("ok", []), ("ok", [])])
    monkeypatch.setattr(sr, "check_balances", lambda _s: [("openalex", "WARN", "$1.20 left")])
    assert sr.Cycle(sr.Settings.from_env(test=True), dry=False).run() == sr.EXIT_OK
    assert mailbox[-1]["subject"].startswith("[test] WARN: ")
    _canned_cycle(monkeypatch, tmp_path / "2", [("ok", []), ("ok", [])])
    monkeypatch.setenv("STACK_SCHED_RELEASE_ID", "2026-12-26")
    monkeypatch.setattr(sr, "check_balances", lambda _s: [("openrouter", "ERROR", "$0.00 left")])
    assert sr.Cycle(sr.Settings.from_env(test=True), dry=False).run() == sr.EXIT_OK
    assert mailbox[-1]["subject"].startswith("[test] ERROR: ")


def test_check_balances_reads_openrouter_credits_and_openalex_headers(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "or-test")
    monkeypatch.setenv("OPENALEX_API_KEY", "oa-test")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-test")
    monkeypatch.setenv("STACK_SCHED_BALANCE_WARN_OPENALEX_USD", "5")
    monkeypatch.setenv("STACK_SCHED_BALANCE_WARN_OPENROUTER_USD", "20")

    def get(url: str, headers: dict[str, str]) -> Any:
        if url == sr.OPENROUTER_CREDITS:
            assert headers["Authorization"] == "Bearer or-test"
            return {"data": {"total_credits": 100.0, "total_usage": 70.5}}, {}
        assert url == sr.OPENALEX_PROBE and headers["Authorization"] == "Bearer oa-test"
        return {}, {"x-ratelimit-prepaid-remaining-usd": "3.10", "x-ratelimit-remaining-usd": "0.95"}

    monkeypatch.setattr(sr, "_get_json", get)
    got = {svc: (sev, msg) for svc, sev, msg in sr.check_balances(sr.Settings.from_env())}
    assert got["openrouter"][0] == "OK" and "$29.50 left" in got["openrouter"][1]
    assert got["openalex"][0] == "WARN" and "$3.10 left" in got["openalex"][1]
    assert got["openai"][0] == "SKIP"

    def gone(url: str, headers: dict[str, str]) -> Any:
        if url == sr.OPENROUTER_CREDITS:
            return {"data": {"total_credits": 10.0, "total_usage": 10.0}}, {}
        raise OSError("down")

    monkeypatch.setattr(sr, "_get_json", gone)
    got = {svc: (sev, msg) for svc, sev, msg in sr.check_balances(sr.Settings.from_env())}
    assert got["openrouter"][0] == "ERROR" and got["openalex"] == ("WARN", "rate-limit header check failed (OSError)")
    # without a management key: the API key's own limit, or SKIP when it has none
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-api")
    lim: dict[str, Any] = {"limit_remaining": 12.0}
    monkeypatch.setattr(sr, "_get_json", lambda url, h: ({"data": lim}, {}) if url == sr.OPENROUTER_KEY else ({}, {}))
    got = {svc: (sev, msg) for svc, sev, msg in sr.check_balances(sr.Settings.from_env())}
    assert got["openrouter"][0] == "WARN" and "key's own limit" in got["openrouter"][1]
    lim["limit_remaining"] = None
    assert dict((v, s_) for v, s_, _m in sr.check_balances(sr.Settings.from_env()))["openrouter"] == "SKIP"


def test_watchdog_mails_when_the_last_cycle_is_stale_or_error(env: None, mailbox: list[dict[str, Any]],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    real_ops = sr.Ops
    monkeypatch.setattr(sr, "Ops", lambda uri, *, read_only, client=None: real_ops(uri, read_only=False, client=fake))
    s = sr.Settings.from_env()
    ops = sr.Ops(OPS, read_only=False)
    now = sr.utcnow()
    assert sr.watchdog(s, now=now) == sr.EXIT_FAILED and "no production cycle summary" in mailbox[-1]["text"]
    ops.put_json("cycles/latest.json", {"cycle": "c1", "severity": "WARN", "outcome": "ok",
                                        "finished_at": (now - sr.dt.timedelta(hours=34)).isoformat()})
    n = len(mailbox)
    assert sr.watchdog(s, now=now) == sr.EXIT_OK and len(mailbox) == n  # silent when recent and OK/WARN
    assert sr.watchdog(s, now=now + sr.dt.timedelta(days=7)) == sr.EXIT_FAILED
    assert "finished 202 h ago" in mailbox[-1]["text"] and mailbox[-1]["subject"] == "ERROR: Stack weekly cycle watchdog"
    ops.put_json("cycles/latest.json", {"cycle": "c2", "severity": "ERROR", "outcome": "halted", "errors": ["x"],
                                        "finished_at": now.isoformat()})
    assert sr.watchdog(s, now=now) == sr.EXIT_FAILED and "ended ERROR: halted; x" in mailbox[-1]["text"]


def test_a_crash_mails_an_alert_then_propagates(env: None, mailbox: list[dict[str, Any]],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: str) -> Any:
        raise RuntimeError("plan unreadable")

    monkeypatch.setattr(sr, "plan_info", boom)
    with pytest.raises(RuntimeError):
        sr.main(["cycle", "--test"])
    assert len(mailbox) == 1 and mailbox[0]["subject"].startswith("[test] ERROR: Stack weekly cycle CRASHED: RuntimeError")


def test_alert_send_failure_is_logged_not_raised(env: None, mailbox: list[dict[str, Any]],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    def down(*_a: Any, **_k: Any) -> Any:
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert sr.send_email("s", "t") is False
    monkeypatch.delenv("STACK_SCHED_ALERT_TO")
    assert sr.alert_channel() is None and sr.send_email("s", "t") is False


def test_llm_keys_follow_the_pins_roles_to_their_providers() -> None:
    """The production plan's OpenAI-direct roles (D714) need OPENAI_API_KEY beside OpenRouter's key."""
    keys = sr.plan_keys("configs/plans/batch-2026-10a.yaml")
    assert keys["OPENAI_API_KEY"] == ["annotate.model_roles", "annotate.technique_staged"]
    assert "annotate.screen" in keys["OPENROUTER_API_KEY"] and "triage.safety-relevance-v4" in keys["OPENROUTER_API_KEY"]
    from stack import config

    llm = config.load().llm
    assert sr.llm_keys({"annotate.affiliation": {"name": "a"}}, llm) == {"OPENROUTER_API_KEY": ["annotate.affiliation"]}
    assert sr.llm_keys({}, llm) == {}


def test_preflight_refuses_a_missing_openai_key(env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in ("OPENROUTER_API_KEY", "STACK_MAILTO"):
        monkeypatch.setenv(k, "set")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("STACK_MIRROR", str(tmp_path))
    monkeypatch.setenv("STACK_SCHED_MIN_FREE_GB", "0")
    refuse, _ = sr.preflight(sr.Settings.from_env(), "d" * 64)
    assert [r for r in refuse if "LLM key" in r] == [
        "missing LLM key OPENAI_API_KEY (needed by the plan's annotate.model_roles, annotate.technique_staged)"]
    monkeypatch.setenv("OPENAI_API_KEY", "set")
    assert not [r for r in sr.preflight(sr.Settings.from_env(), "d" * 64)[0] if "LLM key" in r]


def test_llm_keys_read_qualified_model_refs_and_always_need_the_default_key() -> None:
    """D722: a pin's model reference with a provider qualifier needs that provider's key; open_client still refuses
    any LLM stage without the default provider's key, so every pinned stage needs OPENROUTER_API_KEY too."""
    from stack import config

    llm = config.load().llm
    got = sr.llm_keys({"annotate.screen": {"name": "s", "adjudicate_model": "anthropic:claude-opus-5-5"},
                       "annotate.model_roles": {"name": "m", "role": "extract_main_openai"}}, llm)
    assert got == {"ANTHROPIC_API_KEY": ["annotate.screen"],
                   "OPENAI_API_KEY": ["annotate.model_roles"],
                   "OPENROUTER_API_KEY": ["annotate.model_roles", "annotate.screen"]}


# ---------------------------------------------------------------- the image installs what the code imports (D732)
#: Imported by code the image never runs on its default install: docling (the `convert` group, docling-v2 only),
#: polars (stack/annotations.py's ann_expr, "not a dependency"), playwright (the headless rung, off in every plan),
#: referencing (tools/check_configs.py, a dev tool).
IMAGE_OPTIONAL = {"docling", "docling_core", "polars", "playwright", "referencing"}
#: The repository's own top-level packages; ``ladder`` is stack/conversion/ladder.py, imported by the convert worker
#: as a sibling module.
LOCAL = {"stack", "workflows", "tools", "spec", "ladder"}


def _image_closure(root: Path) -> set[str]:
    """The distributions the Dockerfile's ``uv sync --frozen --no-dev --group ...`` installs, from uv.lock: the
    project's dependencies and those groups, followed through each package's dependencies and requested extras
    (markers ignored: a superset)."""
    import tomllib

    groups = re.findall(r"--group (\S+)", (root / "Dockerfile").read_text("utf-8").split("COPY . .", 1)[1])
    assert groups, "the Dockerfile's uv sync names no group"
    pkgs = {p["name"]: p for p in tomllib.loads((root / "uv.lock").read_text("utf-8"))["package"]}
    me = pkgs["stack-pipeline"]
    todo = [*me["dependencies"], *(d for g in groups for d in me["dev-dependencies"][g])]
    seen: set[str] = set()
    while todo:
        dep = todo.pop()
        p = pkgs[dep["name"]]
        extras = dep.get("extra", [])
        key = f"{dep['name']}[{','.join(sorted(extras))}]"
        if key in seen:
            continue
        seen.add(key)
        todo += p.get("dependencies", [])
        todo += [d for e in extras for d in p.get("optional-dependencies", {}).get(e, [])]
    return {k.split("[", 1)[0] for k in seen}


def test_the_image_installs_every_module_the_code_imports() -> None:
    """Every third-party module imported anywhere under stack/ and workflows/ (lazy imports too) or by
    tools/scheduled_run.py comes with the image's install, unless :data:`IMAGE_OPTIONAL`. Red before D732:
    stack/fold/dataset.py's bundle checks import jsonschema, a dev-group package, and the image's export failed on
    it once duckdb could spill."""
    import ast
    from importlib.metadata import packages_distributions

    root = Path(__file__).parents[1]
    files = [*root.joinpath("stack").rglob("*.py"), *root.joinpath("workflows").rglob("*.py"),
             root / "tools" / "scheduled_run.py", root / "tools" / "memlog.py"]
    mods: set[str] = set()
    for f in files:
        for n in ast.walk(ast.parse(f.read_text("utf-8"))):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods.add(n.module.split(".")[0])
    third = mods - set(sys.stdlib_module_names) - LOCAL - IMAGE_OPTIONAL
    dists = packages_distributions()
    norm = {re.sub(r"[-_.]+", "-", d).lower() for d in _image_closure(root)}
    # a module of a group this venv did not sync (release: huggingface_hub) is looked up by its own name
    missing = {m: dists.get(m) for m in third
               if not {re.sub(r"[-_.]+", "-", d).lower() for d in dists.get(m) or [m]} & norm}
    assert missing == {}, f"imported but not installed in the image (Dockerfile groups, uv.lock): {missing}"


def test_run_stack_keeps_but_does_not_echo_json_stdout(monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    # A build's --json report is 1000+ lines at once, over Railway's log rate: kept and parsed, not echoed.
    script = "import sys; print('{\"status\": \"ok\"}'); print('progress line', file=sys.stderr)"
    monkeypatch.setattr(sr, "stack_cmd", lambda: [sys.executable, "-c", script])
    r = sr.run_stack(["build", "plan", "--json"], track=False)
    assert r.rc == 0 and r.json_doc() == {"status": "ok"}
    printed = capsys.readouterr().out
    assert "progress line" in printed and '"status"' not in printed
    sr.run_stack(["status"], track=False)
    assert '"status"' in capsys.readouterr().out


def test_failed_stack_status_shows_its_masked_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # The scheduler's own failure keeps its (masked) output lines; a provider error stays redacted to its class.
    script = "import sys; print('boom at https://example.invalid/x', file=sys.stderr); sys.exit(2)"
    monkeypatch.setattr(sr, "stack_cmd", lambda: [sys.executable, "-c", script])
    with pytest.raises(sr.StackFailed) as ei:
        sr.status_json()
    assert sr.error_text(ei.value) == "stack status failed rc=2: boom at <url>"
    assert sr.error_text(RuntimeError("https://example.invalid/secret")) == "RuntimeError"


def test_mem_note_reads_the_cgroup_split_and_the_childs_rss(tmp_path: Path) -> None:
    """D782: the LIVE line's memory note: cgroup usage, peak, anon / file / shmem, tasks of their limit (D786), and the
    child's VmRSS / VmHWM and threads."""
    g = 2**30
    (tmp_path / "memory.current").write_text(f"{30 * g}\n")
    (tmp_path / "memory.peak").write_text(f"{32 * g}\n")
    (tmp_path / "memory.stat").write_text(f"anon {9 * g}\nfile {20 * g}\nkernel 1\nshmem 0\n")
    (tmp_path / "memory.swap.current").write_text("0\n")
    (tmp_path / "memory.swap.max").write_text("max\n")
    (tmp_path / "pids.current").write_text("812\n")
    (tmp_path / "pids.max").write_text("1000\n")
    note = sr.mem_note(sr.os.getpid(), cgroup=tmp_path)
    assert note.startswith(", mem cgroup 30.0 GB (anon 9.0, file 20.0, shmem 0.0); peak 32.0 GB; swap 0.0 GB of max; "
                           "pids 812 of 1000; child rss ")  # D786: the container's tasks of its limit
    (tmp_path / "memory.swap.current").unlink()  # no swap accounting: no swap part
    assert re.search(r"child rss \d+\.\d GB, hwm \d+\.\d GB, threads \d+$", note)
    (tmp_path / "pids.current").unlink()  # no pids controller: no pids part
    (tmp_path / "memory.peak").unlink()  # a kernel without memory.peak: the rest stays
    assert sr.mem_note(None, cgroup=tmp_path) == ", mem cgroup 30.0 GB (anon 9.0, file 20.0, shmem 0.0)"
    assert sr.mem_note(None, cgroup=tmp_path / "absent") == ""  # nothing readable (not Linux, cgroup v1): no note
    assert sr.mem_note(2**22 + 7, cgroup=tmp_path / "absent") == ""  # the child already exited


@pytest.mark.parametrize("ending", ["ok", "halt", "crash", "signal"])
def test_the_memory_log_runs_with_the_cycle_and_ends_with_it(ending: str, hf: Any, mailbox: list[dict[str, Any]],
                                                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                              capfd: pytest.CaptureFixture[str]) -> None:
    """D783: the cycle starts tools/memlog.py when it starts; it is stopped on every way out (an ok cycle, a halt, an
    exception, a signal's stop) and the scheduler's SIGTERMs never reach it. D784: its [mem] lines are on stdout once
    each, in the cycle's log and its log.txt, and not in the email's log tail."""
    import time as _time

    monkeypatch.setenv("STACK_SCHED_MEMLOG_S", "0.2")
    builds = [("ok", []), ("failed", [])] if ending == "halt" else [("ok", []), ("ok", [])]
    fake, _calls = _canned_cycle(monkeypatch, tmp_path, builds)
    started: list[Any] = []
    real_start, canned = sr.start_memlog, sr.run_stack
    monkeypatch.setattr(sr, "start_memlog", lambda s: started.append(real_start(s)) or started[-1])
    monkeypatch.setattr(sr, "log", sr.Log())
    out: list[str] = []

    def stack(args: list[str], **kw: Any) -> Any:
        if len(out) == 0:  # the first stack command waits for the memory log's first line
            deadline = _time.monotonic() + 20
            while "[mem]" not in "".join(out) and _time.monotonic() < deadline:
                out.append(capfd.readouterr().out)
                _time.sleep(0.05)
            p = started[0].proc
            assert p is not None and p.poll() is None
            if ending == "signal":
                sr.children.request_stop("signal SIGTERM")  # what install_signals' handler does
        if ending == "crash" and args[0] == "build":
            raise RuntimeError("boom")
        return canned(args, **kw)

    monkeypatch.setattr(sr, "run_stack", stack)
    monkeypatch.setattr(sr.children, "stop_reason", None)
    cyc = sr.Cycle(sr.Settings.from_env(), dry=False)
    code = cyc.run()
    assert code == {"ok": sr.EXIT_OK, "halt": sr.EXIT_FAILED, "crash": sr.EXIT_FAILED, "signal": sr.EXIT_INCOMPLETE}[
        ending], cyc.outcome
    m = started[0]
    assert m.proc.returncode == -sr.signal.SIGTERM  # stopped by the cycle, reaped
    assert not m.reader.is_alive()
    assert cyc.memlog is None
    printed = "".join(out) + capfd.readouterr().out
    assert re.search(r"Z \[mem\] .* \| \S", printed)
    assert "WARNING: the memory log" not in printed and "memory log not" not in printed
    mem = [ln for ln in sr.log.lines if re.match(r"\S+Z \[mem\] ", ln)]
    assert mem and all(printed.count(ln + "\n") == 1 for ln in mem)  # in the log, and on stdout exactly once
    log_txt = fake.objects[("stack-data", f"stack-store-ops/cycles/{cyc.id}/log.txt")].decode()
    assert "[mem]" in log_txt and "[sched]" in log_txt
    if ending in ("halt", "crash"):  # an ERROR mail carries the cycle's log tail, without the memory log's lines
        mail = mailbox[-1]["text"]
        assert "the cycle's own log (last 40 lines):" in mail and "[sched]" in mail
        assert "[mem]" not in mail


def test_log_tail_leaves_out_the_memory_log(capsys: pytest.CaptureFixture[str]) -> None:
    """D784: a memory log line is kept and printed as it is (no second stamp or tag) and left out of the mails'
    tail; the cycle's own lines are counted from the end as before."""
    lg = sr.Log()
    lg("one")
    lg.verbatim("2026-11-01T00:00:00Z [mem] cg 1.0/2.0G | sched 0.1G")
    lg("two")
    lg.verbatim("2026-11-01T00:03:00Z [mem] cg 1.1/2.0G | sched 0.1G")
    assert len(lg.lines) == 4 and lg.lines[1] == "2026-11-01T00:00:00Z [mem] cg 1.0/2.0G | sched 0.1G"
    assert [ln.split(" [sched] ")[1] for ln in lg.tail(40)] == ["one", "two"]
    assert [ln.split(" [sched] ")[1] for ln in lg.tail(1)] == ["two"]
    assert capsys.readouterr().out.count("[mem]") == 2


def test_the_memory_log_stop_drains_its_last_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                   capfd: pytest.CaptureFixture[str]) -> None:
    """D784: stop_memlog ends the process and returns only after its last lines (here one written on SIGTERM) are
    in the log; on stdout each once. Red without the reader's join in stop_memlog."""
    import time as _time

    fake_log = tmp_path / "fake_memlog.py"
    fake_log.write_text(
        "import signal, sys, time\n"
        "def bye(*_a):\n"
        "    print('2026-11-01T00:09:00Z [mem] last', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "for i in range(3):\n"
        "    print(f'2026-11-01T00:0{i}:00Z [mem] line {i}', flush=True)\n"
        "time.sleep(60)\n")
    monkeypatch.setattr(sr, "MEMLOG", fake_log)
    lg = sr.Log()
    monkeypatch.setattr(sr, "log", lg)
    verbatim = lg.verbatim
    monkeypatch.setattr(lg, "verbatim", lambda ln: (_time.sleep(0.3), verbatim(ln)))  # a slow reader
    m = sr.start_memlog(1)
    assert m is not None
    deadline = _time.monotonic() + 20
    while len(sr.log.lines) < 3 and _time.monotonic() < deadline:
        _time.sleep(0.05)
    t0 = _time.monotonic()
    sr.stop_memlog(m)
    assert _time.monotonic() - t0 < 8
    assert m.proc.returncode == 0 and not m.reader.is_alive()
    assert sr.log.lines == [f"2026-11-01T00:0{i}:00Z [mem] line {i}" for i in range(3)] + [
        "2026-11-01T00:09:00Z [mem] last"]
    out = capfd.readouterr().out
    assert all(out.count(ln + "\n") == 1 for ln in sr.log.lines)
    assert sr.log.tail(60) == []


def test_the_memory_log_off_unstartable_or_crashed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """D783: interval 0 starts nothing; a log that cannot start or that died is one WARNING line, never an error."""
    assert sr.start_memlog(0) is None
    monkeypatch.setattr(sr.sys, "executable", str(tmp_path / "no-python"))
    assert sr.start_memlog(1) is None
    assert "WARNING: memory log not started: FileNotFoundError" in capsys.readouterr().out
    monkeypatch.undo()
    crash = tmp_path / "crash.py"
    crash.write_text("import sys; sys.exit(3)\n")
    monkeypatch.setattr(sr, "MEMLOG", crash)
    m = sr.start_memlog(1)
    assert m is not None and m.proc.wait(timeout=30) == 3
    sr.stop_memlog(m)
    assert not m.reader.is_alive()
    assert "WARNING: the memory log had exited early (rc=3)" in capsys.readouterr().out
    sr.stop_memlog(None)


def test_the_memory_log_setting(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACK_SCHED_MEMLOG_S")
    assert sr.Settings.from_env().memlog_s == 180
    monkeypatch.setenv("STACK_SCHED_MEMLOG_S", "0")
    assert sr.Settings.from_env().memlog_s == 0
