# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=16", "jsonschema>=4.21", "pyyaml>=6", "pytest>=8"]
# ///
"""Tests of tools/release-validator.py on a small synthetic release (its own four schemas, in the shape of format 1):
the valid release passes; each negative fixture fails with the check it breaks; the fast row check agrees with
jsonschema; the archive helpers (strip, canonical compare, --formats, the edit check against git).

    uv run tools/test_release_validator.py
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

TOOLS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("release_validator", TOOLS / "release-validator.py")
assert _spec is not None and _spec.loader is not None
rv = importlib.util.module_from_spec(_spec)
sys.modules["release_validator"] = rv
_spec.loader.exec_module(rv)

TECHNIQUES = TOOLS.parent / "taxonomies" / "techniques" / "techniques-1.1.0.yaml"
URN = "urn:the-safety-stack:format:1.0:"
S2020 = "https://json-schema.org/draft/2020-12/schema"
AT = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


def _s(t: str | list[str], arrow: str, **kw: Any) -> dict[str, Any]:
    return {"type": t, "x-arrow": arrow, **kw}


def _obj(props: dict[str, Any], nullable: bool = False, arrow: str = "struct") -> dict[str, Any]:
    return {"type": ["object", "null"] if nullable else "object", "x-arrow": arrow, "additionalProperties": False,
            "required": list(props), "properties": props}


META = _obj({"config_id": _s("string", "string"), "override_ids": _s("array", "list", items=_s("string", "string")),
             "decided_at": _s(["string", "null"], "timestamp[us, tz=UTC]", format="date-time")})
CORPUS = {"$schema": S2020, "$id": URN + "data/corpus.schema.json", "title": "one row", **_obj({
    "doc_id": _s("string", "string", description="Document id."),
    "published_date": _s(["string", "null"], "date32[day]", format="date"),
    "identity": _obj({"override_ids": _s("array", "list", items=_s("string", "string")),
                      "members": _s("array", "list", items=_obj({"override_id": _s(["string", "null"], "string")}))}),
    "annotations": _obj({
        "screen": _obj({"metadata": META, "payload": _obj({"verdict": _s(
            ["string", "null"], "string", enum=["relevant", "irrelevant", None]),
            "buckets": _s(["array", "null"], "list", items=_s("string", "string"), **{"x-open": True,
                                                                                      "x-values": ["paper"]})},
            nullable=True)}, nullable=True),
        "safety_techniques": _obj({"metadata": META, "payload": _obj({
            "taxonomy_version": _s(["string", "null"], "string"),
            "techniques": _s(["array", "null"], "list", items=_obj({"slug": _s("string", "string")})),
            "extra_techniques": _s(["array", "null"], "list", items=_obj({
                "hint_technique": _s(["string", "null"], "string")}))}, nullable=True)}, nullable=True),
        "other": _s("array", "list", items=_obj({"metadata": META, "payload": _s("string", "string")}))}),
})}
CONFIGS = {"$schema": S2020, "$id": URN + "config/configs.schema.json", "type": "array",
           "items": {"type": "object", "required": ["config_id", "taxonomy", "override_ids"],
                     "properties": {"config_id": {"type": "string"}, "override_ids": {"type": "array"},
                                    "taxonomy": {"type": ["object", "null"]}}}}
OVERRIDES = {"$schema": S2020, "$id": URN + "config/overrides.schema.json", "type": "object",
             "required": ["override_id", "kind", "target", "key"], "additionalProperties": False,
             "properties": {"override_id": {"type": "string"}, "target": {"type": "string"}, "key": {"type": "string"},
                            "kind": {"enum": ["scope_decision", "identity_merge", "metadata_fix"]}}}
CORE = ["data/corpus.parquet", "data/corpus.schema.json", "config/configs.json", "config/configs.schema.json",
        "config/overrides.jsonl", "config/overrides.schema.json", "release.schema.json"]
RELEASE = {"$schema": S2020, "$id": URN + "release.schema.json", "type": "object",
           "required": ["release_id", "format_version", "taxonomies", "files"],
           "properties": {"release_id": {"type": "string"},
                          "format_version": {"type": "string", "pattern": "^1\\.0\\.(0|[1-9][0-9]*)$"},
                          "taxonomies": {"type": "object"},
                          "files": {"type": "array", "items": {"type": "object", "required": ["path", "bytes", "sha256"]},
                                    "allOf": [{"contains": {"properties": {"path": {"const": p}}}} for p in CORE]}}}


def arrow(js: dict[str, Any]) -> pa.DataType:
    """The Arrow type of a schema node of the fixture (from its x-arrow)."""
    x = js["x-arrow"]
    if x == "struct":
        return pa.struct([pa.field(k, arrow(v), "null" in v["type"]) for k, v in js["properties"].items()])
    if x == "list":
        return pa.list_(pa.field("element", arrow(js["items"]), "null" in js["items"]["type"]))
    return {"string": pa.string(), "date32[day]": pa.date32(), "timestamp[us, tz=UTC]": pa.timestamp("us", "UTC")}[x]


CID = "screen:screen-v1:m:00000000"
TCID = "safety_techniques:t-v1:m:11111111"


def row(doc: str, **kw: Any) -> dict[str, Any]:
    meta = {"config_id": CID, "override_ids": [], "decided_at": AT}
    r = {"doc_id": doc, "published_date": dt.date(2025, 1, 2),
         "identity": {"override_ids": [], "members": [{"override_id": None}]},
         "annotations": {"screen": {"metadata": meta, "payload": {"verdict": "relevant", "buckets": ["paper"]}},
                         "safety_techniques": {"metadata": {**meta, "config_id": TCID},
                                               "payload": {"taxonomy_version": "1.1.0",
                                                           "techniques": [{"slug": "rlhf"}],
                                                           "extra_techniques": [{"hint_technique": None}]}},
                         "other": []}}
    r.update(kw)
    return r


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(root: Path, rows: list[dict[str, Any]] | None = None, *, configs: list[dict[str, Any]] | None = None,
          overrides: list[dict[str, Any]] | None = None, version: str | None = "1.0.0",
          corpus_schema: dict[str, Any] | None = None, release_schema: dict[str, Any] | None = None) -> Path:
    """A release directory (every file listed in release.json with its sha256)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir()
    (root / "config").mkdir()
    (root / "taxonomies").mkdir()
    cs = corpus_schema or CORPUS
    pq.write_table(pa.Table.from_pylist(rows if rows is not None else [row("sdoc000000001"), row("sdoc000000002")],
                                        schema=pa.schema(list(arrow(cs)))), root / "data" / "corpus.parquet")
    for rel, js in (("data/corpus.schema.json", cs), ("config/configs.schema.json", CONFIGS),
                    ("config/overrides.schema.json", OVERRIDES), ("release.schema.json", release_schema or RELEASE)):
        (root / rel).write_text(json.dumps(js, indent=2))
    tax = {"name": "techniques", "version": "1.1.0", "path": "taxonomies/techniques-1.1.0.yaml",
           "sha256": sha(TECHNIQUES)}
    (root / "config" / "configs.json").write_text(json.dumps(configs if configs is not None else [
        {"config_id": CID, "taxonomy": None, "override_ids": []},
        {"config_id": TCID, "taxonomy": tax, "override_ids": []}]))
    (root / "config" / "overrides.jsonl").write_text("".join(json.dumps(o) + "\n" for o in (overrides or [])))
    shutil.copyfile(TECHNIQUES, root / tax["path"])
    files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)}
             for p in sorted(root.rglob("*")) if p.is_file()]
    doc: dict[str, Any] = {"release_id": "2026-11-03", "taxonomies": {"techniques": [
        {k: tax[k] for k in ("version", "sha256", "path")}]}, "files": files}
    if version is not None:
        doc["format_version"] = version
    (root / "release.json").write_text(json.dumps(doc))
    return root


def problems(rep: Any, check: str) -> list[str]:
    return [f"{p.where}: {p.message}" for p in rep.problems if p.check == check]


@pytest.fixture
def good(tmp_path: Path) -> Path:
    return write(tmp_path / "rel")


# ---------------------------------------------------------------- the valid release
def test_a_valid_release_passes(good: Path) -> None:
    rep = rv.check_release(good, rows="all", archive=False)
    assert rep.status == "valid", rep.text()
    assert rep.exit_code == rv.EXIT_OK and rep.rows_checked == 2 and rep.rows_mode == "all"
    assert rep.checks["taxonomies"] == "1 files pinned; 2 id values resolved"


def test_the_cli_exit_codes(good: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert rv.main([str(good), "--rows", "all"]) == rv.EXIT_OK
    assert capsys.readouterr().out.rstrip().endswith("VALID")
    with pytest.raises(SystemExit) as e:
        rv.main([str(good), "--rows", "some"])
    assert e.value.code == rv.EXIT_USAGE


def test_a_sample_is_seeded(tmp_path: Path) -> None:
    root = write(tmp_path / "r", [row(f"sdoc{i:09d}") for i in range(50)])
    a = rv.check_release(root, rows=10, seed=3, archive=False)
    assert a.status == "valid" and a.rows_checked == 10 and a.rows_mode == "a seeded sample of 10 (seed 3)"
    assert rv._sample(50, 10, 3) == rv._sample(50, 10, 3) != rv._sample(50, 10, 4)


# ---------------------------------------------------------------- negative fixtures
def test_a_corrupt_file(good: Path) -> None:
    p = good / "data" / "corpus.parquet"
    p.write_bytes(p.read_bytes()[: p.stat().st_size // 2])
    rep = rv.check_release(good, archive=False)
    assert rep.status == "invalid"
    assert "data/corpus.parquet: size or sha256 differs from release.json" in problems(rep, "manifest")
    assert any(w.startswith("(check stopped)") for w in problems(rep, "schemas"))


def test_a_bad_enum_value(tmp_path: Path) -> None:
    bad = row("sdoc000000002")
    bad["annotations"]["screen"]["payload"]["verdict"] = "maybe"
    rep = rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), bad]), archive=False)
    got = problems(rep, "rows")
    assert len(got) == 1 and got[0].startswith("data/corpus.parquet annotations.screen.payload.verdict: 1 row(s), "
                                               "e.g. doc_id sdoc000000002: 'maybe' is not one of"), got


def test_an_open_list_takes_a_new_value(tmp_path: Path) -> None:
    new = row("sdoc000000002")
    new["annotations"]["screen"]["payload"]["buckets"] = ["paper", "a_new_bucket"]
    assert rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), new]), archive=False).status == "valid"


def test_a_dangling_config_id(tmp_path: Path) -> None:
    bad = row("sdoc000000002")
    bad["annotations"]["screen"]["metadata"]["config_id"] = "screen:gone:m:99999999"
    rep = rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), bad]), archive=False)
    assert problems(rep, "ids") == ["annotations.<kind>.metadata.config_id: 'screen:gone:m:99999999' (1 records) is "
                                    "not in config/configs.json"]


def test_a_dangling_config_id_of_an_other_record(tmp_path: Path) -> None:
    bad = row("sdoc000000002")
    bad["annotations"]["other"] = [{"metadata": {"config_id": "x:y:z:00000000", "override_ids": [], "decided_at": AT},
                                    "payload": "{}"}]
    rep = rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), bad]), archive=False)
    assert problems(rep, "ids") == ["annotations.<kind>.metadata.config_id: 'x:y:z:00000000' (1 records) is not in "
                                    "config/configs.json"]


def test_a_duplicate_doc_id(tmp_path: Path) -> None:
    rep = rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), row("sdoc000000001")]), archive=False)
    assert problems(rep, "ids") == ["doc_id: 1 ids on more than one row, e.g. ['sdoc000000001']"]


def test_a_missing_listed_file(good: Path) -> None:
    (good / "config" / "overrides.jsonl").unlink()
    rep = rv.check_release(good, archive=False)
    assert "config/overrides.jsonl: listed in release.json, missing" in problems(rep, "manifest")


def test_an_unlisted_file(good: Path) -> None:
    (good / "notes.txt").write_text("x")
    (good / ".gitattributes").write_text("*.parquet filter=lfs")
    (good / ".cache").mkdir()
    (good / ".cache" / "x").write_text("x")
    rep = rv.check_release(good, archive=False)
    assert problems(rep, "manifest") == ["notes.txt: present but not listed in release.json"]
    assert rep.ignored == [".cache/", ".gitattributes"]


@pytest.mark.parametrize("version", ["2.0.0", None, "1.0"])
def test_an_unknown_version_is_unsupported_not_invalid(tmp_path: Path, version: str | None) -> None:
    # the release's own manifest schema fits its release.json (a release of another format ships its own)
    own = {**RELEASE, "required": ["files"], "properties": {**RELEASE["properties"], "format_version": {}}}
    root = write(tmp_path / "r", version=version, release_schema=own)
    rep = rv.check_release(root, archive=False)
    assert rep.status == "unsupported" and rep.exit_code == rv.EXIT_UNSUPPORTED
    assert "ids" not in rep.checks and rep.checks["manifest"] and rep.rows_checked == 2
    assert any("format-specific checks" in x for x in rep.not_checked)
    (root / "notes.txt").write_text("x")  # the format-independent checks still run
    assert problems(rv.check_release(root, archive=False), "manifest") == [
        "notes.txt: present but not listed in release.json"]


def test_an_unknown_minor_of_a_known_major_is_checked(tmp_path: Path) -> None:
    rep = rv.check_release(write(tmp_path / "r", version="1.7.0"), archive=False)
    assert rep.supported and problems(rep, "manifest") and "ids" in rep.checks  # the 1.0 pattern refuses 1.7


def test_a_footer_type_that_differs_from_x_arrow(tmp_path: Path) -> None:
    cs = copy.deepcopy(CORPUS)
    root = write(tmp_path / "r", corpus_schema=cs)
    cs["properties"]["published_date"]["x-arrow"] = "timestamp[us, tz=UTC]"
    (root / "data" / "corpus.schema.json").write_text(json.dumps(cs))
    got = problems(rv.check_release(root, archive=False), "schemas")
    assert "data/corpus.parquet footer published_date: x-arrow 'timestamp[us, tz=UTC]', the parquet footer has " \
           "'date32[day]'" in got


def test_overrides_ids_and_targets(tmp_path: Path) -> None:
    merged = row("sdoc000000002", identity={"override_ids": ["m1"], "members": [{"override_id": "m1"}]})
    merged["annotations"]["screen"]["metadata"]["override_ids"] = ["s1", "f1"]
    ovs = [{"override_id": "m1", "kind": "identity_merge", "target": "identity",
            "key": '["sdoc000000002","sdoc000000003"]'},
           {"override_id": "s1", "kind": "scope_decision", "target": "screen", "key": "sdoc000000002"},
           {"override_id": "x1", "kind": "scope_decision", "target": "screen", "key": "sgonegonegone"},  # absent doc
           {"override_id": "f1", "kind": "metadata_fix", "target": "fields", "key": "sdoc000000002"}]
    rep = rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), merged], overrides=ovs), archive=False)
    assert problems(rep, "ids") == ["annotations.screen: 1 override id reference(s) to a metadata_fix line of target "
                                    "'fields'"]
    ovs += [{"override_id": "s1", "kind": "scope_decision", "target": "nothing", "key": "not-a-doc"}]
    got = problems(rv.check_release(write(tmp_path / "r2", [row("sdoc000000001")], overrides=ovs), archive=False),
                   "ids")
    assert "config/overrides.jsonl: override id 's1' on more than one line" in got
    assert "config/overrides.jsonl s1: key 'not-a-doc' is not the shape kind 'scope_decision' takes" in got
    assert "config/overrides.jsonl s1: target 'nothing' is not an annotation kind of this release" in got


def test_an_unresolved_taxonomy_id(tmp_path: Path) -> None:
    bad = row("sdoc000000002")
    bad["annotations"]["safety_techniques"]["payload"]["techniques"] = [{"slug": "no-such-technique"}]
    bad["annotations"]["safety_techniques"]["payload"]["extra_techniques"] = [{"hint_technique": "rlhf"}]
    rep = rv.check_release(write(tmp_path / "r", [row("sdoc000000001"), bad]), archive=False)
    assert problems(rep, "taxonomies") == ["annotations.safety_techniques.payload.techniques[].slug: 'no-such-technique' "
                                           "is not an id of techniques 1.1.0: 1 value(s), e.g. doc_id sdoc000000002"]


# ---------------------------------------------------------------- the fast row check agrees with jsonschema
def _variants() -> list[dict[str, Any]]:
    out = [row("sdoc000000001")]
    edits = [("doc_id", 5), ("published_date", "2025-01-02"), ("published_date", AT), ("published_date", None),
             ("identity", None), ("annotations", {**row("x")["annotations"], "extra": 1})]
    for k, v in edits:
        out.append(row("sdoc000000001", **{k: v}))
    for meta in ({"decided_at": AT.replace(tzinfo=None)}, {"decided_at": dt.date(2025, 1, 1)},
                 {"decided_at": "2025-01-01T00:00:00Z"}, {"decided_at": "2025-13-01T00:00:00Z"},
                 {"config_id": None}, {"override_ids": [1]}):
        r = row("sdoc000000001")
        r["annotations"]["screen"]["metadata"].update(meta)
        out.append(r)
    for payload in ({"verdict": True}, {"verdict": None, "buckets": None}, {"verdict": "relevant"}):
        r = row("sdoc000000001")
        r["annotations"]["screen"]["payload"] = payload
        out.append(r)
    return out


def test_the_fast_check_agrees_with_jsonschema() -> None:
    fast, full = rv.fast_validator(CORPUS), rv.validator(CORPUS)
    assert fast is not None
    got = [(fast(r), full.is_valid(rv.to_json(r))) for r in _variants()]
    assert all(a == b for a, b in got), got
    # true: the base row, a date as a string, a NULL date, a date-time as a string, a NULL payload list
    assert [a for a, _b in got].count(True) == 5


def test_the_fast_check_declines_other_keywords() -> None:
    assert rv.fast_validator({"type": "string", "pattern": "^a"}) is None
    assert rv.fast_validator({"type": "object", "additionalProperties": {"type": "string"}}) is None


# ---------------------------------------------------------------- the archive helpers
def test_strip_keeps_property_names_and_data() -> None:
    s = {"title": "t", "description": "d", "properties": {"description": {"type": "string", "description": "x"},
                                                          "title": {"enum": [{"description": 1}]}},
         "items": {"$comment": "c", "examples": [1], "type": "integer"}}
    assert rv.strip_schema(s) == {"properties": {"description": {"type": "string"},
                                                 "title": {"enum": [{"description": 1}]}},
                                  "items": {"type": "integer"}}


def test_canonical_ignores_key_order_and_set_order() -> None:
    a = {"type": ["string", "null"], "required": ["a", "b"], "enum": ["x", "y"], "items": {"a": 1, "b": 2}}
    b = {"items": {"b": 2, "a": 1}, "enum": ["y", "x"], "required": ["b", "a"], "type": ["null", "string"]}
    assert rv.canonical(a) == rv.canonical(b)
    assert rv.canonical({"allOf": [1, 2]}) != rv.canonical({"allOf": [2, 1]})  # other lists keep their order
    assert rv.schema_diff(rv.canonical(a), rv.canonical({**b, "enum": ["x"]})) == ['/enum: ["x", "y"] -> ["x"]']


def _formats(root: Path, entry: str) -> Path:
    f = root / "formats"
    d = f / "1.0"
    for rel, js in (("data/corpus.schema.json", CORPUS), ("config/configs.schema.json", CONFIGS),
                    ("config/overrides.schema.json", OVERRIDES), ("release.schema.json", RELEASE)):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(json.dumps(rv.strip_schema(js), indent=2))
    (f / "CHANGELOG.md").write_text(f"# Changelog\n\n{entry}\n\n- First.\n")
    return f


def test_check_formats(tmp_path: Path) -> None:
    f = _formats(tmp_path, "## [1.0.0]")
    assert rv.check_formats(f) == []
    (f / "1.0" / "data" / "corpus.schema.json").write_text(json.dumps(CORPUS))  # not stripped
    (f / "1.0" / "extra.json").write_text("{}")
    (f / "1.1").mkdir()
    (f / "CHANGELOG.md").write_text("# Changelog\n")
    got = rv.check_formats(f)
    assert "formats/1.0: no entry [1.0.<patch>] in formats/CHANGELOG.md" in got
    assert "formats/1.0/data/corpus.schema.json: keeps description, title, examples or $comment" in got
    assert "formats/1.0/extra.json: not a schema file of the format" in got
    assert "formats/1.1/release.schema.json: missing" in got


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.mark.parametrize(("entry", "flagged"), [("## [1.0.0] - 2026-11-20", True), ("## [1.0.0]", False)])
def test_an_edit_to_a_dated_format_is_refused(tmp_path: Path, entry: str, flagged: bool) -> None:
    f = _formats(tmp_path, entry)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "formats")
    _git(tmp_path, "commit", "-q", "-m", "archive")
    p = f / "1.0" / "config" / "configs.schema.json"
    p.write_text(p.read_text().replace('"array"', '"object"', 1))
    _git(tmp_path, "commit", "-q", "-am", "edit")
    got = rv.check_formats(f, "HEAD~1")
    assert got == ([f"formats/1.0/config/configs.schema.json: changed since HEAD~1 (M), but format 1.0 was dated "
                    "there; an archived format is never edited"] if flagged else [])


def test_the_archive_compare_of_a_release(tmp_path: Path, good: Path) -> None:
    f = _formats(tmp_path, "## [1.0.0]")
    assert rv.check_release(good, archive=f).status == "valid"
    assert rv.check_release(good, archive=tmp_path / "none").status == "valid"  # not required: a check left out
    assert problems(rv.check_release(good, archive=tmp_path / "none", require_archive=True), "archive")
    js = json.loads((f / "1.0" / "config" / "overrides.schema.json").read_text())
    js["properties"]["kind"]["enum"].append("manual_source")
    (f / "1.0" / "config" / "overrides.schema.json").write_text(json.dumps(js))
    got = problems(rv.check_release(good, archive=f), "archive")
    assert len(got) == 1 and got[0].startswith("config/overrides.schema.json: differs from "
                                               "formats/1.0/config/overrides.schema.json: /properties/kind/enum")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
