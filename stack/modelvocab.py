"""The model vocabulary (``configs/models.yaml``: models 1.1.0 since 2026-10-29, D738; 1.0.0 from 2026-10-24, D707;
vendored from the taxonomies data repository, ``taxonomy/vendored.lock.yaml``): its typed shape and its loader.

Used by ``annotate.model_roles`` (the matcher, ``workflows/annotate/model_roles/match.py``, and the
menu's names, makers and glosses; ex-``annotate.model_mentions``, merged in by D591) and by
``stack eval model_mentions``. Ported from v3
``stack/types/vocab.py`` (``tomas/model-roles``, d011e5e; the models below are verbatim) plus v3's
``load_vocab`` (from v3's ``workflows/annotate/model_mentions/main.py``), which in v3 went through
``stack.config.load_config_file``; v4 has no such function, so :func:`load_vocab` uses the same
private helpers ``stack.config.load`` uses (read, validate, ``<semver>+<sha8>`` stamp).

A typed mirror of ``doc/model-collection/schema.md`` (schema v0.3, in v3) in the catalogue envelope
(:mod:`stack.catalogue`, schema ``models-2``, models 1.1.0 on): one ``entries`` list of nodes with stable ids,
each ``family`` | ``group`` | ``model``, with ``parent`` pointers; ``maker`` (one, or a list for a joint release)
and ``lab`` are references ``{id, name}`` into the organisation vocabulary of ``meta.references.orgs``, ``name``
the display string the menu shows. The file carries TWO version numbers and they are deliberately distinct:

``version``      the CONTENT version; it feeds the content stamp (``<semver>+<sha8>``).
``meta.schema``  the STRUCTURE (its JSON Schema, ``models-2``), counted on its own.

**Earlier files** (models 1.0.0 and before: a ``nodes`` mapping keyed by id, makers as slugs of ``meta.makers``, the
display names there, ``lab`` a string, ``meta.schema_version`` and ``meta.maker_facets``) are read through
:func:`from_legacy` into the same shape, read-only (pinned eval sets and stored runs name them): each maker slug
becomes ``{id: slug, name: meta.makers[slug]}`` (the slug when absent, as the menu showed it), ``lab`` ``{id: None,
name: lab}`` (``lab_id`` when present).

Scope: this module validates the file's SHAPE. The semantic rules (parent resolution,
kind-specific field bans, pattern lint, cycle detection) were v3's
``doc/model-collection/vocab_tool.py``'s, not ported. The matcher consumes the RAW YAML mapping
(:attr:`Vocabulary.nodes`), not these models: the pydantic layer is a validator, not the runtime
carrier, so the matcher body stays byte-identical to its v3 twin.

**Provenance in v4** (D235 round 5, D591): the file's full sha256 (:func:`vocab_digest`) is one of
``annotate.model_roles``' ``PromptPin.template_digests``, so it is in that workflow's
``config_digest`` and every work key: a vocabulary edit re-asks every document in scope on the next
build (carried rows stay until then; a re-asked document whose menu did not change is served from
the LLM cache).

All models are strict (``extra="forbid"``): an unknown node field is an authoring error
that must be loud, not silently carried.
"""

from __future__ import annotations

import datetime
import functools
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

# YAML parses an unquoted ``2025-05-22`` to ``datetime.date`` and a quoted one (or a
# partial ``2025-05`` / ``2025``) to ``str``; both spellings occur across the vocabulary,
# so the wire type is the union. The string branch is pinned to vocab_tool's ``DATE_RE``.
PartialDate = datetime.date | Annotated[str, Field(pattern=r"^\d{4}(-\d{2})?(-\d{2})?$")]

NodeKind = Literal["family", "group", "model"]
GroupLevel = Literal["line", "series", "generation"]
ParamKind = Literal["official", "estimate", "undisclosed"]
Confidence = Literal["verified", "weak", "stub"]
# ``action``: robot / agent control outputs (VLAs, Gato), vocabulary 0.9.
Modality = Literal["text", "image", "audio", "video", "music", "embedding", "code", "3d", "action"]

# ── Facets (vocabulary 0.9). Classification metadata for dashboards, reports and negative-space
# maps, so that no consumer has to read a subtree of the tree as a class. Never matched.
#: What the model DOES (multi-valued). ``judge-model`` only for dedicated judge/evaluator models,
#: not for general LLMs used as judges.
ModelClass = Literal[
    "language-model", "vision-language", "text-encoder", "embedding", "reranker",
    "guard-classifier", "reward-model", "judge-model", "image-gen", "video-gen",
    "speech-recognition", "speech-generation", "music-gen", "robot-policy", "world-model",
    "vision-backbone", "detector-segmenter", "rl-agent", "science", "interpretability-tool",
]
#: A capability the model is specifically built or marketed for (multi-valued, optional).
Specialization = Literal["reasoning", "code", "computer-use", "cyber", "bio", "medical"]
#: How the node represents its models: ``expanded`` (the models have child nodes), ``folded``
#: (several models live as aliases on this one node; its scalar params/dates are blank),
#: ``class-only`` (a cluster with no model nodes of its own), ``pointer`` (a node kept only so
#: that mentions resolve somewhere; not a catalogue entry for the thing named).
RepresentationStatus = Literal["expanded", "folded", "class-only", "pointer"]
#: PREPARED, NOT YET FILLED (to be filled in a later pass): weight availability.
WeightsAccessStatus = Literal["downloadable", "gated", "not-distributed", "unknown"]
#: PREPARED, NOT YET FILLED (to be filled in a later pass): availability stage at ``status_as_of``.
LifecycleStatus = Literal["announced", "preview", "ga", "restricted", "retired", "unknown"]


class VocabMeta(BaseModel):
    """``meta:`` of the envelope (:mod:`stack.catalogue`): the schema name, status, date, description and the
    organisation vocabulary version the references resolve in. A file read through :func:`from_legacy` has
    ``schema`` ``models-1`` (its ``schema_version`` and maker facets are not carried) and no status."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema")
    status: Literal["draft", "released"] | None = None
    updated: PartialDate
    description: str | None = None
    sources: dict[str, str] | None = None
    #: catalogue name → version (``{orgs: 0.2.0}``): where ``maker`` and ``lab`` ids resolve.
    references: dict[str, str] = Field(default_factory=dict)


class OrgRef(BaseModel):
    """A reference into the organisation vocabulary: ``id`` (an entry id of ``meta.references.orgs``) and ``name``
    (the display string; a maker's is the menu's maker name). ``id`` is None only on a ``lab`` read from a file
    before 1.1.0, which had no lab ids."""

    model_config = ConfigDict(extra="forbid")

    id: str | None
    name: str


class Lifecycle(BaseModel):
    """All dates of a model's life; each optional, ``released`` is the canonical anchor.

    Vocabulary 0.9 PREPARES (does not yet fill; to be filled in a later pass) an availability
    stage: ``status`` at ``status_as_of``, and ``first_public`` (first public availability in any
    form, preview included) kept distinct from ``ga`` (general availability). Until that pass,
    ``released`` stays the anchor and is the first public date by catalogue convention."""

    model_config = ConfigDict(extra="forbid")

    announced: PartialDate | None = None
    preview: PartialDate | None = None
    released: PartialDate | None = None
    deprecated: PartialDate | None = None
    retired: PartialDate | None = None
    status: LifecycleStatus | None = None
    status_as_of: PartialDate | None = None
    first_public: PartialDate | None = None
    ga: PartialDate | None = None


class WeightsAccess(BaseModel):
    """PREPARED, NOT YET FILLED (to be filled in a later pass): whether the weights can be obtained,
    under which licence (a separate field: a licence is not an access level), on what evidence,
    as of when."""

    model_config = ConfigDict(extra="forbid")

    status: WeightsAccessStatus
    licence: str | None = None
    evidence: str | None = None
    as_of: PartialDate | None = None


class Links(BaseModel):
    """Primary sources for the entry (URLs, never verified for liveness here)."""

    model_config = ConfigDict(extra="forbid")

    announcement: str | None = None
    paper: str | None = None
    model_card: str | None = None


class Params(BaseModel):
    """Parameter count in MILLIONS, and its provenance. ``source`` is required for ``estimate``.

    Fractional values are legal and are not a rounding artefact: the pre-llm section (09) revokes
    the vocabulary's ~100M floor, so LeNet-5 (~60k params) is ``total_m: 0.06`` and SqueezeNet
    ``1.25``. ``int | float`` rather than plain ``float`` so the frontier-scale counts stay ints
    on the validated model instead of being coerced to ``1600000.0``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ParamKind
    total_m: int | float | None = None
    active_m: int | float | None = None  # MoE only
    source: str | None = None


class Modalities(BaseModel):
    """What the model consumes and emits. ``in`` is a Python keyword, hence the alias."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    in_: list[Modality] = Field(default_factory=list, alias="in")
    out: list[Modality] = Field(default_factory=list)


class VocabNode(BaseModel):
    """One vocabulary node: a family, an intermediate group, or a concrete model.

    Field applicability is kind-dependent (families/groups carry no release metadata) but is
    NOT enforced here — ``vocab_tool.validate_nodes`` owns that rule set, and duplicating it
    would give two places to keep in sync. This model's job is the field vocabulary and the
    value types.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    name: str
    level: GroupLevel | None = None
    parent: str | None = None
    # a handful of nodes are joint releases (Megatron-Turing NLG: nvidia + microsoft)
    maker: OrgRef | list[OrgRef] | None = None
    # the unit inside the corporate maker (vocabulary 0.7: DeepMind / Brain models carry maker `google`
    # and ``lab: DeepMind``); display only, never matched or rendered
    lab: OrgRef | None = None
    lifecycle: Lifecycle | None = None
    links: Links | None = None
    params: Params | None = None
    modalities: Modalities | None = None
    # mention patterns — canonical token sequences (see schema.md "Mention matching").
    # `None` is accepted because the assembler emits `weak_mentions:` with an empty body
    # for a few nodes; it means "no patterns", same as `[]`.
    mentions: list[str] | None = None
    weak_mentions: list[str] | None = None
    mentions_verbatim: list[str] | None = None
    confidence: Confidence | None = None
    checked: PartialDate | None = None
    # free-form classification labels (``pre-llm``, …); open vocabulary by design, like
    # ``variant_kind``. The kebab-case/nonempty lint is vocab_tool's ``validate_nodes``, which
    # owns the value-level rules; this model's job stops at "an optional list of strings".
    tags: list[str] | None = None
    # judge-facing one-liner (≤15 words) for menu rendering: what this node denotes,
    # standalone, thing-type noun first. Distinct audience from ``notes`` (matcher/curator
    # register); non-matching metadata — the matcher never reads it.
    gloss: str | None = None
    notes: str | None = None
    uncertainties: list[str] | None = None
    # relations (ids into the same map; referential integrity is vocab_tool's check)
    variant_of: str | None = None
    variant_kind: str | None = None  # open vocabulary by design (schema.md)
    based_on: str | None = None
    distilled_from: str | None = None
    same_weights_as: str | None = None
    # facets (vocabulary 0.9; see the Literal types above). ``model_class`` on a family/group is the
    # union over the models it covers (its child nodes and the models folded into it as aliases);
    # ``specialization`` on a family/group only when it holds for every model it covers.
    model_class: list[ModelClass] | None = None
    specialization: list[Specialization] | None = None
    representation_status: RepresentationStatus | None = None
    weights_access: WeightsAccess | None = None  # prepared, filled in a later pass


def maker_refs(node: VocabNode) -> list[OrgRef]:
    """The node's own makers (none, one, or a joint release's list)."""
    m = node.maker
    return [] if m is None else m if isinstance(m, list) else [m]


class ModelVocab(BaseModel):
    """``configs/models.yaml`` as a whole: content version + meta + the entries, and :attr:`nodes` by id."""

    model_config = ConfigDict(extra="forbid")

    version: str
    meta: VocabMeta
    entries: list[VocabNode]
    _nodes: dict[str, VocabNode] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _index(self) -> ModelVocab:
        nodes = {e.id: e for e in self.entries}
        if len(nodes) != len(self.entries):
            dup = sorted({e.id for e in self.entries if sum(x.id == e.id for x in self.entries) > 1})
            raise ValueError(f"duplicate entry ids: {dup[:5]}")
        names: dict[str | None, set[str]] = {}
        for e in self.entries:
            for m in maker_refs(e):
                names.setdefault(m.id, set()).add(m.name)
        several = {i: sorted(n) for i, n in names.items() if len(n) > 1}
        if several:
            raise ValueError(f"a maker id with several names: {several}")
        self._nodes = nodes
        return self

    @property
    def nodes(self) -> dict[str, VocabNode]:
        """Entry id → entry, in file order."""
        return self._nodes


# ─────────────────────────── the loader (v3 model_mentions/main.py's ``load_vocab``) ───────

VOCAB_FILENAME = "models.yaml"
#: The JSON Schema of the envelope files (``meta.schema``; :mod:`stack.catalogue`).
SCHEMA = "models-2"


def from_legacy(data: Mapping[str, Any]) -> dict[str, Any]:
    """A models file before 1.1.0 (``nodes`` mapping, ``meta.makers``) in the envelope shape, read-only (module
    docstring). Not validated here: :class:`ModelVocab` is."""
    meta = data.get("meta") or {}
    makers = meta.get("makers") or {}

    def ref(slug: str) -> dict[str, Any]:
        return {"id": slug, "name": makers.get(slug, slug)}

    entries = []
    for nid, node in (data.get("nodes") or {}).items():
        e: dict[str, Any] = {"id": nid}
        for k, v in node.items():
            if k == "lab_id":
                continue
            if k == "maker" and v is not None:
                v = [ref(x) for x in v] if isinstance(v, list) else ref(v)
            elif k == "lab" and v is not None:
                v = {"id": node.get("lab_id"), "name": v}
            e[k] = v
        entries.append(e)
    return {"version": data.get("version"),
            "meta": {"schema": "models-1", "updated": meta.get("updated")},
            "entries": entries}


@dataclass(frozen=True)
class Vocabulary:
    """A loaded, validated, stamped model vocabulary, ready to match with.

    ``nodes`` is the raw YAML (what the matcher core takes) as a mapping id → entry without its ``id``;
    ``validated`` is the pydantic view of the same file; ``stamp`` is the usual ``<semver>+<sha8>`` content stamp
    over the file bytes; ``digest`` the full sha256 of the bytes (the config-digest input)."""

    path: Path
    stamp: str
    digest: str
    nodes: dict[str, Any]
    validated: ModelVocab


def vocab_path(dir: Path | None = None, filename: str = VOCAB_FILENAME) -> Path:
    """``<config dir>/models.yaml``; the config dir resolves as :func:`stack.config.load`'s
    (argument → ``STACK_CONFIG_DIR`` → ``<repo>/configs``)."""
    from stack.config import _resolve_dir

    return _resolve_dir(dir) / filename


def vocab_digest(dir: Path | None = None, filename: str = VOCAB_FILENAME) -> str:
    """sha256 hex of the vocabulary file's bytes (no parse; a missing file raises
    :class:`stack.config.ConfigError`)."""
    from stack.config import ConfigError

    path = vocab_path(dir, filename)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigError(f"{filename}: cannot read '{path}': {exc}") from exc


def load_vocab(dir: Path | None = None, filename: str = VOCAB_FILENAME) -> Vocabulary:
    """Load, validate and stamp the vocabulary (cached per path and content: ~1 s each)."""
    path = vocab_path(dir, filename)
    return _load(str(path.resolve()), vocab_digest(dir, filename))


@functools.lru_cache(maxsize=2)
def _load(path_str: str, digest: str) -> Vocabulary:
    from stack.config import _read_yaml, _stamp, _validate

    path = Path(path_str)
    from stack import catalogue
    from stack.config import ConfigError

    data, raw = _read_yaml(path, path.name)
    if hashlib.sha256(raw).hexdigest() != digest:  # the file changed between hash and read
        raise RuntimeError(f"{path}: changed while loading the model vocabulary")
    if isinstance(data, Mapping) and "entries" in data:
        try:
            catalogue.validate(data, SCHEMA)
        except catalogue.CatalogueError as exc:
            raise ConfigError(f"{path.name}: {exc}") from exc
    else:
        data = from_legacy(data)
    validated = _validate(ModelVocab, data, path.name, path.name)
    nodes = {e["id"]: {k: v for k, v in e.items() if k != "id"} for e in data["entries"]}
    return Vocabulary(path=path, stamp=_stamp(validated.version, raw), digest=digest,
                      nodes=nodes, validated=validated)
