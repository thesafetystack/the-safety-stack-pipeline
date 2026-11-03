# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow>=16", "jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Check a release of The Safety Stack dataset, or this repository's archive of release formats.

    uv run tools/release-validator.py <release directory>            # a seeded sample of 1000 rows
    uv run tools/release-validator.py <release directory> --rows all # every row
    uv run tools/release-validator.py --formats [--since <git ref>]  # formats/<major>.<minor>/ (CI)

A release (format 1.0.0 on) names its format in `release.json` `format_version`; the same file sits in this
repository as `tools/release-validator.py` and in every release as `tools/release-validator.py`. The checks:

- **version**: `format_version` of `release.json`. A format whose major version this checker does not know (or a
  release without the field: the dated releases before 1.0.0) is an *unsupported format version*: exit code 3, and
  only the checks that do not depend on the format run (manifest, sibling schemas, rows), not "invalid". A newer
  minor version of a known major is checked as that major: a minor version only adds.
- **manifest**: `release.json` against `release.schema.json`; every file it lists exists with its size and sha256;
  no file it does not list is present (`release.json` itself aside; the hosting's hidden top-level entries, such as
  `.gitattributes` and `.cache/` of a Hugging Face download, are ignored and reported).
- **schemas**: every `<stem>.schema.json` validates its sibling `<stem>.json` (whole), `<stem>.jsonl` (each line) or
  `<stem>.parquet` (the footer's Arrow schema against the `x-arrow` keyword of every node; then the rows).
- **rows**: the parquet rows as JSON values (dates and timestamps as ISO strings), with format checking: a seeded
  sample (`--rows N`, default 1000) or every row (`--rows all`).
- **ids** (format 1): every row: `doc_id` unique; every `config_id` (`annotations.<kind>.metadata.config_id`) an entry
  of `config/configs.json`, and an `override:<ids>` config's ids lines of `config/overrides.jsonl`; override ids
  unique; every override id a row names a line, of a kind that fits where the row names it; each line's target and
  key of the shape its kind takes (an override may name a document absent from the release, such as an exclusion).
- **taxonomies** (format 1): every file `release.json` pins exists with its sha256; the ids of every record resolve
  in the taxonomy file its config pinned (organisations: the version the row's `affiliation_orgs` record names; a
  technique record of an override config: its `taxonomy_version`). Run from a checkout of the data repository, every
  shipped taxonomy file also goes through `tools/taxonomy-schema-validator.py` with that repository's schemas.
- **archive**: run from a checkout of the data repository, the release's schemas, stripped of description, title,
  examples and $comment, equal `formats/<major>.<minor>/` (objects compared without key order, `required`, `enum`,
  `type` and `x-values` as sets).

`--formats` checks the archive itself: each `formats/<major>.<minor>/` holds the format's schema files, valid JSON
Schemas (2020-12) with the format's `$id`, already stripped, with an entry in `formats/CHANGELOG.md`; with `--since
<ref>`, no file of a directory whose changelog entry was dated at `<ref>` has changed since.

Exit codes: 0 valid; 1 invalid; 2 usage; 3 unsupported format version. Also usable as a module:
`check_release(path, rows=...)` returns a :class:`Report`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import random
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import yaml
from jsonschema import Draft202012Validator, FormatChecker

CHECKER_VERSION = "1.0.0"
#: The format majors this checker knows; a later minor of one is checked as that major.
SUPPORTED_MAJORS = (1,)
EXIT_OK, EXIT_INVALID, EXIT_USAGE, EXIT_UNSUPPORTED = 0, 1, 2, 3
DEFAULT_SAMPLE = 1000
TOOLS = Path(__file__).resolve().parent
#: The data repository this file sits in, when it does (tools/ beside taxonomies/ and formats/).
DATA_REPO = TOOLS.parent if (TOOLS.parent / "taxonomies").is_dir() else None
ID_PREFIX = "urn:the-safety-stack:format"
#: The schema files of a format-1 release (and of its archive directory), by path.
SCHEMA_FILES_1 = ("release.schema.json", "data/corpus.schema.json", "config/configs.schema.json",
                  "config/overrides.schema.json")
STRIP_KEYS = frozenset({"description", "title", "examples", "$comment"})
SET_KEYS = frozenset({"required", "enum", "type", "x-values"})
#: Keywords whose value is a map from names to subschemas (the names are not keywords).
NAMED_SCHEMAS = frozenset({"properties", "$defs", "patternProperties", "dependentSchemas"})
#: Keywords whose value is data, not a subschema.
DATA_KEYS = frozenset({"enum", "const", "required", "x-values", "default", "examples", "x-arrow", "x-open"})
DOC_ID = re.compile(r"^[0-9a-z]{13}$")
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CHANGELOG_ENTRY = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?:\s*-\s*(\d{4}-\d{2}-\d{2}))?", re.M)


# ---------------------------------------------------------------- the report
@dataclass
class Problem:
    check: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.where}: {self.message}"


@dataclass
class Report:
    """What :func:`check_release` found. ``status``: ``valid``, ``invalid`` or ``unsupported``."""

    release: str
    format_version: str | None = None
    supported: bool = False
    rows_total: int = 0
    rows_checked: int = 0
    rows_mode: str = ""
    checks: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checker_version: str = CHECKER_VERSION

    def add(self, check: str, where: str, message: str) -> None:
        self.problems.append(Problem(check, where, message))

    @property
    def status(self) -> str:
        if not self.supported:
            return "unsupported"
        return "invalid" if self.problems else "valid"

    @property
    def exit_code(self) -> int:
        return {"valid": EXIT_OK, "invalid": EXIT_INVALID, "unsupported": EXIT_UNSUPPORTED}[self.status]

    def as_dict(self) -> dict[str, Any]:
        return {"checker_version": self.checker_version, "release": self.release, "status": self.status,
                "format_version": self.format_version, "supported": self.supported,
                "rows": {"total": self.rows_total, "checked": self.rows_checked, "mode": self.rows_mode},
                "checks": self.checks, "problems": [p.__dict__ for p in self.problems],
                "not_checked": self.not_checked, "ignored": self.ignored, "notes": self.notes}

    def text(self) -> str:
        out = [f"release-validator {self.checker_version}: {self.release}",
               f"format_version: {self.format_version or '(none)'}"
               f"{'' if self.supported else ' (unsupported format version)'}",
               f"rows: {self.rows_total:,}; validated: {self.rows_mode or 'none'}"]
        out += [f"  {k}: {v}" for k, v in self.checks.items()]
        out += [f"not checked: {x}" for x in self.not_checked]
        out += [f"note: {x}" for x in self.notes]
        if self.ignored:
            out.append(f"ignored: {', '.join(self.ignored)}")
        if self.problems:
            out.append(f"PROBLEMS ({len(self.problems)}):")
            out += [f"  {p}" for p in self.problems]
        out.append({"valid": "VALID", "invalid": "INVALID",
                    "unsupported": "UNSUPPORTED FORMAT VERSION"}[self.status])
        return "\n".join(out)


# ---------------------------------------------------------------- JSON Schema helpers
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$")
FORMATS = FormatChecker([])


@FORMATS.checks("date", raises=ValueError)
def _is_date(v: object) -> bool:
    if not isinstance(v, str):
        return True
    return bool(_DATE.match(v)) and dt.date.fromisoformat(v) is not None


@FORMATS.checks("date-time", raises=ValueError)
def _is_date_time(v: object) -> bool:
    """RFC 3339 (an offset or Z required), checked without optional packages."""
    if not isinstance(v, str):
        return True
    return bool(_DATE_TIME.match(v)) and dt.datetime.fromisoformat(v.replace("z", "Z").replace("t", "T")) is not None


def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=FORMATS)


def to_json(v: Any) -> Any:
    """A value read from parquet as its JSON value: dates and timestamps as ISO 8601 strings."""
    if isinstance(v, dict):
        return {k: to_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_json(x) for x in v]
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def strip_schema(s: Any) -> Any:
    """``s`` without the annotation keywords of :data:`STRIP_KEYS` at any level (property names are kept)."""
    if isinstance(s, list):
        return [strip_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k in STRIP_KEYS:
            continue
        if k in NAMED_SCHEMAS and isinstance(v, dict):
            out[k] = {name: strip_schema(sub) for name, sub in v.items()}
        elif k in DATA_KEYS:
            out[k] = v
        else:
            out[k] = strip_schema(v)
    return out


def canonical(s: Any, key: str = "") -> Any:
    """``s`` for a structural comparison: lists under :data:`SET_KEYS` sorted (compared as sets); dicts compare
    without key order anyway."""
    if isinstance(s, dict):
        return {k: canonical(v, k) for k, v in s.items()}
    if isinstance(s, list):
        items = [canonical(x) for x in s]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True)) if key in SET_KEYS else items
    return s


def schema_diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Where canonical schemas ``a`` and ``b`` differ (JSON-pointer-like paths, at most the first 20)."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: {'added' if k not in a else 'removed'}")
            else:
                out += schema_diff(a[k], b[k], f"{path}/{k}")
    elif a != b:
        out.append(f"{path or '/'}: {json.dumps(a)[:80]} -> {json.dumps(b)[:80]}")
    return out[:20]


# ---------------------------------------------------------------- a fast row validator
_FAST_KEYS = frozenset({"$schema", "$id", "title", "description", "examples", "$comment", "type", "properties",
                        "required", "additionalProperties", "items", "enum", "format", "x-arrow", "x-open",
                        "x-values"})


class _Unsupported(Exception):
    pass


_JSON_TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})


def _type_pred(names: list[str], fmt: str | None) -> Callable[[Any], bool]:
    """JSON Schema ``type`` over a value as read from parquet (exact Python types). A native date or timestamp
    stands for its ISO string (:func:`to_json`): a ``string`` of format ``date`` or ``date-time``, respectively."""
    if set(names) - _JSON_TYPES:
        raise _Unsupported(f"type {names!r}")
    null, boolean, integer, number, string, array, obj = (n in names for n in
                                                          ("null", "boolean", "integer", "number", "string", "array",
                                                           "object"))
    stamp, day = string and fmt == "date-time", string and fmt == "date"

    def pred(v: Any) -> bool:
        t = type(v)
        if t is str:
            return string
        if v is None:
            return null
        if t is dict:
            return obj
        if t is list:
            return array
        if t is bool:
            return boolean
        if t is int:
            return integer or number
        if t is float:
            return number or (integer and v.is_integer())
        if t is dt.datetime:
            return stamp
        if t is dt.date:
            return day
        return False

    return pred


def _fast(s: dict[str, Any]) -> Callable[[Any], bool]:
    if not isinstance(s, dict) or set(s) - _FAST_KEYS:
        raise _Unsupported(str(sorted(set(s) - _FAST_KEYS)) if isinstance(s, dict) else "boolean schema")
    checks: list[Callable[[Any], bool]] = []
    fmt = s.get("format")
    if "type" in s:
        checks.append(_type_pred([s["type"]] if isinstance(s["type"], str) else list(s["type"]), fmt))
    if "enum" in s:
        vals = s["enum"]
        if any(isinstance(x, (dict, list)) for x in vals):
            raise _Unsupported("enum of containers")
        plain = {x for x in vals if type(x) is str or x is None}
        rest = [x for x in vals if not (type(x) is str or x is None)]
        checks.append(lambda v: v in plain if (type(v) is str or v is None) else
                      any((type(v) is bool) == (type(x) is bool) and v == x for x in rest))
    if fmt is not None:
        if fmt == "date-time":
            checks.append(lambda v: (v.tzinfo is not None) if type(v) is dt.datetime else
                          (type(v) is not str or _safe(_is_date_time, v)))
        elif fmt == "date":
            checks.append(lambda v: type(v) is not str or _safe(_is_date, v))
        else:
            raise _Unsupported(f"format {fmt!r}")
    if "properties" in s or "required" in s or "additionalProperties" in s:
        props = {k: _fast(v) for k, v in (s.get("properties") or {}).items()}
        req = list(s.get("required") or [])
        addl = s.get("additionalProperties", True)
        if not isinstance(addl, bool):
            raise _Unsupported("additionalProperties schema")

        def obj(v: Any) -> bool:
            if type(v) is not dict:
                return True
            for r in req:
                if r not in v:
                    return False
            for k, x in v.items():
                p = props.get(k)
                if p is None:
                    if not addl:
                        return False
                elif not p(x):
                    return False
            return True

        checks.append(obj)
    if "items" in s:
        it = _fast(s["items"])

        def arr(v: Any) -> bool:
            if type(v) is list:
                for x in v:
                    if not it(x):
                        return False
            return True

        checks.append(arr)
    if not checks:
        return lambda v: True
    if len(checks) == 1:
        return checks[0]
    if len(checks) == 2:
        a, b = checks
        return lambda v: a(v) and b(v)

    def every(v: Any) -> bool:
        for c in checks:
            if not c(v):
                return False
        return True

    return every


def _safe(check: Callable[[object], bool], v: str) -> bool:
    try:
        return check(v)
    except ValueError:
        return False


def fast_validator(schema: dict[str, Any]) -> Callable[[Any], bool] | None:
    """A predicate that accepts a value read from parquet exactly when jsonschema (with :data:`FORMATS`) accepts its
    JSON value (:func:`to_json`), for schemas of the keywords in :data:`_FAST_KEYS`; None for any other schema."""
    try:
        return _fast(schema)
    except _Unsupported:
        return None


# ---------------------------------------------------------------- reading
def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _read_json(p: Path) -> Any:
    return json.loads(p.read_text("utf-8"))


def _field_path(path: Iterable[Any]) -> str:
    """A jsonschema error's path as a field path: list indices as ``[]``."""
    out = ""
    for x in path:
        out += "[]" if isinstance(x, int) else (f".{x}" if out else str(x))
    return out or "(top)"


def _errors(v: Draft202012Validator, value: Any) -> list[tuple[str, str]]:
    return [(_field_path(e.absolute_path), e.message[:160]) for e in v.iter_errors(value)]


def _values(arr: pa.Array, path: str) -> tuple[list[int], list[Any]]:
    """The values at ``path`` (``a.b[].c``; ``[]`` flattens one list level) of a struct array: (row index, value)
    pairs as two lists, NULLs left out."""
    rows = list(range(len(arr)))
    for seg in path.split("."):
        name, flat = seg.replace("[]", ""), seg.count("[]")
        if name:
            arr = pc.struct_field(arr, [name])
        for _ in range(flat):
            parents = pc.list_parent_indices(arr).to_pylist()
            arr = pc.list_flatten(arr)
            rows = [rows[i] for i in parents]
    vals = arr.to_pylist()
    keep = [i for i, x in enumerate(vals) if x is not None]
    return [rows[i] for i in keep], [vals[i] for i in keep]


def _sample(n: int, k: int, seed: int) -> list[int]:
    return sorted(random.Random(seed).sample(range(n), k)) if k < n else list(range(n))


# ---------------------------------------------------------------- the checks
def check_manifest(root: Path, doc: dict[str, Any], rep: Report) -> None:
    sch = root / "release.schema.json"
    if sch.is_file():
        for where, msg in _errors(validator(_read_json(sch)), doc):
            rep.add("manifest", f"release.json {where}", msg)
    elif rep.supported:
        rep.add("manifest", "release.schema.json", "missing")
    listed: dict[str, dict[str, Any]] = {}
    for fe in doc.get("files") or []:
        if not isinstance(fe, dict) or not isinstance(fe.get("path"), str):
            rep.add("manifest", "release.json files", f"an entry without a path: {fe!r}"[:160])
            continue
        listed[fe["path"]] = fe
        p = root / fe["path"]
        if not p.is_file():
            rep.add("manifest", fe["path"], "listed in release.json, missing")
        elif p.stat().st_size != fe.get("bytes") or sha256_file(p) != fe.get("sha256"):
            rep.add("manifest", fe["path"], "size or sha256 differs from release.json")
    hidden = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if p.name.startswith("."))
    rep.ignored += hidden
    on_disk = {p.relative_to(root).as_posix() for p in root.rglob("*")
               if p.is_file() and not p.relative_to(root).parts[0].startswith(".")}
    for extra in sorted(on_disk - set(listed) - {"release.json"}):
        rep.add("manifest", extra, "present but not listed in release.json")
    rep.checks["manifest"] = f"{len(listed)} files listed"


def _sibling(sch: Path) -> Path | None:
    stem = sch.name.removesuffix(".schema.json")
    hits = [sch.with_name(stem + ext) for ext in (".json", ".jsonl", ".parquet") if sch.with_name(stem + ext).is_file()]
    return hits[0] if len(hits) == 1 else None


def check_arrow(js: dict[str, Any], t: pa.DataType, nullable: bool, path: str, out: list[tuple[str, str]]) -> None:
    """The footer's Arrow type ``t`` against schema node ``js``: ``x-arrow``, nullability, struct fields, list items."""
    want = js.get("x-arrow")
    types = js.get("type")
    types = [types] if isinstance(types, str) else list(types or [])
    kind = "struct" if pa.types.is_struct(t) else "list" if pa.types.is_list(t) else str(t)
    if want != kind:
        out.append((path or "(top)", f"x-arrow {want!r}, the parquet footer has {kind!r}"))
        return
    if ("null" in types) != nullable:
        out.append((path or "(top)", f"nullable {nullable} in the footer, the schema says {'null' in types}"))
    if kind == "struct":
        props = js.get("properties") or {}
        names = [f.name for f in t]
        if set(names) != set(props):
            out.append((path or "(top)", f"fields differ: footer only {sorted(set(names) - set(props))}, schema only "
                                         f"{sorted(set(props) - set(names))}"))
        for f in t:
            if f.name in props:
                check_arrow(props[f.name], f.type, f.nullable, f"{path}.{f.name}" if path else f.name, out)
    elif kind == "list":
        check_arrow(js.get("items") or {}, t.value_type, t.value_field.nullable, path + "[]", out)


def check_parquet(root: Path, sch_rel: str, js: dict[str, Any], p: Path, rep: Report, rows: str | int,
                  seed: int) -> None:
    rel = p.relative_to(root).as_posix()
    pf = pq.ParquetFile(p)
    arrow = pf.schema_arrow
    out: list[tuple[str, str]] = []
    check_arrow(js, pa.struct(list(arrow)), False, "", out)
    for where, msg in out:
        rep.add("schemas", f"{rel} footer {where}", msg)
    n = pf.metadata.num_rows
    k = n if rows == "all" else min(int(rows), n)
    pick = set(_sample(n, k, seed)) if k < n else None
    rep.rows_total = n
    rep.rows_mode = "all" if pick is None else f"a seeded sample of {k:,} (seed {seed})"
    fast = fast_validator(js)
    full = validator(js)
    if fast is None:
        rep.notes.append(f"{sch_rel}: keywords beyond the fast path; rows validated with jsonschema")
    failed: dict[str, list[Any]] = {}
    checked, start = 0, 0
    for g in range(pf.num_row_groups):
        size = pf.metadata.row_group(g).num_rows
        idx = range(start, start + size) if pick is None else [i - start for i in range(start, start + size)
                                                               if i in pick]
        start += size
        if pick is not None and not idx:
            continue
        tbl = pf.read_row_group(g)
        if pick is not None:
            tbl = tbl.take(pa.array(list(idx), pa.int64()))
        for r in tbl.to_pylist():
            checked += 1
            ok = fast(r) if fast is not None else full.is_valid(to_json(r))
            if ok:
                continue
            rid = r.get("doc_id") if isinstance(r, dict) else None
            for where, msg in _errors(full, to_json(r)) or [("(top)", "the fast check failed; jsonschema passes")]:
                e = failed.setdefault(where, [0, rid, msg])
                e[0] += 1
    rep.rows_checked = checked
    for where, (cnt, rid, msg) in sorted(failed.items()):
        rep.add("rows", f"{rel} {where}", f"{cnt:,} row(s), e.g. doc_id {rid}: {msg}")


def check_schemas(root: Path, rep: Report, rows: str | int, seed: int) -> dict[str, Any]:
    """Every ``*.schema.json``: a valid 2020-12 schema; its sibling validates (module docstring). Returns the schemas
    by path."""
    found: dict[str, Any] = {}
    for sch in sorted(root.rglob("*.schema.json")):
        rel = sch.relative_to(root).as_posix()
        if rel.split("/")[0].startswith("."):
            continue
        try:
            js = _read_json(sch)
            Draft202012Validator.check_schema(js)
        except Exception as exc:  # noqa: BLE001 - any unreadable or invalid schema is a problem of the release
            rep.add("schemas", rel, f"not a valid JSON Schema: {' '.join(str(exc).split())[:160]}")
            continue
        found[rel] = js
        sib = _sibling(sch)
        if sib is None:
            rep.add("schemas", rel, "no single sibling <stem>.json, .jsonl or .parquet")
            continue
        if sib.name == "release.json":
            continue  # check_manifest
        srel = sib.relative_to(root).as_posix()
        if sib.suffix == ".parquet":
            check_parquet(root, rel, js, sib, rep, rows, seed)
            continue
        v = validator(js)
        try:
            if sib.suffix == ".json":
                errs = [(srel, w, m) for w, m in _errors(v, _read_json(sib))]
            else:
                errs = []
                for i, line in enumerate(sib.read_text("utf-8").splitlines(), 1):
                    errs += [(f"{srel} line {i}", w, m) for w, m in _errors(v, json.loads(line))]
        except (OSError, ValueError) as exc:
            rep.add("schemas", srel, f"unreadable: {' '.join(str(exc).split())[:160]}")
            continue
        for where, w, m in errs[:20]:
            rep.add("schemas", f"{where} {w}", m)
        if len(errs) > 20:
            rep.add("schemas", srel, f"{len(errs) - 20} more schema errors")
    rep.checks["schemas"] = f"{len(found)} schema files"
    if rep.supported:
        for need in SCHEMA_FILES_1:
            if need not in found and not (root / need).is_file():
                rep.add("schemas", need, "missing")
    return found


# -- format 1: ids, configs, overrides
#: Format 1: the override kinds by what their target and key are.
ANNOTATION_KINDS = ("scope_decision", "stand_in_annotation", "annotation_replacement", "quarantine")
OVERRIDE_TARGETS = {"identity_merge": "identity", "identity_separation": "identity", "metadata_fix": "fields",
                    "manual_source": "manual_sources"}


def _key_ok(kind: str, key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if kind in ("identity_merge", "identity_separation"):
        try:
            ids = json.loads(key)
        except ValueError:
            return False
        return isinstance(ids, list) and len(ids) >= 2 and all(isinstance(i, str) and DOC_ID.match(i) for i in ids)
    if kind == "manual_source":
        return bool(re.match(r"^[a-z_]+=\S", key))
    return bool(DOC_ID.match(key))


def check_ids(root: Path, rep: Report) -> None:
    """Format 1 full-scan checks (module docstring, ids)."""
    try:
        cfgs = _read_json(root / "config" / "configs.json")
        lines = [json.loads(x) for x in (root / "config" / "overrides.jsonl").read_text("utf-8").splitlines()]
    except (OSError, ValueError) as exc:
        rep.add("ids", "config/", f"cannot read configs or overrides: {' '.join(str(exc).split())[:160]}")
        return
    cfg_ids = {c.get("config_id") for c in cfgs if isinstance(c, dict)}
    ov = {}
    for o in lines:
        if not isinstance(o, dict):
            continue
        oid = o.get("override_id")
        if oid in ov:
            rep.add("ids", "config/overrides.jsonl", f"override id {oid!r} on more than one line")
        ov[oid] = o
        kind, target = o.get("kind"), o.get("target")
        if kind in ANNOTATION_KINDS:
            good_target = isinstance(target, str) and target not in OVERRIDE_TARGETS.values()
        else:
            good_target = OVERRIDE_TARGETS.get(kind) == target
        if not good_target:
            rep.add("ids", f"config/overrides.jsonl {oid}", f"target {target!r} does not fit kind {kind!r}")
        if not _key_ok(kind, o.get("key")):
            rep.add("ids", f"config/overrides.jsonl {oid}", f"key {o.get('key')!r} is not the shape kind {kind!r} "
                                                             "takes")
    for c in cfgs:
        cid = c.get("config_id", "") if isinstance(c, dict) else ""
        if cid.startswith("override:"):
            ids = c.get("override_ids") or []
            if cid != "override:" + "+".join(ids) or set(ids) - set(ov):
                rep.add("ids", f"config/configs.json {cid}", "override_ids do not resolve in config/overrides.jsonl")
    pf = pq.ParquetFile(root / "data" / "corpus.parquet")
    ann_t = pf.schema_arrow.field("annotations").type
    kind_cols = {f.name for f in ann_t if pa.types.is_struct(f.type)}
    #: each kind column, and ``other[]`` (records without a column of their own) as a list level
    kinds = [f.name if pa.types.is_struct(f.type) else f"{f.name}[]" for f in ann_t]
    for o in ov.values():
        if o.get("kind") in ANNOTATION_KINDS and o.get("target") not in kind_cols:
            rep.add("ids", f"config/overrides.jsonl {o.get('override_id')}",
                    f"target {o.get('target')!r} is not an annotation kind of this release")
    seen: set[str] = set()
    dupes: set[str] = set()
    bad_cfg: dict[str, int] = {}
    bad_ref: dict[tuple[str, str], int] = {}
    for g in range(pf.num_row_groups):
        t = pf.read_row_group(g, columns=["doc_id", "identity", "annotations"])
        for d in t.column("doc_id").to_pylist():
            (dupes if d in seen else seen).add(d)
        ann = t.column("annotations").combine_chunks()
        ident = t.column("identity").combine_chunks()
        refs: list[tuple[str, Any]] = []
        for k in kinds:
            for cid in _values(ann, f"{k}.metadata.config_id")[1]:
                if cid not in cfg_ids:
                    bad_cfg[cid] = bad_cfg.get(cid, 0) + 1
            refs += [(f"annotations.{k.removesuffix('[]')}", x)
                     for x in _values(ann, f"{k}.metadata.override_ids[]")[1]]
        refs += [("identity.override_ids", x) for x in _values(ident, "override_ids[]")[1]]
        refs += [("identity.members[].override_id", x) for x in _values(ident, "members[].override_id")[1]]
        for where, oid in refs:
            o = ov.get(oid)
            if o is None:
                why = "no line of config/overrides.jsonl"
            elif where.startswith("annotations."):
                why = "" if o.get("target") == where.split(".")[1] else f"a {o.get('kind')} line of target " \
                                                                         f"{o.get('target')!r}"
            elif where == "identity.override_ids":
                why = "" if o.get("kind") in ("identity_merge", "identity_separation", "metadata_fix") else \
                    f"a {o.get('kind')} line"
            else:
                why = "" if o.get("kind") == "identity_merge" else f"a {o.get('kind')} line"
            if why:
                bad_ref[(where, why)] = bad_ref.get((where, why), 0) + 1
    if dupes:
        rep.add("ids", "doc_id", f"{len(dupes)} ids on more than one row, e.g. {sorted(dupes)[:3]}")
    for cid, n in sorted(bad_cfg.items()):
        rep.add("ids", "annotations.<kind>.metadata.config_id", f"{cid!r} ({n} records) is not in config/configs.json")
    for (where, why), n in sorted(bad_ref.items()):
        rep.add("ids", where, f"{n} override id reference(s) to {why}")
    rep.checks["ids"] = f"{len(seen):,} doc ids, {len(cfg_ids)} configs, {len(ov)} overrides"


# -- format 1: taxonomies
#: Format 1: the id fields of an annotation kind's payload and the taxonomy they resolve in.
ID_FIELDS_1: dict[str, tuple[str, tuple[str, ...]]] = {
    "safety_techniques": ("techniques", ("techniques[].slug", "extra_techniques[].hint_technique")),
    "model_roles": ("models", ("models[].node_id", "models[].matched_node_id", "variant_models[].base_node_id",
                               "extra_models[].node_id")),
    "model_name_matches": ("models", ("matches[].node_id", "ambiguous[].candidates[]")),
    "affiliation_orgs": ("orgs", ("org_ids[]",)),
    "affiliation": ("orgs", ("items[].affiliations[].org_ids[]", "items[].affiliations[].org_candidates[][]")),
}


def taxonomy_ids(doc: Any) -> set[str]:
    """The ids of a taxonomy file: the catalogue envelope's ``entries[].id``; the earlier layouts' technique and
    group-node slugs, or model node keys."""
    if isinstance(doc.get("entries"), list):
        return {e["id"] for e in doc["entries"]}
    if isinstance(doc.get("techniques"), list):
        return {t["slug"] for t in doc["techniques"]} | {g["slug"] for g in doc.get("group_nodes") or []}
    if isinstance(doc.get("nodes"), dict):
        return set(doc["nodes"])
    raise ValueError("neither the catalogue envelope nor an earlier techniques or models layout")


def _taxonomy_validator() -> Any:
    if DATA_REPO is None or not (TOOLS / "taxonomy-schema-validator.py").is_file():
        return None
    spec = importlib.util.spec_from_file_location("taxonomy_schema_validator", TOOLS / "taxonomy-schema-validator.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_taxonomies(root: Path, doc: dict[str, Any], rep: Report) -> None:
    pins = {(name, t.get("version")): t for name, ts in (doc.get("taxonomies") or {}).items()
            for t in (ts if isinstance(ts, list) else [ts]) if isinstance(t, dict)}
    files: dict[tuple[str, str], set[str]] = {}
    for (name, ver), t in sorted(pins.items()):
        p = root / str(t.get("path"))
        if not p.is_file():
            rep.add("taxonomies", str(t.get("path")), f"pinned {name} {ver}: missing")
            continue
        if sha256_file(p) != t.get("sha256"):
            rep.add("taxonomies", str(t.get("path")), f"pinned {name} {ver}: sha256 differs from release.json")
        try:
            data = yaml.load(p.read_text("utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            files[(name, ver)] = taxonomy_ids(data)
        except (OSError, ValueError, yaml.YAMLError, KeyError, TypeError) as exc:
            rep.add("taxonomies", str(t.get("path")), f"unreadable: {' '.join(str(exc).split())[:160]}")
            continue
        if str(data.get("version")) != str(ver) and not str(ver).startswith(str(data.get("version")) + "."):
            rep.add("taxonomies", str(t.get("path")), f"version field {data.get('version')!r}, pinned as {ver}")
    tv = _taxonomy_validator()
    if tv is None:
        rep.not_checked.append("taxonomy files against their schemas: run from a checkout of the data repository "
                               "(tools/taxonomy-schema-validator.py)")
    else:
        for (name, _v), t in sorted(pins.items()):
            p = root / str(t.get("path"))
            if p.is_file():
                for e in tv.problems(p, schema_dir=DATA_REPO / "taxonomies" / name / "schemas"):
                    rep.add("taxonomies", str(t.get("path")), e)
    try:
        cfgs = {c["config_id"]: c for c in _read_json(root / "config" / "configs.json")}
    except (OSError, ValueError, KeyError, TypeError):
        return  # check_ids reports it
    pf = pq.ParquetFile(root / "data" / "corpus.parquet")
    kinds = {f.name for f in pf.schema_arrow.field("annotations").type}
    bad: dict[tuple[str, str, str], list[Any]] = {}
    unpinned: dict[str, int] = {}
    resolved = 0
    for g in range(pf.num_row_groups):
        ann = pf.read_row_group(g, columns=["doc_id", "annotations"])
        doc_ids = ann.column("doc_id").to_pylist()
        a = ann.column("annotations").combine_chunks()
        orgs_ver = pc.struct_field(a, ["affiliation_orgs", "payload", "vocab_version"]).to_pylist() \
            if "affiliation_orgs" in kinds else [None] * len(doc_ids)
        for kind, (tax, paths) in ID_FIELDS_1.items():
            if kind not in kinds:
                continue
            cids = pc.struct_field(a, [kind, "metadata", "config_id"]).to_pylist()
            tver = pc.struct_field(a, [kind, "payload", "taxonomy_version"]).to_pylist() \
                if kind == "safety_techniques" else None
            for path in paths:
                rows, vals = _values(a, f"{kind}.payload.{path}")
                for r, v in zip(rows, vals, strict=True):
                    pin = (cfgs.get(cids[r]) or {}).get("taxonomy") or {}
                    if kind == "affiliation":
                        ver = orgs_ver[r]
                    elif pin.get("name") == tax:
                        ver = pin.get("version")
                        if tver is not None and tver[r] != ver:
                            bad.setdefault((kind, "payload.taxonomy_version", f"{tver[r]} under a {ver} pin"),
                                           [0, doc_ids[r]])[0] += 1
                    elif tver is not None and tver[r]:
                        ver = tver[r]
                    else:
                        unpinned[kind] = unpinned.get(kind, 0) + 1
                        continue
                    ids = files.get((tax, ver))
                    if ids is None:
                        bad.setdefault((kind, path, f"{tax} {ver} is not shipped"), [0, doc_ids[r]])[0] += 1
                    elif v not in ids:
                        bad.setdefault((kind, path, f"{v!r} is not an id of {tax} {ver}"), [0, doc_ids[r]])[0] += 1
                    else:
                        resolved += 1
    for (kind, path, why), (n, d) in sorted(bad.items())[:50]:
        rep.add("taxonomies", f"annotations.{kind}.payload.{path}", f"{why}: {n} value(s), e.g. doc_id {d}")
    for kind, n in sorted(unpinned.items()):
        rep.notes.append(f"taxonomies: {n} id value(s) of {kind} records whose config pins no taxonomy (override "
                         "configs) not resolved")
    rep.checks["taxonomies"] = f"{len(pins)} files pinned; {resolved:,} id values resolved"


# -- the archive
def check_archive(root: Path, version: str, schemas: dict[str, Any], archive: Path | None, require: bool,
                  rep: Report) -> None:
    mm = ".".join(version.split(".")[:2])
    d = archive / mm if archive is not None else None
    if d is None or not d.is_dir():
        where = f"at {archive}" if archive else "in reach (not run from a checkout of the data repository)"
        msg = f"archive: no formats/{mm}/ {where}"
        if require:
            rep.add("archive", f"formats/{mm}", msg)
        else:
            rep.not_checked.append(msg)
        return
    for rel in SCHEMA_FILES_1:
        a = d / rel
        if rel not in schemas:
            continue  # check_schemas reports a missing schema
        if not a.is_file():
            rep.add("archive", f"formats/{mm}/{rel}", "not in the archive")
            continue
        diff = schema_diff(canonical(_read_json(a)), canonical(strip_schema(schemas[rel])))
        if diff:
            rep.add("archive", rel, f"differs from formats/{mm}/{rel}: " + "; ".join(diff[:5]))
    rep.checks["archive"] = f"formats/{mm}/ compared"


# ---------------------------------------------------------------- the entry point
def check_release(path: str | Path, *, rows: str | int = DEFAULT_SAMPLE, seed: int = 0,
                  archive: str | Path | None | bool = True, require_archive: bool = False) -> Report:
    """Check the release directory ``path`` (module docstring). ``rows``: ``"all"`` or a sample size. ``archive``:
    the ``formats/`` directory to compare the schemas with (True: this repository's, when this file sits in one;
    False/None: none); ``require_archive``: a missing archive directory is a problem, not a check left out."""
    root = Path(path)
    rep = Report(str(path))
    try:
        doc = _read_json(root / "release.json")
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        rep.add("manifest", "release.json", f"unreadable: {' '.join(str(exc).split())[:160]}")
        rep.not_checked.append("everything else: no release.json")
        return rep
    v = doc.get("format_version")
    rep.format_version = v if isinstance(v, str) else None
    m = VERSION.match(v) if isinstance(v, str) else None
    rep.supported = m is not None and int(m.group(1)) in SUPPORTED_MAJORS
    if not rep.supported:
        rep.not_checked.append(f"the format-specific checks (ids, taxonomies, archive): format_version {v!r} is not "
                               f"of a format this checker knows (majors {', '.join(map(str, SUPPORTED_MAJORS))})")
    _guarded(rep, "manifest", check_manifest, root, doc, rep)
    schemas = _guarded(rep, "schemas", check_schemas, root, rep, rows, seed) or {}
    if not rep.supported:
        return rep
    if (root / "data" / "corpus.parquet").is_file():
        _guarded(rep, "ids", check_ids, root, rep)
        _guarded(rep, "taxonomies", check_taxonomies, root, doc, rep)
    arch = (DATA_REPO / "formats" if DATA_REPO is not None else None) if archive is True else \
        (Path(archive) if archive else None)
    _guarded(rep, "archive", check_archive, root, str(v), schemas, arch, require_archive, rep)
    return rep


def _guarded(rep: Report, check: str, fn: Callable[..., Any], *args: Any) -> Any:
    """``fn(*args)``; a crash (an unreadable or malformed file the check did not expect) is a problem of the release,
    reported with the error, not a traceback."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        rep.add(check, "(check stopped)", f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}")
        return None


# ---------------------------------------------------------------- the archive in this repository (--formats)
def changelog_entries(text: str) -> dict[str, str | None]:
    """``formats/CHANGELOG.md``: version -> its date (None: undated)."""
    return {m.group(1): m.group(2) for m in CHANGELOG_ENTRY.finditer(text)}


def check_formats(formats: Path, since: str | None = None) -> list[str]:
    """The archive (module docstring, ``--formats``); a list of problems."""
    probs: list[str] = []
    log_p = formats / "CHANGELOG.md"
    entries = changelog_entries(log_p.read_text("utf-8")) if log_p.is_file() else {}
    if not log_p.is_file():
        probs.append("formats/CHANGELOG.md: missing")
    dirs = sorted(p for p in formats.iterdir() if p.is_dir())
    for d in dirs:
        mm = d.name
        if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", mm):
            probs.append(f"formats/{mm}: not a <major>.<minor> directory")
            continue
        if not any(v.rsplit(".", 1)[0] == mm for v in entries):
            probs.append(f"formats/{mm}: no entry [{mm}.<patch>] in formats/CHANGELOG.md")
        have = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
        for rel in sorted(set(SCHEMA_FILES_1) ^ have):
            what = "missing" if rel in SCHEMA_FILES_1 else "not a schema file of the format"
            probs.append(f"formats/{mm}/{rel}: {what}")
        for rel in sorted(set(SCHEMA_FILES_1) & have):
            try:
                js = _read_json(d / rel)
                Draft202012Validator.check_schema(js)
            except Exception as exc:  # noqa: BLE001
                probs.append(f"formats/{mm}/{rel}: not a valid JSON Schema: {' '.join(str(exc).split())[:160]}")
                continue
            if js.get("$id") != f"{ID_PREFIX}:{mm}:{rel}":
                probs.append(f"formats/{mm}/{rel}: $id {js.get('$id')!r}, expected {ID_PREFIX}:{mm}:{rel}")
            if strip_schema(js) != js:
                probs.append(f"formats/{mm}/{rel}: keeps description, title, examples or $comment")
    if since:
        probs += _edited_since(formats, since)
    return probs


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _edited_since(formats: Path, ref: str) -> list[str]:
    """Files of ``formats/<x.y>/`` changed (modified, deleted, renamed) since ``ref`` where ``ref``'s changelog dated
    an entry of ``<x.y>``: a dated version's directory is never edited."""
    top = Path(_git("rev-parse", "--show-toplevel", cwd=formats).strip())
    rel = formats.resolve().relative_to(top).as_posix()
    try:
        old_log = _git("show", f"{ref}:{rel}/CHANGELOG.md", cwd=top)
    except subprocess.CalledProcessError:
        old_log = ""
    dated = {v.rsplit(".", 1)[0] for v, d in changelog_entries(old_log).items() if d}
    out = []
    for line in _git("diff", "--name-status", "--diff-filter=MDRT", ref, "--", rel, cwd=top).splitlines():
        status, path = line.split("\t")[0], line.split("\t")[1]
        m = re.match(rf"^{re.escape(rel)}/((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))/", path)
        if m and m.group(1) in dated:
            out.append(f"{path}: changed since {ref} ({status}), but format {m.group(1)} was dated there; an archived "
                       "format is never edited")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check a release of The Safety Stack dataset against its format.")
    ap.add_argument("release", nargs="?", type=Path, help="the release directory (holding release.json)")
    ap.add_argument("--rows", default=str(DEFAULT_SAMPLE), help="rows to validate: a sample size or 'all'")
    ap.add_argument("--seed", type=int, default=0, help="the sample's seed")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--archive", type=Path, help="the formats/ directory to compare the release's schemas with "
                                                 "(default: this repository's, when run from a checkout)")
    ap.add_argument("--formats", action="store_true", help="check the archive of formats instead of a release")
    ap.add_argument("--formats-dir", type=Path, help="with --formats: the formats/ directory (default: this "
                                                     "repository's)")
    ap.add_argument("--since", help="with --formats: a git ref; no dated format's file may have changed since")
    a = ap.parse_args(argv)
    if a.formats:
        fdir = a.formats_dir or (DATA_REPO / "formats" if DATA_REPO else None)
        if fdir is None or a.release is not None:
            ap.error("--formats: give no release directory, and --formats-dir when not run from the data repository")
        probs = check_formats(fdir, a.since)
        print("\n".join([*probs, f"{'FAIL' if probs else 'ok'}: {fdir} ({len(probs)} problem(s))"]))
        return EXIT_INVALID if probs else EXIT_OK
    if a.release is None:
        ap.error("give a release directory, or --formats")
    if a.rows != "all" and not a.rows.isdigit():
        ap.error("--rows: a sample size or 'all'")
    rep = check_release(a.release, rows="all" if a.rows == "all" else int(a.rows), seed=a.seed,
                        archive=a.archive if a.archive is not None else True)
    print(json.dumps(rep.as_dict(), indent=2) if a.json else rep.text())
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
