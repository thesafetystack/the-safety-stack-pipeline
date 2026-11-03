"""The release format (D803): the generated schemas equal the archived format of the declared version
(``data-repo/formats/<major>.<minor>/``, compared structurally) and the changelog has its entry; the classifier, one
fixture per rule of ``data-repo/formats/README.md``; the check of a declared version whose directory is missing; and
the export end to end on synthetic rows (the real ``run_dataset_export`` with its store reads replaced): the release
checker passes on it, its files carry the format version, and ``check_bundle`` refuses a tampered bundle."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from stack.fold import dataset as ds
from stack.fold.release import REPO_ROOT
from stack.schemas.blocks import DOCUMENT_FIELDS
from tests.synth.rows import document_row
from tests.test_dataset_release import AT, D, _affiliation_row, _inputs, _pin, _row

_spec = importlib.util.spec_from_file_location("release_format_tool", REPO_ROOT / "tools" / "release_format.py")
assert _spec is not None and _spec.loader is not None
rf = importlib.util.module_from_spec(_spec)
sys.modules["release_format_tool"] = rf
_spec.loader.exec_module(rf)

FORMATS = ds.DATA_REPO / "formats"


@pytest.fixture(scope="module")
def m() -> ds.Mapping_:
    return ds.Mapping_(ds.DATASET_DIR / "mapping.yaml")


@pytest.fixture(scope="module")
def gen(m: ds.Mapping_) -> dict[str, dict[str, Any]]:
    return rf.stripped(rf.generated(m))


# ---------------------------------------------------------------- the archive and the changelog
def test_the_generated_schemas_equal_the_archive_of_the_declared_version(m: ds.Mapping_,
                                                                         gen: dict[str, Any]) -> None:
    mm = ".".join(m.format_version.split(".")[:2])
    assert (FORMATS / mm).is_dir(), f"no data-repo/formats/{mm}/: tools/release_format.py check says what to do"
    v = rf.check(FORMATS, m.format_version, gen)
    assert v.ok, "\n".join(v.lines)
    arch = rf.read_dir(FORMATS / mm)
    assert set(arch) == set(ds.FORMAT_SCHEMA_FILES)
    for rel, js in gen.items():  # structurally: key order, required and enum order do not matter
        assert ds.release_validator().canonical(arch[rel]) == ds.release_validator().canonical(js), rel


def test_the_declared_version_has_a_changelog_entry(m: ds.Mapping_) -> None:
    entries = ds.release_validator().changelog_entries((FORMATS / "CHANGELOG.md").read_text("utf-8"))
    assert m.format_version in entries


def test_a_prose_change_is_no_change(gen: dict[str, Any], m: ds.Mapping_) -> None:
    full = rf.generated(m)
    full[ds.ROW_SCHEMA_FILE]["properties"]["doc_id"]["description"] = "Reworded."
    full[ds.RELEASE_SCHEMA_FILE]["title"] = "Retitled"
    assert rf.stripped(full) == gen
    assert rf.check(FORMATS, m.format_version, rf.stripped(full)).ok


def test_the_schemas_carry_the_format_marks(m: ds.Mapping_) -> None:
    js = rf.generated(m)[ds.ROW_SCHEMA_FILE]
    p = js["properties"]
    assert js["$id"] == "urn:the-safety-stack:format:1.0:data/corpus.schema.json"
    assert p["published_precision"]["enum"] == ["day", "year_inferred", "unknown"]  # closed
    role = p["content"]["properties"]["role"]
    assert role["x-open"] is True and "full_text" in role["x-values"] and "enum" not in role  # open
    assert not {"enum", "x-open", "x-values"} & set(p["doc_type"])  # a plain string (in_schema: false)
    assert p["content"]["properties"]["source"]["properties"]["captured_at"]["x-arrow"] == "timestamp[us, tz=UTC]"
    assert p["published_date"]["x-arrow"] == "date32[day]" and p["identity"]["x-arrow"] == "struct"
    buckets = p["annotations"]["properties"]["screen"]["properties"]["payload"]["properties"]["buckets"]
    assert buckets["x-arrow"] == "list" and buckets["items"]["x-open"] is True  # a list's marks sit on its items

    def every(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
        if isinstance(node, dict) and "type" in node:
            yield path, node
            for k, sub in (node.get("properties") or {}).items():
                yield from every(sub, f"{path}.{k}")
            if "items" in node:
                yield from every(node["items"], path + "[]")

    nodes = list(every(js))
    assert len(nodes) > 300 and all("x-arrow" in n for _p, n in nodes)


@pytest.mark.parametrize(("spec", "message"), [
    ({"values": ["a"], "in_schema": False}, "in_schema"),
    ({"values": ["a"], "open": True, "bogus": 1}, "bogus"),
])
def test_the_mapping_refuses_a_bad_list_flag(tmp_path: Path, spec: dict[str, Any], message: str) -> None:
    from stack import yamlio

    raw = yamlio.load((ds.DATASET_DIR / "mapping.yaml").read_text("utf-8"))
    next(e for e in raw["fields"] if e["public"] == "doc_type")["enum"] = spec
    p = tmp_path / "mapping.yaml"
    p.write_text(json.dumps(raw))
    with pytest.raises(ds.DatasetError, match=message):
        ds.Mapping_(p)


def test_the_mapping_refuses_a_bad_format_version(tmp_path: Path) -> None:
    from stack import yamlio

    raw = yamlio.load((ds.DATASET_DIR / "mapping.yaml").read_text("utf-8"))
    raw["format_version"] = "1.0"
    p = tmp_path / "mapping.yaml"
    p.write_text(json.dumps(raw))
    with pytest.raises(ds.DatasetError, match="format_version"):
        ds.Mapping_(p)


# ---------------------------------------------------------------- the classifier: one fixture per rule
ROW, REL, OVR = ds.ROW_SCHEMA_FILE, ds.RELEASE_SCHEMA_FILE, ds.OVERRIDES_SCHEMA_FILE


def _p(js: dict[str, Any], *path: str) -> dict[str, Any]:
    for k in path:
        js = js["items"] if k == "[]" else js["properties"][k]
    return js


def _rename(js: dict[str, Any], old: str, new: str) -> None:
    js["properties"][new] = js["properties"].pop(old)
    js["required"] = [new if r == old else r for r in js["required"]]


def _type(js: dict[str, Any], t: str) -> None:
    js["type"] = [t, "null"] if isinstance(js["type"], list) else t


def _drop_null(js: dict[str, Any]) -> None:
    js["type"] = [x for x in js["type"] if x != "null"][0]


RULES: list[tuple[str, Callable[[dict[str, Any]], None], str, str]] = [
    ("a field removed", lambda s: (_p(s[ROW], "identity")["properties"].pop("channels"),
                                   _p(s[ROW], "identity")["required"].remove("channels")),
     "major", "data/corpus.schema.json:identity.channels: field removed"),
    ("a field renamed", lambda s: _rename(s[ROW], "title", "headline"),
     "major", "data/corpus.schema.json:title: field removed or renamed"),
    ("a file removed", lambda s: _p(s[REL], "files")["allOf"].pop(0),
     "major", "file:FORMAT.md: file removed"),
    ("a JSON type changed", lambda s: _type(_p(s[ROW], "release", "quote_chars"), "string"),
     "major", "data/corpus.schema.json:release.quote_chars: JSON type ['integer'] -> ['string']"),
    ("an x-arrow type changed", lambda s: _p(s[ROW], "release", "quote_chars").update({"x-arrow": "int64"}),
     "major", "data/corpus.schema.json:release.quote_chars: x-arrow int32 -> int64"),
    ("nullability loosened", lambda s: _p(s[ROW], "doc_id").update({"type": ["string", "null"]}),
     "major", "data/corpus.schema.json:doc_id: may now be null"),
    ("a closed list gains a value", lambda s: _p(s[ROW], "published_precision")["enum"].append("month"),
     "major", "data/corpus.schema.json:published_precision: closed list"),
    ("a closed list loses a value", lambda s: _p(s[ROW], "published_precision")["enum"].remove("unknown"),
     "major", "data/corpus.schema.json:published_precision: closed list"),
    ("a closed list opened", lambda s: _p(s[ROW], "published_precision").update(
        {"x-open": True, "x-values": _p(s[ROW], "published_precision").pop("enum")}),
     "major", "data/corpus.schema.json:published_precision: no longer a closed list"),
    ("an open list made a plain string",
     lambda s: [_p(s[ROW], "content", "role").pop(k) for k in ("x-open", "x-values")],
     "major", "data/corpus.schema.json:content.role: no longer an open list (a plain string)"),
    ("the format_version pattern changed", lambda s: _p(s[REL], "format_version").update({"pattern": "^1\\.0\\.0$"}),
     "major", "release.schema.json:format_version: other keywords changed"),
    ("an open-list value removed", lambda s: _p(s[ROW], "content", "role")["x-values"].remove("readme"),
     "major", "data/corpus.schema.json:content.role: open-list values removed: ['readme']"),
    ("a required key made optional", lambda s: s[REL]["required"].remove("stand_ins"),
     "major", "release.schema.json:stand_ins: no longer required"),
    ("another keyword changed", lambda s: _p(s[REL], "release_id").update({"minLength": 10}),
     "major", "release.schema.json:release_id: other keywords changed"),
    ("a closed object opened", lambda s: s[OVR].update({"additionalProperties": True}),
     "major", "config/overrides.schema.json:: other keywords changed"),
    ("a field added", lambda s: (_p(s[ROW], "identity")["properties"].update(
        {"language": {"type": ["string", "null"], "x-arrow": "string"}}),
        _p(s[ROW], "identity")["required"].append("language")),
     "minor", "data/corpus.schema.json:identity.language: field added"),
    ("a file added", lambda s: _p(s[REL], "files")["allOf"].append(
        {"contains": {"properties": {"path": {"const": "data/texts.parquet"}}, "required": ["path"]}}),
     "minor", "file:data/texts.parquet: file added"),
    ("an open-list value added", lambda s: _p(s[ROW], "content", "role")["x-values"].append("slides"),
     "minor", "data/corpus.schema.json:content.role: open-list values added: ['slides']"),
    ("a taxonomy key added", lambda s: _p(s[REL], "taxonomies")["properties"].update(
        {"agendas": copy.deepcopy(_p(s[REL], "taxonomies", "orgs"))}),
     "minor", "release.schema.json:taxonomies.agendas: field added"),
    ("nullability tightened", lambda s: _drop_null(_p(s[ROW], "title")),
     "minor", "data/corpus.schema.json:title: no longer null"),
]


@pytest.mark.parametrize(("name", "edit", "level", "what"), RULES, ids=[r[0] for r in RULES])
def test_the_classifier(gen: dict[str, Any], name: str, edit: Callable[[dict[str, Any]], None], level: str,
                        what: str) -> None:
    new = copy.deepcopy(gen)
    edit(new)
    changes = rf.classify(rf.flatten(gen), rf.flatten(new))
    assert rf.needed(changes) == level, changes
    assert any(w.startswith(what) and lvl == level for lvl, w in changes), changes


def test_the_classifier_sees_no_change_between_versions_in_the_version_pattern(gen: dict[str, Any]) -> None:
    new = copy.deepcopy(gen)
    _p(new[REL], "format_version")["pattern"] = "^1\\.1\\.(0|[1-9][0-9]*)$"
    for js in new.values():
        js["$id"] = js["$id"].replace(":1.0:", ":1.1:")
    assert rf.classify(rf.flatten(gen), rf.flatten(new)) == []


# ---------------------------------------------------------------- the check of a declared version
def _formats(tmp_path: Path, entries: str = "## [1.0.0] - 2026-11-20\n\n## [1.1.0]\n\n## [2.0.0]\n") -> Path:
    f = tmp_path / "formats"
    shutil.copytree(FORMATS / "1.0", f / "1.0")
    (f / "CHANGELOG.md").write_text(f"# Changelog\n\n{entries}")
    return f


def _with_field(gen: dict[str, Any]) -> dict[str, Any]:
    new = copy.deepcopy(gen)
    _p(new[ROW], "identity")["properties"]["language"] = {"type": ["string", "null"], "x-arrow": "string"}
    _p(new[ROW], "identity")["required"].append("language")
    return new


def _without_field(gen: dict[str, Any]) -> dict[str, Any]:
    new = copy.deepcopy(gen)
    _p(new[ROW], "identity")["properties"].pop("channels")
    _p(new[ROW], "identity")["required"].remove("channels")
    return new


def test_a_missing_directory_is_classified_against_the_highest_lower_version(tmp_path: Path,
                                                                             gen: dict[str, Any]) -> None:
    f = _formats(tmp_path)
    v = rf.check(f, "1.1.0", _with_field(gen))  # a field added: minor, declared minor
    assert not v.ok and (v.needed, v.bump, v.enough, v.has_entry) == ("minor", "minor", True, True)
    assert any("archive --data-repo" in line for line in v.lines)
    v = rf.check(f, "1.1.0", _without_field(gen))  # a field removed: major, declared minor
    assert (v.needed, v.bump, v.enough) == ("major", "minor", False)
    assert "declare at least a major version above 1.0 (mapping.yaml format_version)" in v.lines
    v = rf.check(f, "2.0.0", _without_field(gen))
    assert (v.needed, v.bump, v.enough) == ("major", "major", True)
    v = rf.check(f, "1.2.0", gen)  # no change: any bump is enough, but the version needs its entry
    assert (v.needed, v.enough, v.has_entry) == ("none", True, False)
    assert "formats/CHANGELOG.md has no entry [1.2.0]" in v.lines
    v = rf.check(tmp_path / "empty", "1.0.0", gen)
    assert not v.ok and "1.0.0 is the first format" in v.lines[0]


def test_a_changed_format_under_an_archived_version_is_refused(tmp_path: Path, gen: dict[str, Any]) -> None:
    f = _formats(tmp_path)
    assert rf.check(f, "1.0.0", gen).ok
    assert rf.check(f, "1.0.3", gen).ok is False  # a patch version needs its own changelog entry
    v = rf.check(f, "1.0.0", _with_field(gen))
    assert not v.ok and v.needed == "minor" and "an archived format does not change" in v.lines[0]


def test_archive_writes_and_never_silently_overwrites(tmp_path: Path, gen: dict[str, Any]) -> None:
    import subprocess

    f = _formats(tmp_path)  # 1.0.0 dated
    d = rf.write_archive(tmp_path, "1.1.0", gen)
    assert d == f / "1.1" and rf.read_dir(d) == gen
    assert rf.write_archive(tmp_path, "1.0.0", gen) == f / "1.0"  # the same schemas: nothing to do
    with pytest.raises(SystemExit, match="dated"):
        rf.write_archive(tmp_path, "1.0.0", _with_field(gen), replace=True)
    with pytest.raises(SystemExit, match="--replace"):  # 1.1 is undated: replaced only on request
        rf.write_archive(tmp_path, "1.1.0", _with_field(gen))
    assert rf.read_dir(f / "1.1") == gen
    assert rf.read_dir(rf.write_archive(tmp_path, "1.1.0", _with_field(gen), replace=True)) == _with_field(gen)
    for args in (["init", "-q"], ["add", "formats"], ["commit", "-q", "-m", "a"], ["tag", "format-1.1.0"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=tmp_path,
                       check=True, capture_output=True)
    with pytest.raises(SystemExit, match="tagged format-1.1.0"):  # tagged: never, whatever the changelog says
        rf.write_archive(tmp_path, "1.1.0", gen, replace=True)


def test_the_vendored_archive_matches_its_lock(tmp_path: Path) -> None:
    assert rf.lock_problems(FORMATS) == []
    f = tmp_path / "formats"
    shutil.copytree(FORMATS, f)
    p = f / "1.0" / ds.ROW_SCHEMA_FILE
    p.write_text(p.read_text().replace('"x-arrow": "string"', '"x-arrow": "large_string"', 1))
    (f / "1.0" / "extra.json").write_text("{}")
    assert rf.lock_problems(f) == ["data-repo/formats/1.0/data/corpus.schema.json: sha256 differs from the lock",
                                   "data-repo/formats/1.0/extra.json: not in the lock"]


def _docker_keeps(rel: str, patterns: list[str]) -> bool:
    """Whether ``.dockerignore`` ``patterns`` keep repo path ``rel`` in the image (the last matching pattern wins; a
    pattern matches a path or any of its parent directories; ``**`` any directories, ``*`` within one)."""
    import re

    def rx(p: str) -> re.Pattern[str]:
        out = ""
        for tok in re.split(r"(\*\*/|\*\*|\*|\?)", p.strip("/")):
            out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(tok, re.escape(tok))
        return re.compile(out + r"\Z")

    parts = rel.split("/")
    keep = True
    for pat in patterns:
        neg, body = pat.startswith("!"), pat.removeprefix("!")
        if any(rx(body).match("/".join(parts[:i])) for i in range(1, len(parts) + 1)):
            keep = neg
    return keep


def test_the_image_keeps_the_archive_and_the_checker() -> None:
    lines = [x.strip() for x in (REPO_ROOT / ".dockerignore").read_text().splitlines()]
    pats = [x for x in lines if x and not x.startswith("#")]
    files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in FORMATS.rglob("*") if p.is_file())
    files.append("data-repo/tools/release-validator.py")
    assert all(_docker_keeps(f, pats) for f in files), [f for f in files if not _docker_keeps(f, pats)]
    assert not _docker_keeps("data/corpus.parquet", pats) and not _docker_keeps("x/data/y", pats)


# ---------------------------------------------------------------- the export on synthetic rows
def _corpus(n: int) -> tuple[pa.Table, dict[str, bytes]]:
    """``n`` acquired documents, every other one with an affiliation and its organisations record, each annotated as
    the dataset tests annotate; texts by sha256 (the internal variant's blobs)."""
    from stack.fold import chained_basis

    rows, texts = [], {}
    for i in range(n):
        r = document_row(i, state="acquired")
        r["identity"]["members"][0]["key_kind"] = "doi"  # the synthetic world's `arxiv` is not a listed key kind
        body = f"# Document {i}\n\nThe text of document {i}.\n".encode()
        sha = hashlib.sha256(body).hexdigest()
        texts[sha] = body
        c = r["content"]
        c["current_text_sha256"] = c["snapshots"][0]["text"]["sha256"] = sha
        r["metadata"]["field_sources"].append({"field": "abstract", "source_id": r["doc_id"], "origin": "provider"})
        ann = (_affiliation_row() if i % 2 == 0 else _row())["annotations"]
        for rec in ann.values():
            if isinstance(rec, dict) and rec.get("basis_kind") == "text":
                rec["basis_digest"] = sha
        if ann.get("affiliation_orgs"):
            ann["affiliation_orgs"]["basis_digest"] = chained_basis(sha, ann, ("affiliation",))
        ann["screen"]["payload"]["verdict"] = "relevant"
        ann["technique_staged"]["payload"]["taxonomy_version"] = "1.1.0"
        r["annotations"] = ann
        rows.append(r)
    return pa.Table.from_pylist(rows, schema=pa.schema(DOCUMENT_FIELDS)), texts


def export(tmp: Path, variant: str = "public", n: int = 12, data_repo: Path | None = None) -> tuple[Path, float]:
    """The real :func:`stack.fold.dataset.run_dataset_export` over :func:`_corpus`, with the store reads (the
    corpus, the build gate, the run records, the quality re-scoring) replaced; returns the release and the export's
    seconds."""
    tbl, texts = _corpus(n)
    inp = _inputs([])
    inp.pins[D["technique_staged"]] = _pin(
        "technique_staged", "technique-staged-v5-a1.1", ["openai/gpt-6-luna"],
        {"name": "technique-staged-v5-a1.1", "taxonomy": "taxonomy/techniques-1.1.0.yaml",
         "role": "extract_staged_openai"})
    blobs = tmp / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for sha, body in texts.items():
        (blobs / sha).write_bytes(body)
    (tmp / "store").mkdir(exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ds, "corpus_input", lambda root, key, fold_config: (tbl, {"file_id": "f1", "code_digest": "c" * 64},
                                                                        Path("corpus")))
        mp.setattr(ds, "release_gate", lambda root, fid, allow_incomplete=False: ("b1", "ok"))
        mp.setattr(ds, "load_inputs", lambda root, bid, status, h: inp)
        mp.setattr(ds, "release_quality", lambda root, build, t, je: ({}, {"synthetic": True}))
        mp.setattr(ds, "judge_eval", lambda rel, bid, rescored: ds.JudgeEval(
            "(synthetic: no judge evaluation)", {"synthetic": True, "missed_relevant": {"not_relevant_records": 4321}}))
        if data_repo is not None:
            mp.setattr(ds, "DATA_REPO", data_repo)
        t0 = time.perf_counter()
        res = ds.run_dataset_export(tmp / "store", "corpus", f"dataset-{variant}", tmp / variant,
                                    blobs_dir=blobs if variant == "internal" else None,
                                    now=dt.datetime(2026, 11, 3, 12, tzinfo=dt.UTC))
        return res.path, time.perf_counter() - t0


@pytest.fixture(scope="module")
def public(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out, secs = export(tmp_path_factory.mktemp("export"))
    print(f"synthetic public export: {secs:.1f} s")
    return out


@pytest.mark.slow
def test_the_checker_passes_on_the_synthetic_export(public: Path, m: ds.Mapping_) -> None:
    rep = ds.release_validator().check_release(public, rows="all", archive=FORMATS, require_archive=True)
    assert rep.status == "valid", rep.text()
    assert rep.rows_checked == 12 and rep.checks["archive"] == "formats/1.0/ compared"
    doc = json.loads((public / "release.json").read_text())
    assert doc["format_version"] == m.format_version and "format" not in doc
    assert list(doc)[:2] == ["release_id", "format_version"]
    meta = pq.read_schema(public / "data" / "corpus.parquet").metadata
    assert meta[b"format_version"] == m.format_version.encode() and meta[b"release_id"] == b"2026-11-03"
    assert b"stack.format" not in meta
    assert (public / ds.VALIDATOR_FILE).read_bytes() == (ds.DATA_REPO / ds.VALIDATOR_FILE).read_bytes()
    card = (public / "README.md").read_text()
    assert f"\nformat_version: {m.format_version}\n" in card.split("---")[1] and "**Checking a download.**" in card
    fmt = (public / "FORMAT.md").read_text()
    assert "## Format versions" in fmt and "@release-2026-11-03/data/corpus.parquet" in fmt
    assert "`taxonomies/orgs-0.2.0.yaml` because `taxonomies/models-1.1.0.yaml` refers to it" in fmt
    vocab = (public / "VOCABULARY.md").read_text()
    assert "**`content.role`** (open list)" in vocab and "**`published_precision`** (closed list)" in vocab
    assert "**`doc_type`** (plain string)" in vocab


@pytest.mark.slow
def test_the_export_ships_the_referenced_organisation_vocabulary(public: Path) -> None:
    orgs = json.loads((public / "release.json").read_text())["taxonomies"]["orgs"]
    assert [(o["version"], o.get("referenced_by")) for o in orgs] == [
        ("0.2.0", ["taxonomies/models-1.1.0.yaml"]), ("0.2.2", None)]
    assert ds.newest_taxonomy({"taxonomies": {"orgs": orgs}}, "orgs")["version"] == "0.2.2"


@pytest.mark.slow
def test_the_card_numbers_list_is_filled_from_the_export(public: Path) -> None:
    """The card's "This release in numbers" placeholders (channels, triage, screened out, organisations) come from the
    export: the anchor channels, the judge eval record, release.json ``rows`` and the shipped orgs vocabulary."""
    card = (public / "README.md").read_text()
    doc = json.loads((public / "release.json").read_text())
    assert "{{" not in card and "TODO" not in card
    orgs = ds.newest_taxonomy(doc, "orgs")["path"]
    n_orgs = len(ds.yamlio.load((public / orgs).read_text("utf-8"))["entries"])
    assert "Selection: 4,321 records marked not relevant by triage" in card
    assert f"{doc['rows']['filtered_out']:,} were not." in card
    assert f"and {n_orgs:,} organisations." in card
    assert re.search(r"Collected from \d+ source channels", card)


@pytest.mark.slow
def test_the_internal_export_passes_too(tmp_path: Path) -> None:
    out, _secs = export(tmp_path, "internal", n=4)
    rep = ds.release_validator().check_release(out, rows="all", archive=FORMATS, require_archive=True)
    assert rep.status == "valid", rep.text()


def _rehash(root: Path) -> None:
    doc = json.loads((root / "release.json").read_text())
    for fe in doc["files"]:
        p = root / fe["path"]
        fe["bytes"], fe["sha256"] = p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()
    (root / "release.json").write_text(json.dumps(doc, indent=2))


def _rewrite_corpus(root: Path, edit: Callable[[list[dict[str, Any]]], None]) -> None:
    p = root / "data" / "corpus.parquet"
    t = pq.read_table(p)
    rows = t.to_pylist()
    edit(rows)
    pq.write_table(pa.Table.from_pylist(rows, schema=t.schema), p)


def _bad_enum(rows: list[dict[str, Any]]) -> None:
    rows[1]["published_precision"] = "sometimes"


def _new_role(rows: list[dict[str, Any]]) -> None:
    rows[0]["content"]["role"] = "slides"


def _dup(rows: list[dict[str, Any]]) -> None:
    rows[1]["doc_id"] = rows[0]["doc_id"]


NEGATIVE: list[tuple[str, Callable[[Path], None], str]] = [
    ("a corrupt file", lambda r: (r / "config" / "configs.json").write_text("[{"),
     "release-validator: [manifest] config/configs.json: size or sha256 differs from release.json"),
    ("a bad closed-list value", lambda r: (_rewrite_corpus(r, _bad_enum), _rehash(r)),
     "release-validator: [rows] data/corpus.parquet published_precision: 1 row(s)"),
    ("a dangling config id", lambda r: ((r / "config" / "configs.json").write_text(json.dumps(
        json.loads((r / "config" / "configs.json").read_text())[1:], indent=2)), _rehash(r)),
     "release-validator: [ids] annotations.<kind>.metadata.config_id:"),
    ("a duplicate doc id", lambda r: (_rewrite_corpus(r, _dup), _rehash(r)),
     "release-validator: [ids] doc_id: 1 ids on more than one row"),
    ("a missing listed file", lambda r: (r / "VOCABULARY.md").unlink(),
     "release-validator: [manifest] VOCABULARY.md: listed in release.json, missing"),
    ("an unlisted file", lambda r: (r / "notes.txt").write_text("x"),
     "release-validator: [manifest] notes.txt: present but not listed in release.json"),
    ("an unknown format version", lambda r: (r / "release.json").write_text(
        (r / "release.json").read_text().replace('"format_version": "1.0.0"', '"format_version": "2.0.0"')),
     "release-validator: [manifest] release.json format_version: '2.0.0' does not match"),
    ("a schema that is not the archived one", lambda r: ((r / ds.OVERRIDES_SCHEMA_FILE).write_text(
        (r / ds.OVERRIDES_SCHEMA_FILE).read_text().replace('"metadata_fix",', "")), _rehash(r)),
     "release-validator: [archive] config/overrides.schema.json: differs from formats/1.0/"),
    ("an unlisted open-list value", lambda r: (_rewrite_corpus(r, _new_role), _rehash(r)),
     "content.role: values ['slides'] are not in its open list (x-values)"),
    ("an edited checker", lambda r: ((r / ds.VALIDATOR_FILE).write_text("# edited\n"), _rehash(r)),
     f"{ds.VALIDATOR_FILE}: not the vendored data repository's file"),
]


@pytest.mark.slow
@pytest.mark.parametrize(("name", "tamper", "want"), NEGATIVE, ids=[n[0] for n in NEGATIVE])
def test_check_bundle_refuses_a_tampered_bundle(public: Path, tmp_path: Path, m: ds.Mapping_, name: str,
                                                tamper: Callable[[Path], None], want: str) -> None:
    root = tmp_path / "rel"
    shutil.copytree(public, root)
    assert ds.check_bundle(root, m, ds.load_dataset_config("dataset-public")) == []
    tamper(root)
    got = ds.check_bundle(root, m, ds.load_dataset_config("dataset-public"))
    assert any(p.startswith(want) for p in got), got


@pytest.mark.slow
def test_the_export_keeps_nothing_when_the_archive_differs(tmp_path: Path) -> None:
    dr = tmp_path / "data-repo"
    shutil.copytree(ds.DATA_REPO, dr, ignore=shutil.ignore_patterns(".git"))
    js = json.loads((dr / "formats" / "1.0" / ds.ROW_SCHEMA_FILE).read_text())
    js["properties"]["doc_id"]["x-arrow"] = "large_string"
    (dr / "formats" / "1.0" / ds.ROW_SCHEMA_FILE).write_text(json.dumps(js))
    with pytest.raises(ds.DatasetError, match=r"nothing kept: release-validator: \[archive\] data/corpus.schema.json"):
        export(tmp_path, data_repo=dr)
    assert not (tmp_path / "public").exists()
