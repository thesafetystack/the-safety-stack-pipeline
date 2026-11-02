"""Family file formats: per family its version history and the steps between versions (D295).

A family's **format** is its pyarrow schema (:mod:`stack.schemas.families`); its **versions** 1, 2, …
number the formats the family has had, oldest first. :data:`VERSIONS` holds, per family, each
version's number, the family's ``schema_digest`` at that version, the **step** from the previous
version and a one-line note naming what changed and the decision. Every family file's footer says
which version wrote it (``stack.family_version``; a legacy file, ``stack.schema_version = "1"``,
has no such key and is mapped by its ``stack.schema_digest``), and the one family reader
(:func:`stack.store.parquet.read_family`) returns every file **at the current version**: an older
file's rows go through the steps from its version on (:func:`upcast`). So a format change never
forces a rewrite: a chain may hold files of several versions, and writers write current-version
files, full or part, on top of older ones. ``stack migrate upcast`` only materialises the upcast.

A step from version k-1 to k is one of

* :data:`ADDITIVE`: version k only adds nullable fields (top level, inside structs, inside
  ``list<struct>`` items) or relaxes non-null to nullable. Implemented by the generic additive
  conform :func:`upcast_table` (D208 (1)): a missing nullable field reads NULL.
* a function ``pa.Table -> pa.Table`` (version k-1 → k) for a change that is not additive (a
  rename, a type change, a split …).
* :class:`Incompatible` ``(reason)``: the change cannot be upcast; reading an older file fails
  loudly with ``reason`` (which names the decision and the remedy: ``stack migrate …``, or
  re-run the stage).

**Applying a path** (:func:`upcast`): the steps from the file's version to the current one run in
order, ``ADDITIVE`` steps being no-ops on the way; **one additive conform to the current schema
runs at the end**; the result must then equal the family's current schema exactly, else
:class:`UpcastBug` names the file, its version and the differing paths (a bug in a step).

**Contract for step functions.** (1) A field added by an earlier ``ADDITIVE`` step may be absent
from the table a function gets (absent = all NULL; the conform only runs at the end). (2) A
family's **key columns never change** across versions: the chain reader windows over the files'
raw key columns (:func:`stack.store.chain.read_view`). A key change is a new family, or
``Incompatible``. (3) A function returns a table whose fields are those of version k, modulo (1).

**Payload types are not versioned separately on disk.** A payload struct change is a format
change of every family that embeds it: ``documents`` and ``corpus`` (``annotations.<wf>.payload``),
``<wf>`` (flat payload columns), ``<producer>_evidence`` (payload columns or the record's struct
column), each with its own version and step. ``PayloadSpec.version`` keeps its A.4 meaning (the
registry version written into ``annotations.<wf>.version``, the prompt pin, the X-checks); it is
not a file-format version, and a format change does not bump it.

The registry (:func:`stack.schemas.families._build`) gives each ``FamilySpec`` its ``version``:
the newest registered number. A schema whose digest is not the newest entry's still imports, but
:func:`registration_problem` makes ``write_family`` refuse (S7) and the registry test fails, both
naming :data:`HOWTO`. This module imports nothing of the registry (no import cycle).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import pyarrow as pa

#: The procedure for changing a family's format.
HOWTO = "docs/howto/change-family-format.md"


class UpcastError(ValueError):
    """The old schema does not upcast additively to the target; ``problems`` names each path."""

    def __init__(self, problems: list[str], what: str = "table") -> None:
        self.problems = problems
        super().__init__(f"{what}: not an additive schema change: " + "; ".join(problems))


class UpcastBug(RuntimeError):
    """The steps of a registered path did not produce the current schema: a bug in a step or in
    :data:`VERSIONS` (not a property of the file; except the subclass :class:`UpcastRefused`).
    Deliberately not a ``ValueError``, so no reader's "this file does not read" clause swallows it."""


class StepRefused(ValueError):
    """Raised by a function step whose guard refuses the file's data (it would discard values; D648, first
    used by D591's retired ``documents_drop_model_mentions``); :func:`upcast` re-raises it as :class:`UpcastRefused`."""


class UpcastRefused(UpcastBug):
    """A step refused this file's data (:class:`StepRefused`, naming the file): a property of the file,
    not a bug in the step. Still an :class:`UpcastBug` (no ``ValueError`` clause swallows it); ``stack
    verify`` reports it as the file's S1 finding instead of a check error."""


# ---------------------------------------------------------------- steps
class _Additive:
    """The additive step (module docstring); use the :data:`ADDITIVE` singleton."""

    def __repr__(self) -> str:
        return "ADDITIVE"


ADDITIVE = _Additive()


@dataclass(frozen=True)
class Incompatible:
    """A step that cannot be upcast; ``reason`` names the decision and the remedy."""

    reason: str


StepFn = Callable[[pa.Table], pa.Table]
Step = _Additive | Incompatible | StepFn


@dataclass(frozen=True)
class Version:
    """One version of a family's format: ``number``, the family's ``schema_digest`` at it, the
    ``note`` (what changed, which decision) and the ``step`` from the previous version (``None``
    for version 1)."""

    number: int
    digest: str
    note: str
    step: Step | None = None

    @property
    def kind(self) -> str:
        """``additive``, ``function:<name>``, ``incompatible`` or ``initial`` (version 1)."""
        if self.step is None:
            return "initial"
        if self.step is ADDITIVE:
            return "additive"
        if isinstance(self.step, Incompatible):
            return "incompatible"
        return f"function:{getattr(self.step, '__name__', repr(self.step))}"


# ---------------------------------------------------------------- the version table
#: D690, D691, D693: why a documents/corpus file before v6, or a sources file before v4, does not read.
BLOB_REFS_REASON = ("D690: blob references need the blob sizes, which older files do not record; every store's "
                    "files were upcast once on disk (D691) by tools/upcast_blob_refs.py, removed with the older "
                    "steps by D693 (a store that was missed: run the tool from a checkout before D693)")

# Digests are full sha256 hex (stack.schemas.families.schema_digest). Versions 1-3 of documents,
# corpus and screen_evidence, and 1-2 of sources, are the legacy formats found in every v4 store on
# 2026-10-13 (footer scan), oldest first; each step checked additive with schema_delta.
# D693: the function steps documents/corpus v3 → v4 (D591, ``documents_drop_model_mentions``) and sources
# v2 → v3 (D524, ``sources_records_v3``) are retired to ``Incompatible``: every path through them ends at
# D690's step, so no reader could reach them once the one-off upcast (which ran them) was removed. The
# entries stay so that an older file is refused by name (``IncompatibleFormat``), not as an unknown digest.
_DOCUMENTS: tuple[Version, ...] = (
    Version(1, "6eeb93d7d8ebbee4a4559255e908a135196ba3c453977cc3d60dc228bb4872c3",
            "the first v4 format (store-dev, 2026-10-12)"),
    Version(2, "b34217d7a4f0440ebaf173da5e2aee5f6f3c74d5018db74c6ad0e8767385f983",
            "D204: content.snapshots[].quality; D207: annotations.stack_relevance, sr_relevance, text_usability",
            ADDITIVE),
    Version(3, "8a05b09b3b5081d547f1b27f0891f00a0f008fbc4c36d71ce0d85220c43a17eb",
            "D266-D268: annotations.model_mentions, model_roles, technique_roles", ADDITIVE),
    Version(4, "17863ef8e1438ce7486890bbe1e97eb575bacd1a08570bac7b7f040b4c113c0a",
            "D591: annotations.model_mentions dropped (merged into model_roles); annotations.model_regexp_matches "
            "(a record of model_roles); annotations.model_roles.payload extra_models[].node_id, "
            "models[].matched_node_id", Incompatible(BLOB_REFS_REASON)),
    Version(5, "35f26fcd3166eb5c910ca6b01060084cffaf9a620c0708e5fb51a85aa4de0956",
            "D604 (techpipe): annotations.technique_staged", ADDITIVE),
    Version(6, "d0506689680e6d82e7fa281231f2bcc369954eaa03164d15e2e819e0c7378104",
            "D690: blob references (snapshot and in-band raw/text BLOBs with sizes, current_text_sha256, "
            "convert_pin, members' inband_digests dropped, quality.tier, roles[].rule)",
            Incompatible(BLOB_REFS_REASON)),
    Version(7, "e221744bbc268408000e5bde0ff7fde85f693a8bab62f3bf3caaf506bd04a7ca",
            "D736: annotations.affiliation_orgs (D737: without root_org_ids; never written by a production build "
            "before it)", ADDITIVE),
    Version(8, "01ee99e08d6e0df77bc7255f5d7e7514e3d856ece1fdb58bbe546011449c5106",
            "D755: annotations.translation (machine translations of title and abstract)", ADDITIVE),
)

VERSIONS: dict[str, tuple[Version, ...]] = {
    "sources": (
        Version(1, "823c390395dd2bd7bb04daceebfe68bd8490923707c3e89047f2ee50f9027764",
                "the first v4 format (store-dev, 2026-10-12)"),
        Version(2, "57ac69fdbb00512b84fe7801d2d50854a984b125fc92d860d0ab3936dd3ac54f",
                "D228: metadata.matched_by", ADDITIVE),
        Version(3, "7245889fcff8af194c5beae2e58d9acbc18a6a6c4e78d44def6f5874af23c35f",
                "D524: metadata.record_ref retired: a digest → the provider_record in-band entry, "
                "a v3 meta_id → a v3_meta_id key", Incompatible(BLOB_REFS_REASON)),
        Version(4, "300f00b90aa81c475a84fec5f415037a0f7114c289886838780e229d2ebc7202",
                "D690: in-band entries' raw/text BLOBs with sizes", Incompatible(BLOB_REFS_REASON)),
    ),
    "coverage": (
        Version(1, "67d225e34e5d2bc71536a48b58ff631ab4b68b403d5ae51cd09c037a3de316b3", "the first v4 format"),
    ),
    "documents": _DOCUMENTS,
    "corpus": _DOCUMENTS,  # the documents schema (§12): one history while the two schemas are one
    "triage_evidence": (
        Version(1, "de72c6b6ca3848103f57baf67ef0305859b0bf49a47dce29c493972670fae86b", "the first v4 format"),
        Version(2, "aac3f021edc2d535d6748c710b75ea0438a1f4e2ed4ee79fdc0e031411e96aa9",
                "D527: basis (fields | title)", ADDITIVE),
    ),
    "screen": (
        Version(1, "0826c2a117ec65395bc0d7f3bec788034b639e77e95a667614a3d866afbd110a", "the first v4 format"),
    ),
    "screen_evidence": (
        Version(1, "c67ae30009cc98a20bb023ccde917a60ba508ca3acbb385831f4bcc30fc90416",
                "the first v4 format (store-dev, 2026-10-12)"),
        Version(2, "c16c13f225690501f28b9e6dbbbb4751b40d09ca8fa1a85b1a73ac0ebf169f6a",
                "D207: record columns stack_relevance, sr_relevance, text_usability", ADDITIVE),
        Version(3, "87c29b08daf75bf179fb51fb32e83648cf2e9fca547ddcb733cc9dffe4e051c6",
                "D257: record column doc_class", ADDITIVE),
    ),
    "affiliation": (
        Version(1, "4a80cfaf2bb3e04590d6b9f11272d480fc892d2da8d796d3961fabfbed2e5fec", "the first v4 format"),
    ),
    "affiliation_evidence": (
        Version(1, "64109cae6781415fecc5342d7bf6d4e695d94e919ebd0e3ba7daaee605d3d53b", "the first v4 format"),
    ),
    "doc_class": (
        Version(1, "0f4745e81ad5f04dc4f709fe53fc4e3a28fd1e5832c82b9f5ccb4b8a10efdab1", "the first v4 format"),
    ),
    "stack_relevance": (
        Version(1, "ca49514938576f7c60169d35c5eb3de5386466ee7218145759c89074fd541ea5", "D207: the first format"),
    ),
    "sr_relevance": (
        Version(1, "2a5032a6442475d211b30f14c3a200937c97be57822057c80aba9f1ecf819d49", "D207: the first format"),
    ),
    "text_usability": (
        Version(1, "43d471df812292d960c4b9731f0d3ce8c8b8d4fd71d9c683971f4e144b7a06e1", "D207: the first format"),
    ),
    # D591: model_mentions and model_mentions_evidence (D266, v1 0fccd6a26800… / c92e0d54cf50…) are no
    # families any more (merged into model_roles); their entries went with them.
    "model_roles": (
        Version(1, "d202ba9be3d9de9225b7fd34cdebab797c2297ae77962d31c49605b8ffdc663d", "D267: the first format"),
        Version(2, "799f19143760cd1c4c76b23411412dc999c865ba9fa4de4ff34981577ddd53ed", "D591: matches, ambiguous, extra_models[].node_id", ADDITIVE),
        Version(3, "64cc10c50f01edd43182a0f6b6c521230ec16907c8aafb56432404ba67375262",
                "D591: matches/ambiguous moved to the record model_regexp_matches; models[].matched_node_id",
                Incompatible("D591: model_roles v1/v2 files are not read (matches/ambiguous left the payload); "
                             "re-run annotate.model_roles, or start a new store")),
    ),
    "model_roles_evidence": (
        Version(1, "84ae1cb48d513cbf4e261354d7a70536fe69ada046c08c6d9d0bf960dff85bc3", "D267: the first format"),
        Version(2, "895452e4fd4d6495fe6471af97df8c13ce01f8dc55c0ac838c2c68a289819856", "D591: matches, ambiguous, extra_models[].node_id", ADDITIVE),
        Version(3, "43eb4f6763d77a8e803dc8d1a75eb8acf4a9bf26a8416cb03fdb092c6226c516",
                "D591: matches/ambiguous moved to the record column model_regexp_matches; models[].matched_node_id",
                Incompatible("D591: model_roles_evidence v1/v2 files are not read (matches/ambiguous left the "
                             "payload); re-run annotate.model_roles, or start a new store")),
        Version(4, "91c6d25c09ce57b9ba3904beba734ddee768469c9995eb1b65f3a70e1295bdce",
                "D714: usage columns tokens_cached, tokens_cache_write, tokens_reasoning", ADDITIVE),
    ),
    "model_regexp_matches": (
        Version(1, "502ae4020e6dad420d4948303c0cf5fb6ed869bf1f6d98771d7af4844adfc4f6",
                "D591: the first format (a record of model_roles)"),
    ),
    "technique_roles": (
        Version(1, "46e3525b6469306b443f6bfee16a913518b9e4353ad7ac993e0615cccbdebc8c", "D268: the first format"),
    ),
    "technique_roles_evidence": (
        Version(1, "38651fc8ab5492b4106e5200f18ca4e8a5855e4d817e974d39a55cdf580da239", "D268: the first format"),
    ),
    "technique_staged": (
        Version(1, "1a018f0333a43287641f703d18d0e97fa52991aff87f2948967122597b25549f",
                "D604 (techpipe): the first format"),
    ),
    "technique_staged_evidence": (
        Version(1, "6c5df31dbf7c7704eefb2ba1551e67fa7ccbe509be3d21a7bef2f3a111090f6c",
                "D604 (techpipe): the first format"),
        Version(2, "6719b4eb5c4cd9d00fa1ad4282f6b08229c5e33ce3998bc9ea44bc95d2f95d32",
                "D714: usage columns tokens_cached, tokens_cache_write, tokens_reasoning", ADDITIVE),
    ),
    "affiliation_orgs": (
        Version(1, "e25d58bee17160a8ed64da77645b39de37bbbe739edebf7b29f3db44c60cefae",
                "D736, D737: the first format (no root_org_ids)"),
    ),
    "affiliation_orgs_evidence": (
        Version(1, "4b473af31c6acfa4f1192077973a7363724a13d4cc291579f06424f8dfd395b9",
                "D736: the first format"),
    ),
    "translation": (
        Version(1, "c99c7c373c138d2ef333be938d2da1c022b5c69f458864a3d5bd5820fae4f0bb", "D755: the first format"),
    ),
    "translation_evidence": (
        Version(1, "17a5b8e4d61d52b82d6284288dae7ffc0d3e640d33e457100a5e0932c957a71e", "D755: the first format"),
    ),
    "overrides": (
        Version(1, "785240ac3b1eecc6e4c09d5df70f5ba76dfb928104ae3af664816ac1b9cf7bb6", "D336: the first format"),
    ),
}


# ---------------------------------------------------------------- lookups
def history(family: str) -> tuple[Version, ...]:
    """The versions of ``family``, oldest first; ``()`` when it has no entry."""
    return VERSIONS.get(family, ())


def newest(family: str) -> int:
    """The newest registered version number of ``family`` (0: no entry)."""
    h = history(family)
    return h[-1].number if h else 0


def version(family: str, number: int) -> Version | None:
    """Version ``number`` of ``family``, or ``None`` when it is not registered."""
    return next((v for v in history(family) if v.number == number), None)


def version_of_digest(family: str, digest: str) -> Version | None:
    """The version of ``family`` whose registered digest is ``digest`` (a legacy file's), or ``None``."""
    return next((v for v in reversed(history(family)) if v.digest == digest), None)


def path(family: str, start: int, end: int) -> tuple[Version, ...]:
    """The versions ``start+1 .. end`` of ``family``: the steps a file at ``start`` goes through."""
    return tuple(v for v in history(family) if start < v.number <= end)


def additive(steps: Iterable[Version]) -> bool:
    """Whether every step of ``steps`` is :data:`ADDITIVE`."""
    return all(v.step is ADDITIVE for v in steps)


def registration_problem(family: str, schema_digest: str) -> str | None:
    """Why the family's current schema (``schema_digest``) is not its newest registered version,
    or ``None``: the message ``write_family`` refuses with (S7)."""
    h = history(family)
    if not h:
        return (f"family {family!r} has no entry in stack/schemas/upcasters.py VERSIONS (its schema digest "
                f"{schema_digest}); add version 1 ({HOWTO})")
    if h[-1].digest != schema_digest:
        return (f"family {family!r}: schema digest {schema_digest} is not a registered version (newest v{h[-1].number} "
                f"is {h[-1].digest[:12]}…); append version {h[-1].number + 1} with its step to "
                f"stack/schemas/upcasters.py VERSIONS ({HOWTO})")
    return None


def registry_problems(digests: Mapping[str, str],
                      table: Mapping[str, tuple[Version, ...]] | None = None) -> list[str]:
    """Every way ``table`` (default :data:`VERSIONS`) disagrees with the registry ``digests``
    (family → current schema digest): a family without an entry, an entry that is no family,
    numbers not ``1..n``, a version 1 with a step or a later one without, a digest twice, the
    newest digest not the family's current one."""
    table = VERSIONS if table is None else table
    bad = [f"{f}: no entry (its current schema digest is {d})" for f, d in sorted(digests.items()) if f not in table]
    bad += [f"{f}: an entry but no such family" for f in sorted(table) if f not in digests]
    for f, h in sorted(table.items()):
        if [v.number for v in h] != list(range(1, len(h) + 1)):
            bad.append(f"{f}: version numbers {[v.number for v in h]} are not 1..{len(h)}")
        if h and h[0].step is not None:
            bad.append(f"{f}: version 1 has a step")
        bad += [f"{f}: v{v.number} has no step" for v in h[1:] if v.step is None]
        ds = [v.digest for v in h]
        if len(set(ds)) != len(ds):
            bad.append(f"{f}: a digest registered twice")
        bad += [f"{f}: v{v.number} digest {v.digest!r} is not a sha256 hex" for v in h
                if len(v.digest) != 64 or any(c not in "0123456789abcdef" for c in v.digest)]
        if f in digests and h and h[-1].digest != digests[f]:
            bad.append(f"{f}: current schema digest {digests[f]} is not the newest version's (v{h[-1].number} "
                       f"{h[-1].digest[:12]}…): append a version ({HOWTO})")
    return bad


# ---------------------------------------------------------------- the additive conform (D208 (1))
@dataclass(frozen=True)
class SchemaDelta:
    """``added``: paths of target fields the source lacks (nullable, so NULL); ``relaxed``: paths
    made nullable; ``problems``: why the change is not additive (empty = upcastable)."""

    added: tuple[str, ...] = ()
    relaxed: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def _type_delta(src: pa.DataType, tgt: pa.DataType, path: str, out: dict[str, list[str]]) -> None:
    if src.equals(tgt):
        return
    if pa.types.is_struct(src) and pa.types.is_struct(tgt):
        have = {f.name: f for f in src}
        for f in tgt:
            p = f"{path}.{f.name}" if path else f.name
            if f.name in have:
                _field_delta(have[f.name], f, p, out)
            elif f.nullable:
                out["added"].append(p)
            else:
                out["problems"].append(f"{p}: missing in the old schema and not nullable in the target")
        want = {f.name for f in tgt}
        out["problems"] += [f"{path + '.' if path else ''}{n}: not in the target schema"
                            for n in have if n not in want]
    elif pa.types.is_list(src) and pa.types.is_list(tgt):
        _field_delta(src.value_field, tgt.value_field, path + "[]", out)
    else:
        out["problems"].append(f"{path}: type {src} in the old schema, {tgt} in the target")


def _field_delta(src: pa.Field, tgt: pa.Field, path: str, out: dict[str, list[str]]) -> None:
    if src.nullable and not tgt.nullable:
        out["problems"].append(f"{path}: nullable in the old schema, not nullable in the target")
    elif tgt.nullable and not src.nullable:
        out["relaxed"].append(path)
    _type_delta(src.type, tgt.type, path, out)


def schema_delta(source: pa.Schema, target: pa.Schema) -> SchemaDelta:
    """What upcasting ``source`` to ``target`` changes (metadata ignored)."""
    out: dict[str, list[str]] = {"added": [], "relaxed": [], "problems": []}
    _type_delta(pa.struct(list(source)), pa.struct(list(target)), "", out)
    return SchemaDelta(tuple(out["added"]), tuple(out["relaxed"]), tuple(out["problems"]))


def _nulls(n: int, t: pa.DataType) -> pa.Array:
    # pa.array (not pa.nulls) gives a NULL struct empty values in its non-nullable children,
    # which the parquet writer requires.
    return pa.array([None] * n, type=t)


def _cast(arr: pa.Array, src: pa.DataType, tgt: pa.DataType) -> pa.Array:
    if src.equals(tgt):
        return arr
    if arr.offset:  # rebuild the buffers from offset 0 (from_arrays refuses sliced offsets with a mask)
        arr = pa.concat_arrays([arr])
    mask = arr.is_null() if arr.null_count else None
    if pa.types.is_struct(tgt):
        children = []
        for f in tgt:
            i = src.get_field_index(f.name)
            children.append(_cast(arr.field(i), src.field(i).type, f.type) if i >= 0
                            else _nulls(len(arr), f.type))
        return pa.StructArray.from_arrays(children, fields=list(tgt), mask=mask)
    values = _cast(arr.values, src.value_type, tgt.value_type)
    return pa.ListArray.from_arrays(arr.offsets, values, type=tgt, mask=mask)


def upcast_table(table: pa.Table, target: pa.Schema) -> pa.Table:
    """``table`` in schema ``target`` by the additive rule (module docstring: a field the source
    lacks and the target declares nullable becomes NULL, at any depth; a non-null → nullable
    change keeps the values; target field order); :class:`UpcastError` naming every offending path
    when the change is not additive. A table already in ``target`` is returned unchanged (the
    same object, so it writes byte-identically)."""
    src = table.schema.remove_metadata()
    if src.equals(target, check_metadata=False):
        return table
    delta = schema_delta(src, target)
    if delta.problems:
        raise UpcastError(list(delta.problems))
    cols = []
    for f in target:
        if f.name in src.names:
            s = src.field(f.name).type
            chunks = [_cast(c, s, f.type) for c in table.column(f.name).chunks]
            cols.append(pa.chunked_array(chunks, type=f.type))
        else:
            cols.append(pa.chunked_array([_nulls(table.num_rows, f.type)], type=f.type))
    return pa.Table.from_arrays(cols, schema=target)


# ---------------------------------------------------------------- applying a path
def _differences(got: pa.Schema, want: pa.Schema) -> list[str]:
    d = schema_delta(got, want)
    return [*d.problems, *(f"{p}: missing" for p in d.added), *(f"{p}: nullability" for p in d.relaxed)] \
        or [f"{got} <> {want}"]


def upcast(table: pa.Table, family: str, start: int, target: pa.Schema, *, what: str) -> pa.Table:
    """``table`` (a file of ``family`` at version ``start``, metadata stripped) at the current
    version, schema ``target``: the steps of :func:`path` in order, then one additive conform, then
    the result validated to equal ``target`` exactly (:class:`UpcastBug` otherwise, naming
    ``what``, the version and the differing paths). A path through :class:`Incompatible` is an
    ``UpcastBug`` here too: the reader (:func:`stack.store.parquet.file_format`) refuses such a
    file before reading rows."""
    h = history(family)
    steps = tuple(v for v in h if v.number > start)
    for v in steps:
        if isinstance(v.step, Incompatible):
            raise UpcastBug(f"{what}: {family} v{start} → v{v.number} is incompatible: {v.step.reason}")
        if v.step is None or isinstance(v.step, _Additive):
            continue
        try:
            table = v.step(table)
        except StepRefused as exc:
            raise UpcastRefused(f"{what}: {family} step v{v.number - 1} → v{v.number} ({v.kind}) refused the "
                                f"file: {exc}") from exc
        except Exception as exc:
            raise UpcastBug(f"{what}: {family} step v{v.number - 1} → v{v.number} ({v.kind}) raised "
                            f"{type(exc).__name__}: {exc}") from exc
    try:
        out = upcast_table(table.replace_schema_metadata(None), target)
    except UpcastError as exc:
        raise UpcastBug(f"{what}: {family} v{start} upcast to v{h[-1].number if h else '?'} does not reach the "
                        f"current schema: {'; '.join(exc.problems)}") from None
    if not out.schema.equals(target, check_metadata=False):
        raise UpcastBug(f"{what}: {family} v{start} upcast does not equal the current schema: "
                        + "; ".join(_differences(out.schema, target)))
    return out


__all__ = ["ADDITIVE", "BLOB_REFS_REASON", "HOWTO", "VERSIONS", "Incompatible", "SchemaDelta", "Step", "StepFn", "StepRefused",
           "UpcastBug", "UpcastError", "UpcastRefused", "Version", "additive", "history",
           "newest", "path", "registration_problem", "registry_problems", "schema_delta",
           "upcast", "upcast_table", "version", "version_of_digest"]
