"""S1-S10: the write-time schema checks (Appendix B, D45). The parquet writer
(:func:`stack.store.parquet.write_family`) runs them and refuses a file on any finding; ``stack
verify`` re-runs them over every ``data/`` file (:mod:`stack.checks.verify`).

Each check is ``(table, header, spec) -> list[Finding]``: ``header`` is the ``stack.*`` header
without its prefix, ``spec`` the family's :class:`~stack.schemas.families.FamilySpec`.
:func:`run_schema_checks` runs S1 first and stops there on a schema mismatch, since S2-S7 read
columns S1 has not vouched for. S8-S10 (T1.4) read the ``documents``/``corpus`` row invariants
of §2.5 (:mod:`stack.states`) and are empty for every other family.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

import pyarrow as pa
import pyarrow.compute as pc

from stack.checks import Finding, register
from stack.checks.rows import iter_values, row_slices
from stack.schemas.blocks import BLOCK_NAMES
from stack.schemas.families import ID_FORMATS, FamilySpec, ids_digest
from stack.schemas.payloads.registry import REGISTRY as PAYLOADS
from stack.schemas.upcasters import registration_problem
from stack.schemas.vocab import (
    ALL_ERROR_CLASSES,
    DOCUMENT_STATES,
    ERROR_CLASSES,
    FOLD_CONFIGS,
    STATUS,
    STATUS_NO_ERROR,
    VERSION_KINDS,
)
from stack.states import DIGEST_AGREEMENTS, REQUIRED_BLOCKS, TRIAGE_BASIS_KIND
from stack.store.layout import (
    CHAIN_STAGES,
    FOLD_HEADER_KEYS,
    HEADER_KEYS,
    LEGACY_SCHEMA_VERSION,
    NONE,
    SCHEMA_VERSIONS,
    WILDCARD,
    canonical_key_header,
)

Header = dict[str, str]
_EXAMPLES = 5


# ---------------------------------------------------------------- paths
def _top(table: pa.Table, name: str) -> pa.Array:
    """The top-level column ``name`` as one array: what ``struct_field`` of the table's row struct
    (no NULL rows) gives, without copying the other columns (round 5 S3b)."""
    return table.column(name).combine_chunks()


def _present(arr: pa.Array) -> pa.Array:
    return arr.filter(arr.is_valid()) if arr.null_count else arr


def resolve(table: pa.Table, path: str) -> pa.Array:
    """The values at ``path`` (``a.b[].c``) over every present position: entries under a NULL
    struct or list are dropped, lists are flattened. Two paths with the same container prefix
    resolve to aligned arrays."""
    arr: pa.Array | None = None
    for seg in path.split("."):
        is_list = seg.endswith("[]")
        name = seg.removesuffix("[]")
        arr = _top(table, name) if arr is None else pc.struct_field(_present(arr), name)
        if is_list:
            arr = _present(arr).flatten()
    return arr


def resolve_rows(table: pa.Table, path: str) -> pa.Array:
    """The value at a struct path (no ``[]``) aligned with the table's rows; NULL where any
    ancestor struct is NULL."""
    first, *rest = path.split(".")
    arr = _top(table, first)
    for seg in rest:
        arr = pc.struct_field(arr, seg)
    return arr


def _examples(values: list[str]) -> tuple[str, ...]:
    return tuple(values[:_EXAMPLES])


# ---------------------------------------------------------------- S1
@register("S1", "schema_equal")
def schema_equal(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S1: the table's schema equals the registry's (names, order, types, nullability, nested)."""
    got, want = table.schema, spec.schema
    if got.equals(want, check_metadata=False):
        return []
    items: list[str] = []
    if got.names != want.names:
        items.append(f"columns {got.names} <> registry {want.names}")
    for f in want:
        if f.name in got.names and not got.field(f.name).equals(f):
            items.append(f"{f.name}: {got.field(f.name)} <> registry {f}")
    return [Finding("schema_equal", "hard", f"{spec.name}: schema differs from the registry",
                    max(len(items), 1), _examples(items))]


# ---------------------------------------------------------------- S2
def _nulls(arr: pa.Array, f: pa.Field, path: str, out: list[tuple[str, int]]) -> None:
    if not f.nullable and arr.null_count:
        out.append((path, arr.null_count))
    t = f.type
    if pa.types.is_list(t):
        _nulls(_present(arr).flatten(), t.value_field, path + "[]", out)
    elif pa.types.is_struct(t):
        present = _present(arr)
        for sub in t:
            _nulls(pc.struct_field(present, sub.name), sub, f"{path}.{sub.name}", out)


@register("S2", "nonnull")
def nonnull(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S2: no NULL in a non-nullable (``!``) field at any depth; a field under a NULL parent
    struct or list is not present and so not checked."""
    out: list[tuple[str, int]] = []
    for f in spec.schema:
        _nulls(table.column(f.name).combine_chunks(), f, f.name, out)
    return [Finding("nonnull", "hard", f"{spec.name}.{p}: {n} NULL(s) in a non-nullable field", n,
                    (p,)) for p, n in out]


# ---------------------------------------------------------------- S3
@register("S3", "key_unique")
def key_unique(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S3: the family key is non-NULL and unique within the file."""
    out: list[Finding] = []
    for k in spec.key:
        if n := table.column(k).null_count:
            out.append(Finding("key_unique", "hard", f"{spec.name}.{k}: {n} NULL key value(s)", n,
                               (k,)))
    counts = table.select(list(spec.key)).group_by(list(spec.key)).aggregate([([], "count_all")])
    dup = counts.filter(pc.greater(counts.column("count_all"), 1))
    if dup.num_rows:
        ex = [repr(tuple(r[k] for k in spec.key)) for r in dup.slice(0, _EXAMPLES).to_pylist()]
        out.append(Finding("key_unique", "hard",
                           f"{spec.name}: {dup.num_rows} duplicate key(s) on {spec.key}",
                           dup.num_rows, tuple(ex)))
    return out


# ---------------------------------------------------------------- S4
def pairing_error(status: str | None, error_class: str | None) -> str | None:
    """Why ``(status, error_class)`` breaks §2.6's pairing, or ``None`` if it holds."""
    if status is None:
        return None if error_class is None else "error_class without a status"
    if status not in STATUS:
        return f"status {status!r} not in the vocabulary"
    if status in STATUS_NO_ERROR:
        return None if error_class is None else f"{status} with error_class {error_class!r}"
    allowed = ERROR_CLASSES.get(status)
    if allowed is None:  # partial: any listed class or none
        return None if error_class is None or error_class in ALL_ERROR_CLASSES else (
            f"{status} with unlisted error_class {error_class!r}")
    if error_class is None:
        return f"{status} without an error_class"
    return None if error_class in allowed else f"{status} with error_class {error_class!r}"


@register("S4", "status_pairing")
def status_pairing(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S4: every status/error_class pair of the family (top level and nested) follows §2.6:
    error_class NULL for ok/empty, required and from the status's set for skipped, failed_*,
    quarantined, not_attempted."""
    out: list[Finding] = []
    for sp, ep in spec.status_pairs:
        st, er = resolve(table, sp), resolve(table, ep)
        bad = [f"{why}" for s, e in zip(iter_values(st), iter_values(er), strict=True)
               if (why := pairing_error(s, e))]
        if bad:
            out.append(Finding("status_pairing", "hard",
                               f"{spec.name}.{sp}: {len(bad)} status/error_class out of pairing",
                               len(bad), _examples(bad)))
    return out


# ---------------------------------------------------------------- S5
@register("S5", "nested_unique")
def nested_unique(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S5: entries of the family's nested lists are unique per row on their keys (roles per
    ``(source_id, content_role)``, snapshots, members, in-band entries, triage questions)."""
    out: list[Finding] = []
    for list_path, keys in spec.nested_unique:
        dups: list[tuple] = []
        for start, part in row_slices(table):  # a row's entries never straddle two slices
            lists = resolve_rows(part, list_path.removesuffix("[]"))
            parents = [start + p for p in pc.list_parent_indices(lists).to_pylist()]
            elems = lists.flatten()
            cols = []
            for k in keys:
                arr = elems
                for seg in k.split("."):
                    arr = pc.struct_field(arr, seg)
                cols.append(arr.to_pylist())
            counts = Counter(zip(parents, *cols))
            dups += [k for k, c in counts.items() if c > 1]
        if dups:
            ex = [f"row {d[0]}: {dict(zip(keys, d[1:]))}" for d in dups]
            out.append(Finding("nested_unique", "hard",
                               f"{spec.name}.{list_path}: {len(dups)} duplicated {keys}",
                               len(dups), _examples(ex)))
    return out


# ---------------------------------------------------------------- S6
@register("S6", "id_formats")
def id_formats(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S6: 64-hex digests, minted ids in ``stack.core.ids`` shape (source ids ``s…``, run ids
    ``r…``, evidence ids any kind letter), snapshot ids ``<run_id>.<seq>`` (FamilySpec.id_paths)."""
    out: list[Finding] = []
    for path, fmt in spec.id_paths:
        rx = ID_FORMATS[fmt]
        wrong = [v for v in iter_values(resolve(table, path)) if v is not None and not rx.fullmatch(v)]
        if wrong:
            out.append(Finding("id_formats", "hard",
                               f"{spec.name}.{path}: {len(wrong)} malformed {fmt}", len(wrong),
                               _examples([repr(v) for v in wrong])))
    return out


# ---------------------------------------------------------------- S7
def _is_json(v: str) -> bool:
    try:
        json.loads(v)
    except ValueError:
        return False
    return True


def header_problems(table: pa.Table, header: Header, spec: FamilySpec) -> list[str]:
    """Every way ``header`` breaks A.6/D14/D15 for ``table`` of family ``spec``."""
    bad: list[str] = []
    legacy = header.get("schema_version") == LEGACY_SCHEMA_VERSION
    missing = [k for k in HEADER_KEYS if k not in header and not (legacy and k == "family_version")]
    if missing:
        bad.append(f"missing keys {missing}")
    bad += [f"{k}: value is {type(v).__name__}, not a string" for k, v in header.items()
            if not isinstance(v, str)]
    if bad:
        return bad
    h = header
    if h["schema_version"] not in SCHEMA_VERSIONS:
        bad.append(f"schema_version = {h['schema_version']!r}, expected one of {list(SCHEMA_VERSIONS)}")
    elif legacy and "family_version" in h:
        bad.append(f"family_version on a schema_version {LEGACY_SCHEMA_VERSION!r} file (a legacy file has none)")
    elif not legacy and h["family_version"] != str(spec.version):
        bad.append(f"family_version = {h['family_version']!r}, expected {str(spec.version)!r}")
    unregistered = registration_problem(spec.name, spec.schema_digest)
    if unregistered is not None:
        bad.append(unregistered)
    expect = {
        "family": spec.name,
        "key": canonical_key_header(spec.key),
        "schema_digest": spec.schema_digest,
        "row_count": str(table.num_rows),
        "file_id": f"{h['run_id']}.{spec.name}",
        "ids_digest": ids_digest(table, spec.key),
    }
    bad += [f"{k} = {h[k]!r}, expected {v!r}" for k, v in expect.items() if h[k] != v]
    if not ID_FORMATS["run_id"].fullmatch(h["run_id"]):
        bad.append(f"run_id {h['run_id']!r} is not a minted run id")
    for k in ("stage", "writer", "plan", "config_digest", "finished_at", "code_digest", "chain_id"):
        if not h[k]:
            bad.append(f"{k} is empty")
    if h["chain_id"] == NONE:
        bad.append("chain_id is 'none'")
    if h["kind"] not in VERSION_KINDS:
        bad.append(f"kind {h['kind']!r} not in {VERSION_KINDS}")
    if h["kind"] == "part" and not h["base"]:
        bad.append("a part file with no base (a part file extends its directory's last file)")
    if h["base"] and h["base"] != h["after"]:
        bad.append(f"base {h['base']!r} is not after {h['after']!r}")
    if spec.kind in ("wf", "corpus", "evidence"):  # always full (D14, D54, D62)
        if h["kind"] != "full":
            bad.append(f"{spec.kind} file is always full: kind = {h['kind']!r}")
        if h["chain_id"].split(".", 1)[0] != spec.name:  # D372: data/<family>[.<sub>]/ (D374: corpus.build)
            bad.append(f"chain_id {h['chain_id']!r} is not a {spec.name} directory")
    if h["scope_complete"] not in ("true", "false"):
        bad.append(f"scope_complete {h['scope_complete']!r} not 'true'/'false'")
    bad += [f"{k} is not JSON: {h[k]!r}" for k in ("inputs", "prior", "only") if not _is_json(h[k])]
    stage = h["stage"]
    if stage in CHAIN_STAGES:
        try:
            blocks = json.loads(h["blocks_written"])
            if not isinstance(blocks, list) or not set(blocks) <= set(BLOCK_NAMES):
                bad.append(f"blocks_written {h['blocks_written']!r} is not a list of {BLOCK_NAMES}")
        except ValueError:
            bad.append(f"blocks_written {h['blocks_written']!r} is not a JSON list ({stage} file)")
    elif h["blocks_written"] != WILDCARD:
        bad.append(f"blocks_written {h['blocks_written']!r} on a {stage} file, expected '*'")
    if spec.kind == "wf":
        assert spec.workflow is not None
        want = PAYLOADS[spec.workflow].basis_kind
        if h["basis_kind"] != want:
            bad.append(f"basis_kind {h['basis_kind']!r}, registry says {want!r}")
    if stage.startswith("fold."):
        if h["fold_config"] != stage.removeprefix("fold.") or h["fold_config"] not in FOLD_CONFIGS:
            bad.append(f"fold_config {h['fold_config']!r} on a {stage} file")
        if not (_is_json(h["annotations_folded"]) and isinstance(json.loads(h["annotations_folded"]), list)):
            bad.append(f"annotations_folded {h['annotations_folded']!r} is not a JSON list")
    else:
        bad += [f"{k} = {h[k]!r} on a non-fold file, expected 'none'" for k in FOLD_HEADER_KEYS
                if h[k] != NONE]
    return bad


@register("S7", "kv_complete")
def kv_complete(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S7: every A.6 key present as a string (D15; ``family_version`` only on ``schema_version
    "2"`` files, where it is the family's current version, D295; a legacy ``"1"`` file has none),
    the family's schema a registered version (:mod:`stack.schemas.upcasters`), the computed ones
    (family, key, schema_digest, row_count, file_id, ids_digest) equal to the file's, and the version keys
    consistent on their own (a part file names its ``base``; a ``base`` equals ``after``;
    ``corpus``, ``<wf>`` and ``<wf>_evidence`` always full in ``data/<family>/``, D14/D62/D372). Chain-level consistency is
    the chain's (V11)."""
    bad = header_problems(table, header, spec)
    return [Finding("kv_complete", "hard", f"{spec.name}: header {b}", 1, (b,)) for b in bad]


# ---------------------------------------------------------------- S8-S10 (documents, corpus)
_ROW_FAMILIES = ("documents", "corpus")


def _doc_ids(table: pa.Table, mask: list[bool]) -> list[str]:
    ids = table.column("doc_id").to_pylist()
    return [d for d, m in zip(ids, mask, strict=True) if m]


@register("S8", "state_blocks")
def state_blocks(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S8: every row's ``state`` is a ``documents`` state and the blocks §2.5 requires for it
    (:data:`stack.states.REQUIRED_BLOCKS`) are non-NULL (``triaged`` ⇒ ``annotations.triage``,
    ``acquired`` ⇒ ``annotations.triage`` and ``content``)."""
    if spec.kind not in _ROW_FAMILIES:
        return []
    paths = sorted({p for ps in REQUIRED_BLOCKS.values() for p in ps})
    n_unknown, unknown = 0, set()
    missing: dict[str, list[str]] = {p: [] for p in paths}
    for _, part in row_slices(table):
        states = part.column("state").to_pylist()
        wrong = [s for s in states if s not in DOCUMENT_STATES]
        n_unknown += len(wrong)
        unknown |= set(map(repr, wrong))
        for path in paths:
            valid = resolve_rows(part, path).is_valid().to_pylist()
            bad = [s in REQUIRED_BLOCKS and path in REQUIRED_BLOCKS[s] and not v
                   for s, v in zip(states, valid, strict=True)]
            if any(bad):
                missing[path] += _doc_ids(part, bad)
    out: list[Finding] = []
    if n_unknown:
        out.append(Finding("state_blocks", "hard",
                           f"{spec.name}: {n_unknown} row(s) with a state outside "
                           f"{DOCUMENT_STATES}", n_unknown, _examples(sorted(unknown))))
    for path in paths:
        if ids := missing[path]:
            out.append(Finding("state_blocks", "hard",
                               f"{spec.name}: {len(ids)} row(s) without the required {path}",
                               len(ids), _examples([f"{d}: {path} NULL" for d in ids])))
    return out


@register("S9", "digest_agreements")
def digest_agreements(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S9: the digest agreements of §2.5 (:data:`stack.states.DIGEST_AGREEMENTS`): ``triaged``
    and ``acquired`` ⇒ ``annotations.triage.basis_kind = 'fields'`` and ``basis_digest =
    metadata.fields_digest``; ``acquired`` ⇒ ``content.members_digest =
    identity.members_digest``. A row whose block is NULL is S8's, not S9's."""
    if spec.kind not in _ROW_FAMILIES:
        return []
    bad = [b for _, part in row_slices(table) for b in _digest_disagreements(part)]
    if not bad:
        return []
    return [Finding("digest_agreements", "hard",
                    f"{spec.name}: {len(bad)} digest agreement(s) of §2.5 broken", len(bad),
                    _examples(bad))]


def _digest_disagreements(table: pa.Table) -> list[str]:
    """S9's broken agreements over the rows of ``table`` (one slice of the file), in row order."""
    states = table.column("state").to_pylist()
    ids = table.column("doc_id").to_pylist()
    cache: dict[str, list[object]] = {}

    def col(path: str) -> list[object]:
        if path not in cache:
            cache[path] = resolve_rows(table, path).to_pylist()
        return cache[path]

    def present(path: str) -> list[object]:
        key = f"valid:{path}"
        if key not in cache:
            cache[key] = resolve_rows(table, path).is_valid().to_pylist()
        return cache[key]

    bad: list[str] = []
    triage_present = present("annotations.triage")
    for i, st in enumerate(states):
        agreements = DIGEST_AGREEMENTS.get(st, ())
        if not agreements:
            continue
        if triage_present[i] and col("annotations.triage.basis_kind")[i] != TRIAGE_BASIS_KIND:
            bad.append(f"{ids[i]}: {st} with annotations.triage.basis_kind "
                       f"{col('annotations.triage.basis_kind')[i]!r}")
        for left, right in agreements:
            block = left.rsplit(".", 1)[0]
            if not present(block)[i]:
                continue  # S8
            if col(left)[i] != col(right)[i]:
                bad.append(f"{ids[i]}: {st} with {left} <> {right}")
    return bad


@register("S10", "payload_typed")
def payload_typed(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S10: per row, a workflow's annotation is either in its typed field
    (``annotations.<wf>`` non-NULL) or in ``annotations.other[]``, never both."""
    if spec.kind not in _ROW_FAMILIES:
        return []
    ann_type = spec.schema.field("annotations").type
    typed = [f.name for f in ann_type if f.name != "other"]
    by_wf: dict[str, list[str]] = {wf: [] for wf in typed}
    for _, part in row_slices(table):
        ids = part.column("doc_id").to_pylist()
        other = resolve_rows(part, "annotations.other")
        parents = pc.list_parent_indices(other).to_pylist()
        workflows = pc.struct_field(other.flatten(), "workflow").to_pylist()
        in_other: dict[int, set[str]] = {}
        for p, w in zip(parents, workflows, strict=True):
            in_other.setdefault(p, set()).add(w)
        for wf in typed:
            present = resolve_rows(part, f"annotations.{wf}").is_valid().to_pylist()
            by_wf[wf] += [f"{ids[i]}: {wf} typed and in other[]" for i, v in enumerate(present)
                          if v and wf in in_other.get(i, ())]
    bad = [b for wf in typed for b in by_wf[wf]]
    if not bad:
        return []
    return [Finding("payload_typed", "hard",
                    f"{spec.name}: {len(bad)} annotation(s) both typed and in other[]", len(bad),
                    _examples(bad))]


S_CHECKS: tuple[Callable[[pa.Table, Header, FamilySpec], list[Finding]], ...] = (
    schema_equal,
    nonnull,
    key_unique,
    status_pairing,
    nested_unique,
    id_formats,
    kv_complete,
    state_blocks,
    digest_agreements,
    payload_typed,
)


def run_schema_checks(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """S1-S10 over one file's table and header; S1 failing stops the rest."""
    first = schema_equal(table, header, spec)
    if first:
        return first
    return [f for check in S_CHECKS[1:] for f in check(table, header, spec)]
