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
    "release_id": _s("string", "string"),
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
                          "files": {"type": "array",
                                    "items": {"type": "object", "required": ["path", "bytes", "sha256"]},
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
    r = {"release_id": "2026-11-03", "doc_id": doc, "published_date": dt.date(2025, 1, 2),
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
          corpus_schema: dict[str, Any] | None = None, release_schema: dict[str, Any] | None = None,
          taxa: tuple[str, ...] = (), tweak: str | None = None, kv: dict[str, str] | None = None,
          card: str | None = None) -> Path:
    """A release directory (every file listed in release.json with its sha256); ``taxa``: more taxonomy files of
    this repository (``<name>-<version>``) pinned beside techniques 1.1.0; ``tweak``: a line appended to the shipped
    techniques file; ``kv``: the parquet key-value metadata (default: the version and release id); ``card``: the
    README.md (default: front matter with the version)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir()
    (root / "config").mkdir()
    (root / "taxonomies").mkdir()
    cs = corpus_schema or CORPUS
    meta = kv if kv is not None else {"format_version": version or "", "release_id": "2026-11-03"}
    pq.write_table(pa.Table.from_pylist(rows if rows is not None else [row("sdoc000000001"), row("sdoc000000002")],
                                        schema=pa.schema(list(arrow(cs)), metadata=meta)),
                   root / "data" / "corpus.parquet")
    (root / "README.md").write_text(card if card is not None else f"---\nformat_version: {version}\n---\n\n# Card\n")
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
    pins = {"techniques": [{k: tax[k] for k in ("version", "sha256", "path")}]}
    if tweak:
        with open(root / tax["path"], "a") as f:
            f.write(tweak)
        pins["techniques"][0]["sha256"] = sha(root / tax["path"])
    for t in taxa:
        name, ver = t.split("-", 1)
        src = TOOLS.parent / "taxonomies" / name / f"{t}.yaml"
        shutil.copyfile(src, root / "taxonomies" / f"{t}.yaml")
        pins.setdefault(name, []).append({"version": ver, "sha256": sha(src), "path": f"taxonomies/{t}.yaml"})
    files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)}
             for p in sorted(root.rglob("*")) if p.is_file()]
    doc: dict[str, Any] = {"release_id": "2026-11-03", "taxonomies": pins, "files": files}
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


def test_the_cli_exit_codes(good: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    none = tmp_path / "no-archive"  # the fixture's toy format is not this repository's archived 1.0
    assert rv.main([str(good), "--rows", "all", "--archive", str(none)]) == rv.EXIT_OK
    assert capsys.readouterr().out.rstrip().endswith("VALID")
    assert rv.main([str(good)]) == rv.EXIT_INVALID  # against formats/1.0/ of this repository
    assert "[archive] data/corpus.schema.json: differs from formats/1.0/data/corpus.schema.json" in \
        capsys.readouterr().out
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
    assert problems(rep, "taxonomies") == ["annotations.safety_techniques.payload.techniques[].slug: "
                                           "'no-such-technique' is not an id of techniques 1.1.0: 1 value(s), e.g. "
                                           "doc_id sdoc000000002"]


def test_a_referenced_taxonomy_version_must_ship(tmp_path: Path) -> None:
    rep = rv.check_release(write(tmp_path / "r", taxa=("models-1.1.0",)), archive=False)
    assert problems(rep, "taxonomies") == ["taxonomies/models-1.1.0.yaml: refers to orgs 0.2.0 (meta.references), "
                                           "which the release does not ship"]
    rep = rv.check_release(write(tmp_path / "r2", taxa=("models-1.1.0", "orgs-0.2.0")), archive=False)
    assert rep.status == "valid", rep.text()


def test_a_taxonomy_file_must_be_this_repositorys(tmp_path: Path) -> None:
    rep = rv.check_release(write(tmp_path / "r", tweak="# edited\n"), archive=False)
    assert problems(rep, "taxonomies") == ["taxonomies/techniques-1.1.0.yaml: differs from this repository's "
                                           "techniques 1.1.0"]


def test_a_taxonomy_version_this_repository_lacks_goes_to_the_taxonomy_validator(tmp_path: Path) -> None:
    root = write(tmp_path / "r", tweak="bogus_top_level_key: 1\n")
    doc = json.loads((root / "release.json").read_text())
    doc["taxonomies"]["techniques"][0]["version"] = "1.9.0"  # pinned as a version the repository does not have
    (root / "release.json").write_text(json.dumps(doc))
    got = problems(rv.check_release(root, archive=False), "taxonomies")
    assert "taxonomies/techniques-1.1.0.yaml: version field '1.1.0', pinned as 1.9.0" in got
    assert any(g.startswith("taxonomies/techniques-1.1.0.yaml: schema: (top): Additional properties") for g in got), got
    assert not any("differs from this repository" in g for g in got)


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


def _digests(f: Path) -> dict[str, dict[str, str]]:
    return {d.name: {p.relative_to(d).as_posix(): rv.schema_digest(json.loads(p.read_text()))
                     for p in d.rglob("*.json")} for d in f.iterdir() if d.is_dir()}


def test_check_formats(tmp_path: Path) -> None:
    f = _formats(tmp_path, "## [1.0.0]")
    toy = _digests(f)
    assert rv.check_formats(f, digests=toy) == []
    assert rv.check_formats(f) == [f"formats/1.0/{rel}: tools/release-validator.py FORMAT_DIGESTS does not have its "
                                   f"digest ({toy['1.0'][rel]})" for rel in sorted(rv.SCHEMA_FILES_1)]
    (f / "1.0" / "data" / "corpus.schema.json").write_text(json.dumps(CORPUS))  # not stripped
    (f / "1.0" / "extra.json").write_text("{}")
    (f / "1.1").mkdir()
    (f / "CHANGELOG.md").write_text("# Changelog\n")
    got = rv.check_formats(f, digests=toy)
    assert "formats/1.0: no entry [1.0.<patch>] in formats/CHANGELOG.md" in got
    assert "formats/1.0/data/corpus.schema.json: keeps description, title, examples or $comment" in got
    assert "formats/1.0/extra.json: not a schema file of the format" in got
    assert "formats/1.1/release.schema.json: missing" in got


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=cwd, check=True,
                   capture_output=True)


def _edit(f: Path) -> None:
    p = f / "1.0" / "config" / "configs.schema.json"
    p.write_text(p.read_text().replace('"array"', '"object"', 1))


@pytest.mark.parametrize(("entry", "flagged"), [("## [1.0.0] - 2026-11-20", True), ("## [1.0.0]", False),
                                                ("## [1.0.0] \u2013 2026-11-20", True),  # an en dash dates it too
                                                ("## [1.0.0] \u2014 2026-11-20", True)])
def test_an_edit_to_a_dated_format_is_refused(tmp_path: Path, entry: str, flagged: bool) -> None:
    f = _formats(tmp_path, entry)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "formats")
    _git(tmp_path, "commit", "-q", "-m", "archive")
    _edit(f)
    _git(tmp_path, "commit", "-q", "-am", "edit")
    got = rv.check_formats(f, "HEAD~1", digests=_digests(f))
    assert got == (["formats/1.0/config/configs.schema.json: changed since HEAD~1 (M), but format 1.0 was dated "
                    "there; an archived format is never edited"] if flagged else [])


def test_undating_an_entry_is_refused(tmp_path: Path) -> None:
    f = _formats(tmp_path, "## [1.0.0] - 2026-11-20")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "formats")
    _git(tmp_path, "commit", "-q", "-m", "archive")
    (f / "CHANGELOG.md").write_text("# Changelog\n\n## [1.0.0]\n\n- First.\n")  # undated, to edit in a later push
    _git(tmp_path, "commit", "-q", "-am", "undate")
    assert rv.check_formats(f, "HEAD~1", digests=_digests(f)) == [
        "formats/CHANGELOG.md [1.0.0]: dated 2026-11-20 at HEAD~1, undated now; a dated entry stays as it is"]


def test_a_tagged_format_never_changes(tmp_path: Path) -> None:
    """Undate, edit, date again across pushes: each push's --since check passes, the tag does not."""
    f = _formats(tmp_path, "## [1.0.0]")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "formats")
    _git(tmp_path, "commit", "-q", "-m", "archive")
    _git(tmp_path, "tag", "format-1.0.0")
    assert rv.check_formats(f, digests=_digests(f)) == []
    _edit(f)
    _git(tmp_path, "commit", "-q", "-am", "edit while undated")
    assert rv.check_formats(f, "HEAD~1", digests=_digests(f)) == [
        "formats/1.0/config/configs.schema.json: differs from tag format-1.0.0 (M); an archived format is never edited"]


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



# ---------------------------------------------------------------- the version in every place; paths; status
@pytest.mark.parametrize(("kw", "want"), [
    ({"kv": {"release_id": "2026-11-03"}}, "data/corpus.parquet: its key-value metadata has no format_version"),
    ({"kv": {"format_version": "1.0.1", "release_id": "2026-11-03"}},
     "data/corpus.parquet: key-value format_version '1.0.1', release.json '1.0.0'"),
    ({"kv": {"format_version": "1.0.0", "release_id": "2026-11-02"}},
     "data/corpus.parquet: key-value release_id '2026-11-02', release.json '2026-11-03'"),
    ({"rows": [row("sdoc000000001", release_id="2026-11-02")]},
     "data/corpus.parquet release_id: rows carry ['2026-11-02'], release.json '2026-11-03'"),
    ({"card": "# Card without front matter\n"}, "README.md: no YAML front matter"),
    ({"card": "---\nformat_version: 1.0.9\n---\n"}, "README.md: front matter format_version '1.0.9', release.json "
                                                     "'1.0.0'"),
])
def test_the_version_and_release_id_agree_everywhere(tmp_path: Path, kw: dict[str, Any], want: str) -> None:
    rep = rv.check_release(write(tmp_path / "r", **kw), archive=False)
    assert problems(rep, "versions") == [want], rep.text()


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hostname", "data/../../outside.txt"])
def test_a_listed_path_outside_the_release_is_refused_unread(tmp_path: Path, good: Path, path: str) -> None:
    (tmp_path / "outside.txt").write_text("x")
    doc = json.loads((good / "release.json").read_text())
    doc["files"].append({"path": path, "bytes": 1, "sha256": "0" * 64})
    (good / "release.json").write_text(json.dumps(doc))
    assert problems(rv.check_release(good, archive=False), "manifest") == [
        f"{path}: a path outside the release directory (absolute or with ..)"]


def test_a_failing_format_independent_check_is_invalid_whatever_the_version(tmp_path: Path) -> None:
    own = {**RELEASE, "required": ["files"], "properties": {**RELEASE["properties"], "format_version": {}}}
    root = write(tmp_path / "r", version="2.0.0", release_schema=own)
    assert rv.check_release(root, archive=False).exit_code == rv.EXIT_UNSUPPORTED
    (root / "config" / "configs.json").write_text("[]")
    rep = rv.check_release(root, archive=False)
    assert rep.status == "invalid" and rep.exit_code == rv.EXIT_INVALID


def test_a_standalone_run_compares_the_schemas_with_its_digests(tmp_path: Path, good: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a checkout of this repository (a release's own copy), the archived schemas' digests in this file."""
    f = _formats(tmp_path, "## [1.0.0]")
    monkeypatch.setattr(rv, "DATA_REPO", None)
    monkeypatch.setattr(rv, "FORMAT_DIGESTS", _digests(f))
    assert rv.check_release(good).status == "valid"
    js = json.loads((good / "config" / "overrides.schema.json").read_text())
    js["properties"]["kind"]["description"] = "Reworded."  # prose: the same format
    (good / "config" / "overrides.schema.json").write_text(json.dumps(js))
    _rehash(good)
    assert rv.check_release(good).status == "valid"
    js["properties"]["kind"]["enum"].append("manual_source")  # structure: not the archived format
    (good / "config" / "overrides.schema.json").write_text(json.dumps(js))
    _rehash(good)
    assert problems(rv.check_release(good), "archive") == [
        "config/overrides.schema.json: differs from format 1.0 as archived (this checker's digest of "
        "formats/1.0/config/overrides.schema.json)"]
    monkeypatch.setattr(rv, "FORMAT_DIGESTS", {})
    rep = rv.check_release(good)
    assert rep.status == "valid" and any("knows no digests of format 1.0" in x for x in rep.not_checked)


def _rehash(root: Path) -> None:
    doc = json.loads((root / "release.json").read_text())
    for fe in doc["files"]:
        fe["bytes"], fe["sha256"] = (root / fe["path"]).stat().st_size, sha(root / fe["path"])
    (root / "release.json").write_text(json.dumps(doc))


def test_the_repositorys_archive_has_its_digests_here() -> None:
    assert rv.check_formats(TOOLS.parent / "formats") == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
