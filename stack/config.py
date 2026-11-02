"""``stack.config`` — load, validate, and stamp the six-file config registry.

Contract: v3's ``spec/30-stack-module.md`` §3; semantics v3's ``spec/10-design.md`` §8 + §8.1;
recipes v3's ``spec/20-technical.md`` §3. This module is **standalone**: it imports only stdlib +
pydantic + PyYAML, never ``stack.types`` or ``stack.core`` (config sits below both).

The public surface is :func:`load` returning a :class:`StackConfig`. The six config files
are parsed and **schema-validated by the pydantic models below** (the models *are* the schema
layer here — a validation failure is wrapped in :class:`ConfigError` with the offending file
name and aborts before pipeline step one). The taxonomy is resolved through
``registry.yaml``'s pin. Each file is content-stamped per 20-technical §3
(``"<semver>+<first 8 hex of sha256(file bytes)>"``); sweep pinning is "load once, pass the
object".

**The taxonomy is parsed on first use** (:attr:`StackConfig.taxonomy`). Its file (~21k lines) is
read by no stage, and parsing it was most of a :func:`load`. :func:`load` reads its bytes (the
stamp), takes its ``version`` from the top-level ``version:`` line and checks the pin against it
(cross-check 1); the YAML parse, the schema validation and cross-check 9 run on the first
``config.taxonomy`` and raise the same :class:`ConfigError` there, with the stamp's version
checked against the parsed one.

v3's seventh file, ``workflows.yaml`` (workflow registration: phase, version, ``llm_role``,
``prompt``, ``reads``/``bump_on``, ``requires``, ``policy``, payload schema paths), was retired
at T4.5 (D42): a stage is its module (``workflows/<phase>/<name>/main.py``, D73) and its plan
line (``configs/plans/*.yaml``: inputs, filter, pins, options). A config dir that
still holds a ``workflows.yaml`` is refused, so a stale copy cannot pass for the live registry.

Config directory resolution (``load(dir=...)`` → env ``STACK_CONFIG_DIR`` →
``<repo_root>/configs``). The fallback is the live top-level registry — ``configs/``
graduated out of ``spec/examples/config`` per the spec README's build-time layout.

Cross-checks replicated here (a load-time subset of ``tools/check_configs.py``, all fail loud):

1.  taxonomy pin resolves — ``registry.taxonomy.file`` exists under the config dir and the
    file's ``version`` equals ``registry.taxonomy.active``;
7.  ``llm.egress.by_license_class`` keys == ``enums.license_class`` values;
9.  taxonomy ``exclusive_with`` / ``absorbs`` reference the facet's own menu values (on the
    first ``config.taxonomy``, with the parse).

(Numbers 2-6 were the ``workflows.yaml`` checks; 8, 10 and the excerpt half of 7 checked v3
``policies.yaml`` keys that moved to v3's ``configs/`` (D438); the gaps are kept so
older references to a number still point at the same rule.)

NOT replicated here (``tools/check_configs.py`` remains the full CI gate): JSON-Schema
validation of the config files themselves and of the ``schemas/`` layer (x-enum tag/order
lints), payload/aggregate example-instance validation, the channel-group / key-kind lints,
``interfaces.py`` Literal-domain mirroring, channel ``license_class`` ∈ ``enums.license_class``,
and channels ``concept_groups.techniques`` → taxonomy-slug resolution.
Run ``tools/check_configs.py`` for the complete set.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stack import yamlio


class ConfigError(Exception):
    """Any config load, parse, schema-validation, or cross-check failure.

    Pydantic ``ValidationError``s are wrapped in this with the offending file name so callers
    get one loud, actionable exception type.
    """


# Fixed-name files loaded directly from the config dir (taxonomy filename comes from registry).
_FIXED_FILES: dict[str, str] = {
    "enums": "enums.yaml",
    "registry": "registry.yaml",
    "channels": "channels.yaml",
    "llm": "llm.yaml",
    "policies": "policies.yaml",
}


# ─────────────────────────── enums.yaml ────────────────────────────────────


class EnumVocab(BaseModel):
    """One controlled vocabulary: value slug → one-line gloss."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    ordered: bool = False
    nullable: bool = False
    values: dict[str, str]


class EnumsConfig(BaseModel):
    """The single vocabulary source (I7): every ``enums.yaml`` vocabulary."""

    model_config = ConfigDict(extra="forbid")

    version: str
    enums: dict[str, EnumVocab]

    def vocab(self, name: str) -> tuple[str, ...]:
        """Ordered value slugs of vocabulary ``name`` (``KeyError`` if unknown)."""
        return tuple(self.enums[name].values.keys())

    def is_valid(self, name: str, value: str) -> bool:
        """True iff ``value`` is a member of vocabulary ``name`` (False on unknown vocab)."""
        vocab = self.enums.get(name)
        return vocab is not None and value in vocab.values


# ─────────────────────────── registry.yaml ─────────────────────────────────


class TaxonomyPin(BaseModel):
    """The active-taxonomy pin: version + the file runtime workflows read."""

    model_config = ConfigDict(extra="forbid")

    active: str
    file: str


class ModelEntry(BaseModel):
    """A canonical model release (subject ``model:<slug>``)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    org: str
    family: str
    released: str | None = None
    generation: int | None = None
    attributes: dict[str, Any] | None = None  # open: model-family specific
    aliases: list[str]


class OrgEntry(BaseModel):
    """A registered org (subject ``org:<slug>``)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    kind: str
    domains: list[str] = []
    openalex_ids: list[str] = []
    aliases: list[str]


class RegistryConfig(BaseModel):
    """Subject-model registry + orgs, and the active-taxonomy pin."""

    model_config = ConfigDict(extra="forbid")

    version: str
    taxonomy: TaxonomyPin
    models: list[ModelEntry]
    orgs: list[OrgEntry]


# ─────────────────────────── taxonomy-vX.Y.yaml ────────────────────────────


class FacetValue(BaseModel):
    """One value on a facet's answer menu."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    description: str = ""


class FacetDef(BaseModel):
    """A facet: its answer menu + value-combination hygiene metadata (H-M2)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    cardinality: str
    population: str | None = None
    difficulty: str | None = None
    empty_is_legal: bool = False
    description: str = ""
    exclusive_with: list[list[str]] = []  # mutually-exclusive value groups
    absorbs: dict[str, list[str]] = {}    # {dominant: [absorbed]}
    values: list[FacetValue]


class EdgeDef(BaseModel):
    """A taxonomy edge type (composes_with / variant_of / critiqued_by / …)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    target: str
    description: str = ""


class FieldDef(BaseModel):
    """A descriptive field definition (tldr, resource-cost, …)."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str = ""
    description: str = ""
    values: list[str] = []


class FacetAnswer(BaseModel):
    """A technique's answer for one facet: ``{values, origin}``."""

    model_config = ConfigDict(extra="forbid")

    values: list[str]
    origin: str


class TechniqueDef(BaseModel):
    """A taxonomy technique/variant entry."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    type: str
    family: str
    description: str
    variant_of: str | None = None
    fields: dict[str, Any] = {}  # open: descriptive field answers keyed by field slug
    facets: dict[str, FacetAnswer]


class TaxonomyConfig(BaseModel):
    """The pinned taxonomy artifact (facet-menu source of truth)."""

    model_config = ConfigDict(extra="forbid")

    version: str
    status: str = ""
    sample: bool = False
    facets: list[FacetDef]
    edges: list[EdgeDef] = []
    fields: list[FieldDef] = []
    techniques: list[TechniqueDef]

    def facet(self, slug: str) -> FacetDef:
        """The facet with ``slug`` (``KeyError`` if unknown)."""
        for f in self.facets:
            if f.slug == slug:
                return f
        raise KeyError(slug)

    @property
    def facet_slugs(self) -> frozenset[str]:
        """All facet slugs."""
        return frozenset(f.slug for f in self.facets)

    @property
    def technique_slugs(self) -> frozenset[str]:
        """All technique slugs."""
        return frozenset(t.slug for t in self.techniques)

    def menu(self, facet_slug: str) -> frozenset[str]:
        """The set of value slugs on facet ``facet_slug`` (``KeyError`` if unknown)."""
        return frozenset(v.slug for v in self.facet(facet_slug).values)


# ─────────────────────────── channels.yaml ─────────────────────────────────


class ConceptGroup(BaseModel):
    """A coverage-topic cluster of synonym surface forms → taxonomy techniques."""

    model_config = ConfigDict(extra="forbid")

    id: str
    broad: bool = False
    misuse: bool = False
    # Only meaningful ANDed with `ai_context` ("decision theory" alone is operations research and
    # medicine): the phrase-match collectors and arXiv's keyword family intersect it, OpenAlex
    # renders a boolean phrase query (docs/scope/shallow-review.md (d) E1/E2).
    needs_ai_context: bool = False
    terms: list[str]
    # REPLACES `terms` on OpenAlex only (v1 search_config parity): a phrase that is precise under
    # phrase-matching can flood OpenAlex's token-AND search. `None` = use `terms`; an EMPTY list =
    # no OpenAlex-viable term, skip the group there.
    openalex_terms: list[str] | None = None
    # REPLACES `terms` on Semantic Scholar only, the same way (round 4, P; D229): S2's bulk search
    # stems a quoted word ("steerability" -> "steer"). `None` = use `terms`; EMPTY = skip the
    # group there.
    s2_terms: list[str] | None = None
    # REPLACES `terms` on the forum search (collect.forums: LessWrong, Alignment Forum) the same way (the Chinese-coverage work):
    # their search is not a phrase match, so a term common in prose ("harmful content") returns most of the
    # forum. `None` = use `terms`; EMPTY = no forum queries for the group.
    forum_terms: list[str] | None = None
    techniques: list[str] = []

    def terms_for_openalex(self) -> list[str]:
        """The terms this group queries OpenAlex with (`openalex_terms` when set, else `terms`)."""
        return self.terms if self.openalex_terms is None else self.openalex_terms

    def terms_for_s2(self) -> list[str]:
        """The terms this group queries Semantic Scholar with (`s2_terms` when set, else `terms`)."""
        return self.terms if self.s2_terms is None else self.s2_terms

    def terms_for_forums(self) -> list[str]:
        """The terms this group queries the forums with (`forum_terms` when set, else `terms`)."""
        return self.terms if self.forum_terms is None else self.forum_terms


# Identity groups (design §6, §2.6): keys merge documents only within a group. Mirrors
# `channel_groups` in channels.yaml and the JSON Schema enum; config stays standalone, so the
# words are spelled here rather than imported (tests/test_config.py pins them against
# `stack.schemas.vocab` and the schema).
ChannelGroup = Literal["paper", "forum", "url", "social", "record"]
# What a provider-side miss means (design §2.6, D13): `withdrawn` lets `miss_cap` consecutive
# misses retract a source; `unknown` never withdraws (OpenAlex, §5.6).
MissMeans = Literal["withdrawn", "unknown"]


class KeywordCuts(BaseModel):
    """The backfill cuts a search channel applies to its concept-group (keyword) hits (2026-10-13;
    docs/design/collection-yield.md §10; the classifier and vocabularies are ``stack/rules.py``
    ``keyword_cut``). Institution and author-id queries are never cut."""

    model_config = ConfigDict(extra="forbid")

    # Source cuts: `publisher` drops a hit with a publisher DOI or no DOI and no arXiv location;
    # `self_publication` drops Zenodo and preprint-server DOIs (SSRN kept).
    drop_sources: list[Literal["publisher", "self_publication"]] = Field(default_factory=list)
    # Concept groups whose hits are kept only with an AI term in title or abstract (cut `ai_term`).
    ai_term_groups: list[str] = Field(default_factory=list)
    # D654 (a): a hit published before this ISO date whose title or abstract carries a pre-2020 safety
    # stem (stack/rules.py PRE2020_SAFETY_STEMS) bypasses `drop_sources`; `ai_term` still applies. None = no bypass.
    # The floor and the stem set are in the group's coverage key.
    publisher_cut_floor: str | None = None
    # D654 (b), OpenAlex only: the broad groups listed in `field_filter_groups` are swept once more up to the day
    # before this ISO date without the CS-field filter (`primary_topic.field.id:17`), as a second plan entry with its
    # own coverage key (D671). Both or neither.
    field_filter_floor: str | None = None
    field_filter_groups: list[str] = Field(default_factory=list)
    # The Chinese-coverage work: the `publisher` cut keeps hits with a DOI of an allowlisted venue (stack/rules.py VENUE_DOI_PREFIXES:
    # ACL Anthology, AAAI, IJCAI, NDSS, PLOS). The allowlist's digest is in the group's coverage key (only with `publisher` dropped).
    keep_venues: bool = False
    # With `keep_venues`: the allowlist keeps only a hit with an AI term in title or abstract (stack/rules.py
    # venue_allowlist_applies); a venue hit without one is a `publisher` work. The AI-term vocabulary's digest is in
    # the group's coverage key. Semantic Scholar sets it: its group queries have no field filter, so a broad phrase
    # ("safety evaluation") finds PLOS's biomedical papers.
    venue_ai_term: bool = False

    @model_validator(mode="after")
    def _floors(self) -> KeywordCuts:
        for name in ("publisher_cut_floor", "field_filter_floor"):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"keyword_cuts.{name} {value!r} is not an ISO date (YYYY-MM-DD)")
        if bool(self.field_filter_floor) != bool(self.field_filter_groups):
            missing = "field_filter_groups" if self.field_filter_floor else "field_filter_floor"
            raise ValueError(f"keyword_cuts: field_filter_floor and field_filter_groups go together ({missing} unset)")
        if self.venue_ai_term and not self.keep_venues:
            raise ValueError("keyword_cuts.venue_ai_term guards the venue allowlist: it needs keep_venues")
        return self


class ChannelDef(BaseModel):
    """A configured source origin (search / scrape / watch / manual / feed)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    location: str | None = None
    license_class: str
    breadth: str | None = None
    concept_groups: str | list[str] | None = None  # "all" or an explicit list of group ids
    from_date: str | None = None
    politeness: dict[str, Any] | None = None  # open: rate/robots/mailto knobs
    self_published_org: str | None = None
    # Identity group of every source this channel mints (§6); `gather` unions keys within it.
    channel_group: ChannelGroup
    miss_means: MissMeans = "unknown"
    # Key kinds this channel records that never merge identities (§3.5, §5.6: `related`, `relay`,
    # the X context keys); vocabulary linted by tools/check_configs.py.
    non_merging_keys: list[str] = Field(default_factory=list)
    # Ordered stable record fields hashed into `sources.record_digest` (C23; counters excluded).
    # Empty = the collector's default projection.
    record_projection: list[str] = Field(default_factory=list)
    # `kind: feed` (collect.feeds): a regex an item's link must match (re.search); None = every item.
    include_re: str | None = None
    # OpenAlex / Semantic Scholar: the backfill cuts on concept-group hits (`KeywordCuts`); None = none.
    keyword_cuts: KeywordCuts | None = None


class ChannelGroupDef(BaseModel):
    """One identity group (design §6): the key kinds that merge documents within it."""

    model_config = ConfigDict(extra="forbid")

    merge_keys: list[str]


class Institution(BaseModel):
    """One OpenAlex institution-id sweep target (v1 ``orgs.yaml`` ``openalex_institutions``).

    ``org`` is the kebab org slug (surfaced on coverage receipts as ``org:<slug>``); ``id`` is the
    OpenAlex institution id (``I…``); ``fields`` is the optional ``primary_topic.field.id`` volume
    guard set only on generic big-company entities (v1's ``openalex_fields``).
    """

    model_config = ConfigDict(extra="forbid")

    org: str
    id: str
    fields: list[int] = Field(default_factory=list)


class ChannelsConfig(BaseModel):
    """Configured channels, concept groups, breadth presets, AI-context terms, institutions."""

    model_config = ConfigDict(extra="forbid")

    version: str
    breadth_presets: dict[str, Any] = {}  # open: preset shape is channel-family specific
    concept_groups: list[ConceptGroup] = []
    # AI-context surface forms ANDed onto every `misuse: true` group on phrase-match channels
    # (v1 `search_config.yaml ai_context`). Read by collect.semantic_scholar; token-AND backends
    # (OpenAlex) cannot express that intersection and skip the misuse groups instead.
    ai_context: list[str] = Field(default_factory=list)
    # Identity groups -> merging key kinds (design §6). Keyed by the `ChannelGroup` words.
    channel_groups: dict[ChannelGroup, ChannelGroupDef] = Field(default_factory=dict)
    channels: list[ChannelDef]
    # Stored under an alias so the typed accessor below can be named `institutions()` without
    # shadowing the field (the collect.openalex institution-id sweep, v1 orgs.yaml).
    institutions_: list[Institution] = Field(default_factory=list, alias="institutions")

    @model_validator(mode="after")
    def _keyword_cut_groups_exist(self) -> ChannelsConfig:
        known = {g.id for g in self.concept_groups}
        bad = sorted(f"{c.id}: {g}" for c in self.channels if c.keyword_cuts
                     for g in c.keyword_cuts.ai_term_groups if g not in known)
        if bad:
            raise ValueError(f"keyword_cuts.ai_term_groups name unknown concept groups: {bad}")
        broad = {g.id for g in self.concept_groups if g.broad}
        bad = sorted(f"{c.id}: {g}" for c in self.channels if c.keyword_cuts
                     for g in c.keyword_cuts.field_filter_groups if g not in broad)
        if bad:
            raise ValueError(f"keyword_cuts.field_filter_groups name unknown or non-broad concept groups: {bad}")
        return self

    def channel(self, channel_id: str) -> ChannelDef:
        """The channel with ``channel_id`` (``KeyError`` if unknown)."""
        for c in self.channels:
            if c.id == channel_id:
                return c
        raise KeyError(channel_id)

    def max_results_per_query(self, channel_id: str, default: int) -> int:
        """The configured per-query result cap for a channel: its ``breadth`` preset's
        ``max_results_per_query``, else ``default``.

        THE reader for ``breadth_presets`` — before this existed the presets were committed but
        never consulted, so ``channels.yaml`` advertised a sweep depth (2000) that no sweep used
        (they all ran on a hardcoded 100). Unknown/missing presets and non-integer values fall
        back to ``default`` rather than raising: a breadth typo must not wedge a sweep, and
        ``check_configs`` is where config shape is policed.
        """
        preset = self.breadth_presets.get(self.channel(channel_id).breadth or "")
        if not isinstance(preset, dict):
            return default
        value = preset.get("max_results_per_query")
        return value if isinstance(value, int) and value > 0 else default

    def institutions(self) -> list[Institution]:
        """The OpenAlex institution-id sweep targets (``collect.openalex`` second query family)."""
        return self.institutions_


# ─────────────────────────── llm.yaml ──────────────────────────────────────


class ProviderConfig(BaseModel):
    """LLM transport/provider config; the API key is read from ``api_key_env``, never here.

    ``dialect`` is the wire shape of a chat-completions request (D714): ``openrouter`` (the default: the
    ``provider`` routing block, ``usage.include``, ``max_tokens``, ``reasoning: {effort}``, the system part as a
    ``cache_control`` text block) or ``openai`` (the OpenAI API itself: ``max_completion_tokens``,
    ``reasoning_effort``, ``prompt_cache_key``, a plain system message, no routing block; the cost is computed
    from the price table, cached and cache-write tokens at their own rates). ``strip_model_prefix`` is removed
    from the model id on the wire (``openai/`` for the OpenAI API); the model id everywhere else (egress gate,
    prices, digests) keeps it. ``anthropic`` (D722) is the Anthropic Messages API (``/messages``, ``x-api-key``,
    ``output_config.format`` / ``.effort``, ``cache_control`` breakpoints; the reply is read through a
    chat-completions view, :func:`stack.llm.anthropic_view`).

    ``egress_provider`` (D722): the egress slug of the counterparty for every model this provider serves (a direct
    API: ``openai``, ``anthropic``); unset, the slug comes from the model id's prefix (``egress.provider_prefixes``,
    OpenRouter, which routes by it). ``stream`` (D722, ``dialect: anthropic`` only): ask for server-sent events and
    reassemble the reply in the transport, so a long call gets the API's keepalive pings instead of an idle
    connection that drops."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key_env: str
    referer: str | None = None
    title: str | None = None
    dialect: Literal["openrouter", "openai", "anthropic"] = "openrouter"
    strip_model_prefix: str | None = None
    #: The socket read timeout of this provider's requests (D714), else the transport's (120 s). The OpenAI API sends
    #: nothing until a non-streamed reply is done (OpenRouter sends keepalives), so a long reasoning call needs more.
    timeout_s: float | None = Field(default=None, gt=0)
    egress_provider: str | None = None
    stream: bool = False

    @model_validator(mode="after")
    def _stream_only_anthropic(self) -> ProviderConfig:
        if self.stream and self.dialect != "anthropic":
            raise ValueError(f"provider stream: true is read only by dialect anthropic (this one: {self.dialect})")
        return self


#: The provider qualifier of a model reference (D722): ``<provider>:<model id>``, where ``<provider>`` (no ``/``)
#: names an ``llm.providers`` entry. OpenRouter variant suffixes (``vendor/model:free``) are not qualifiers: their
#: ``:`` comes after a ``/``.
_QUALIFIED = re.compile(r"^([A-Za-z0-9_-]+):(.+)$")


def split_model_ref(ref: str) -> tuple[str | None, str]:
    """``(provider name, model id)`` of a model reference: ``("anthropic", "claude-opus-5-5")`` for
    ``anthropic:claude-opus-5-5``, ``(None, ref)`` for an unqualified id (``openai/gpt-6-luna``,
    ``vendor/model:free``). Pure; whether the provider exists is :meth:`LlmConfig.route`'s check."""
    m = _QUALIFIED.match(ref)
    return (m.group(1), m.group(2)) if m else (None, ref)


@dataclass(frozen=True)
class ModelRoute:
    """Where one model reference of a role goes (D722): the provider's name (None = the default ``provider``), its
    config, the model id at that provider (the reference without its qualifier; the wire id further loses the
    provider's ``strip_model_prefix``) and the egress slug the gate checks (None = unmappable: gated out)."""

    provider_name: str | None
    provider: ProviderConfig
    model_id: str
    egress_slug: str | None

    @property
    def wire_model(self) -> str:
        strip = self.provider.strip_model_prefix
        return self.model_id[len(strip):] if strip and self.model_id.startswith(strip) else self.model_id


class RoleConfig(BaseModel):
    """A named role → model + fallbacks + mandatory ``max_tokens`` (EXEC-2).

    ``temperature`` is an optional ``llm.schema.json`` role field not in the pinned shape;
    included so a config setting it is not rejected by ``extra="forbid"``. ``reasoning_effort``
    (optional, techpipe D612) is sent as OpenRouter's ``reasoning: {effort}`` and enters the sampling
    params (digests, cache key) only when set; unset, the request is the provider default.
    ``provider`` (optional, D714) names an ``llm.providers`` entry the role's calls go to instead of the
    default ``provider`` (OpenRouter); it is a transport choice, in no digest or cache key. ``model`` and each
    ``fallbacks`` entry may instead be provider-qualified, ``<provider>:<model id>`` (D722,
    :func:`split_model_ref`): that model goes to that provider whatever the role's ``provider``; the reference as
    written is the model in the digests, as an unqualified id is.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    fallbacks: list[str] = []
    max_tokens: int
    temperature: float | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    provider: str | None = None


class EgressClass(BaseModel):
    """Per-license-class egress rule (sanctioned providers + data-collection policy)."""

    model_config = ConfigDict(extra="forbid")

    providers: list[str]
    data_collection: str
    allow_fallbacks: bool = True


class EgressConfig(BaseModel):
    """The egress gate: transport, prefix map, default rule, per-license-class rules."""

    model_config = ConfigDict(extra="forbid")

    transport: str | None = None
    provider_prefixes: dict[str, str] = {}
    default: EgressClass
    by_license_class: dict[str, EgressClass] = {}


class BudgetConfig(BaseModel):
    """The per-run USD spend cap of an LLM stage run whose plan line sets no ``budget.max_usd``."""

    model_config = ConfigDict(extra="forbid")

    per_run_usd: float


class ModelPrice(BaseModel):
    """USD per 1M tokens, input / output: the price a ledger **reservation** is computed at
    (D36), and the actual cost when a response carries no ``usage.cost``. ``retired``: the entry
    only prices historical rows (eval combine's usage backfill over old response-cache entries);
    no role may name the model and the client refuses it for new calls
    (:class:`stack.llm.ModelRetired`)."""

    model_config = ConfigDict(extra="forbid")

    in_usd_per_m: float = Field(ge=0)
    out_usd_per_m: float = Field(ge=0)
    retired: bool = False
    #: D714: the cached-input and cache-write (30-minute) rates, used only by :meth:`cost` when a reply reports
    #: cached / cache-write tokens and carries no ``usage.cost`` (a direct OpenAI call); unset = ``in_usd_per_m``.
    cached_in_usd_per_m: float | None = Field(default=None, ge=0)
    cache_write_usd_per_m: float | None = Field(default=None, ge=0)
    #: Long-context pricing: a call with more than ``long_above_tokens`` prompt tokens is priced at ``long``.
    long_above_tokens: int | None = Field(default=None, ge=1)
    long: ModelPrice | None = None

    def cost(self, tokens_in: int, tokens_out: int, cached: int = 0, cache_write: int = 0) -> float:
        """USD for one call: ``tokens_in`` prompt tokens of which ``cached`` were read from and ``cache_write``
        written to the provider's prompt cache (each at its rate when set), plus ``tokens_out``."""
        if self.long is not None and self.long_above_tokens is not None and tokens_in > self.long_above_tokens:
            return self.long.cost(tokens_in, tokens_out, cached, cache_write)
        cached_rate = self.in_usd_per_m if self.cached_in_usd_per_m is None else self.cached_in_usd_per_m
        write_rate = self.in_usd_per_m if self.cache_write_usd_per_m is None else self.cache_write_usd_per_m
        fresh = max(tokens_in - cached - cache_write, 0)
        return (fresh * self.in_usd_per_m + cached * cached_rate + cache_write * write_rate
                + tokens_out * self.out_usd_per_m) / 1_000_000


class PricingConfig(BaseModel):
    """Per-model prices plus the fallback for any model without an entry (v3's
    ``_FALLBACK_PRICING``, $3/$15 per 1M)."""

    model_config = ConfigDict(extra="forbid")

    fallback: ModelPrice = ModelPrice(in_usd_per_m=3.0, out_usd_per_m=15.0)
    models: dict[str, ModelPrice] = {}


class LlmConfig(BaseModel):
    """Provider, roles, egress classes, budget defaults, reservation pricing."""

    model_config = ConfigDict(extra="forbid")

    version: str
    provider: ProviderConfig
    #: D714: further transports a role may name (``roles.<r>.provider``); the default is ``provider``.
    providers: dict[str, ProviderConfig] = {}
    roles: dict[str, RoleConfig]
    egress: EgressConfig
    budget: BudgetConfig
    pricing: PricingConfig = PricingConfig()

    @model_validator(mode="after")
    def _no_retired_roles(self) -> LlmConfig:
        bad = sorted({f"{role}: {m}" for role, rc in self.roles.items() for m in [rc.model, *rc.fallbacks]
                      if self.is_retired(m)})
        if bad:
            raise ValueError(f"roles name retired models (pricing.models.<id>.retired): {bad}")
        unknown = sorted(f"{role}: {rc.provider}" for role, rc in self.roles.items()
                         if rc.provider is not None and rc.provider not in self.providers)
        if unknown:
            raise ValueError(f"roles name providers not in llm.providers ({sorted(self.providers)}): {unknown}")
        bad_refs: list[str] = []
        for role, rc in self.roles.items():
            for m in [rc.model, *rc.fallbacks]:
                try:
                    self.check_route(self.route(m, rc), rc)
                except ValueError as exc:
                    bad_refs.append(f"{role}: {exc}")
        if bad_refs:
            raise ValueError(f"roles name models their provider cannot take: {sorted(bad_refs)}")
        return self

    def provider_of(self, role: RoleConfig) -> ProviderConfig:
        """The transport ``role``'s calls go to: its ``provider`` entry, else the default ``provider``. A
        provider-qualified model reference overrides it per model (:meth:`route`)."""
        return self.provider if role.provider is None else self.providers[role.provider]

    def route(self, model: str, role: RoleConfig) -> ModelRoute:
        """Where ``model`` (a reference: the role's model, a fallback, a plan pin's model) goes when ``role`` asks
        it (D722): its qualifier's provider, else the role's ``provider``, else the default. The egress slug is the
        provider's ``egress_provider`` when set (a direct API), else the model id's prefix through
        ``egress.provider_prefixes``. Raises ``ValueError`` for a qualifier naming no ``llm.providers`` entry."""
        qual, model_id = split_model_ref(model)
        name = qual if qual is not None else role.provider
        if name is not None and name not in self.providers:
            raise ValueError(f"model {model!r}: provider {name!r} is not in llm.providers ({sorted(self.providers)})")
        provider = self.provider if name is None else self.providers[name]
        slug = provider.egress_provider
        if slug is None:
            slug = self.egress.provider_prefixes.get(model_id.split("/", 1)[0])
        return ModelRoute(provider_name=name, provider=provider, model_id=model_id, egress_slug=slug)

    @staticmethod
    def check_route(route: ModelRoute, role: RoleConfig) -> None:
        """Refuse (``ValueError``) a role setting its route's dialect cannot send: on ``dialect: anthropic`` (API
        version 2026-10-21) any ``temperature`` is an HTTP 400, and effort ``minimal`` has no Anthropic value."""
        if route.provider.dialect != "anthropic":
            return
        if role.temperature is not None:
            raise ValueError(f"{route.model_id}: temperature is not accepted by the Anthropic API (2026-10-21)")
        if role.reasoning_effort == "minimal":
            raise ValueError(f"{route.model_id}: reasoning_effort minimal has no Anthropic effort (low is the least)")

    def key_envs(self, role: RoleConfig, models: tuple[str, ...] | list[str] = ()) -> set[str]:
        """The env var NAMES of the keys ``role``'s calls may need: the providers of its model, its fallbacks and
        ``models`` (a plan pin's model references, routed under this role). For a deploy preflight (D722)."""
        return {self.route(m, role).provider.api_key_env for m in [role.model, *role.fallbacks, *models]}

    def _price_entry(self, model: str) -> ModelPrice | None:
        """The price entry of a model reference: its own, else its model id's (``anthropic:claude-opus-5-5`` is
        priced by ``claude-opus-5-5``, D722)."""
        price = self.pricing.models.get(model)
        return price if price is not None else self.pricing.models.get(split_model_ref(model)[1])

    def price_for(self, model: str) -> ModelPrice:
        """The model's price entry, else the fallback (never a cheaper guess)."""
        price = self._price_entry(model)
        return price if price is not None else self.pricing.fallback

    def is_retired(self, model: str) -> bool:
        """``model`` has a ``retired`` price entry: never called again (:class:`ModelPrice`)."""
        price = self._price_entry(model)
        return price is not None and price.retired


# ─────────────────────────── policies.yaml ─────────────────────────────────


class AnchoringConfig(BaseModel):
    """Quote→span acceptance threshold (§6.2), with per-workflow overrides."""

    model_config = ConfigDict(extra="forbid")

    anchor_min_score: float
    per_workflow: dict[str, float] = {}


class PoliciesConfig(BaseModel):
    """The whole ``policies.yaml``: the anchoring threshold (v3's other read-time policies, read by
    no v4 code, are in v3's ``configs/policies.yaml``; D438)."""

    model_config = ConfigDict(extra="forbid")

    version: str
    anchoring: AnchoringConfig


# ─────────────────────────── StackConfig ───────────────────────────────────


@dataclass(frozen=True)
class StackConfig:
    """The loaded, validated, stamped config registry — the sweep-pinned object.

    ``policy_file`` is the ``policies.yaml`` model (``anchoring``); ``policies_cfg`` is the backing
    field. (v3's named admission policies, ``.policies[name]``, went with the rest of v3's
    ``policies.yaml`` to v3's ``configs/``.)
    """

    config_dir: Path
    enums: EnumsConfig
    registry: RegistryConfig
    taxonomy_file: TaxonomyFile
    channels: ChannelsConfig
    llm: LlmConfig
    policies_cfg: PoliciesConfig
    stamps: Mapping[str, str]

    @property
    def taxonomy(self) -> TaxonomyConfig:
        """The pinned taxonomy, parsed, validated and cross-checked (9) on first use."""
        return self.taxonomy_file.parsed()

    @property
    def policy_file(self) -> PoliciesConfig:
        """The whole ``policies.yaml`` model (``anchoring``)."""
        return self.policies_cfg


# ─────────────────────────── loading helpers ───────────────────────────────

_M = TypeVar("_M", bound=BaseModel)


def _read_bytes(path: Path, name: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{name}: cannot read '{path}': {exc}") from exc


def _parse_yaml(raw: bytes, path: Path, name: str) -> Any:
    try:
        return yamlio.load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{name}: YAML parse error in '{path.name}': {exc}") from exc


def _read_yaml(path: Path, name: str) -> tuple[Any, bytes]:
    """Read raw bytes (for stamping) and parse YAML, wrapping failures in ConfigError."""
    raw = _read_bytes(path, name)
    return _parse_yaml(raw, path, name), raw


#: The taxonomy's top-level ``version:`` line (the stamp's version, read without parsing the file).
_TOP_VERSION_RE = re.compile(rb"^version:[ \t]*(\S[^#\n]*?)[ \t]*(?:#[^\n]*)?$", re.MULTILINE)


class TaxonomyFile:
    """The pinned taxonomy file: its bytes and ``version`` at :func:`load`, the parsed
    :class:`TaxonomyConfig` on the first :meth:`parsed` (then cached; copies of a
    :class:`StackConfig` made by ``dataclasses.replace`` share it)."""

    def __init__(self, path: Path, filename: str, raw: bytes) -> None:
        self.path, self.filename, self.raw = path, filename, raw
        found = _TOP_VERSION_RE.findall(raw)
        if len(found) != 1:
            raise ConfigError(f"taxonomy ('{filename}'): expected one top-level `version:` line, "
                              f"found {len(found)}")
        try:
            version = yaml.safe_load(found[0])
        except yaml.YAMLError as exc:
            raise ConfigError(f"taxonomy ('{filename}'): unreadable `version:` value: {exc}") from exc
        if not isinstance(version, str):
            raise ConfigError(f"taxonomy ('{filename}'): `version:` must be a string, got {version!r}")
        self.version: str = version
        self._parsed: TaxonomyConfig | None = None

    def parsed(self) -> TaxonomyConfig:
        """Parse, validate and cross-check (9) the file once; :class:`ConfigError` on any failure."""
        if self._parsed is None:
            data = _parse_yaml(self.raw, self.path, "taxonomy")
            taxonomy = _validate(TaxonomyConfig, data, "taxonomy", self.filename)
            problems = []
            if taxonomy.version != self.version:
                problems.append(f"taxonomy ('{self.filename}'): parsed version '{taxonomy.version}' != "
                                f"its `version:` line '{self.version}' (the stamp's)")
            problems += _taxonomy_checks(taxonomy)
            if problems:
                joined = "\n".join(f"  - {p}" for p in problems)
                raise ConfigError(f"config cross-check failed ({len(problems)} issue(s)):\n{joined}")
            self._parsed = taxonomy
        return self._parsed


def _validate(model_cls: type[_M], data: Any, name: str, filename: str) -> _M:
    """Validate ``data`` against ``model_cls``, wrapping ValidationError with the file name."""
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"{name} ('{filename}') failed schema validation:\n{exc}"
        ) from exc


def _stamp(version: str, raw: bytes) -> str:
    """Config stamp per 20-technical §3: ``<semver>+<first 8 hex of sha256(file bytes)>``."""
    return f"{version}+{hashlib.sha256(raw).hexdigest()[:8]}"


def _resolve_dir(dir: Path | None) -> Path:
    """Resolve the config dir: arg → ``STACK_CONFIG_DIR`` → ``<repo_root>/configs``."""
    if dir is not None:
        return dir
    env = os.environ.get("STACK_CONFIG_DIR")
    if env:
        return Path(env)
    # repo root = parent of the stack/ package (this file is stack/config.py); configs/ is
    # the live top-level registry (graduated from spec/examples/config).
    return Path(__file__).resolve().parent.parent / "configs"


def _cross_checks(
    *,
    enums: EnumsConfig,
    registry: RegistryConfig,
    taxonomy: TaxonomyFile,
    llm: LlmConfig,
) -> list[str]:
    """Return the load-time cross-check problems (empty list == consistent). See module
    docstring 1, 7; 9 is :func:`_taxonomy_checks`, run by :meth:`TaxonomyFile.parsed`."""
    problems: list[str] = []
    license_classes = (
        set(enums.vocab("license_class")) if "license_class" in enums.enums else set()
    )

    # 1. Taxonomy pin version match (file existence is enforced before this, at load).
    if taxonomy.version != registry.taxonomy.active:
        problems.append(
            f"taxonomy pin: registry.taxonomy.active '{registry.taxonomy.active}' != "
            f"pinned file version '{taxonomy.version}' ('{registry.taxonomy.file}')"
        )

    # 7. license_class key-sets single-sourced from enums.license_class.
    egress_keys = set(llm.egress.by_license_class)
    if license_classes and egress_keys and egress_keys != license_classes:
        problems.append(
            f"llm.egress.by_license_class keys {sorted(egress_keys)} != "
            f"enums.license_class {sorted(license_classes)}"
        )
    return problems


def _taxonomy_checks(taxonomy: TaxonomyConfig) -> list[str]:
    """Cross-check 9: taxonomy exclusive_with / absorbs reference the facet's own menu values."""
    problems: list[str] = []
    for facet in taxonomy.facets:
        menu = {v.slug for v in facet.values}
        for group in facet.exclusive_with:
            for value in group:
                if value not in menu:
                    problems.append(
                        f"taxonomy facet '{facet.slug}': exclusive_with references '{value}' "
                        f"absent from its menu {sorted(menu)}"
                    )
        for keeper, absorbed in facet.absorbs.items():
            if keeper not in menu:
                problems.append(
                    f"taxonomy facet '{facet.slug}': absorbs key '{keeper}' absent from its "
                    f"menu {sorted(menu)}"
                )
            for value in absorbed:
                if value not in menu:
                    problems.append(
                        f"taxonomy facet '{facet.slug}': absorbs['{keeper}'] references "
                        f"'{value}' absent from its menu {sorted(menu)}"
                    )

    return problems


def load(dir: Path | None = None) -> StackConfig:
    """Parse, schema-validate, cross-check, and stamp the six config files.

    ``dir`` defaults to ``STACK_CONFIG_DIR`` then ``<repo_root>/configs`` (the live top-level
    registry, graduated from ``spec/examples/config``). The taxonomy is resolved
    through ``registry.yaml``'s pin. Fails loud and early: any parse, validation, or
    cross-check failure (or a leftover v3 ``workflows.yaml``) raises :class:`ConfigError`
    before pipeline step one.
    """
    config_dir = _resolve_dir(dir)
    if (config_dir / "workflows.yaml").exists():
        raise ConfigError(
            f"'{config_dir / 'workflows.yaml'}': workflows.yaml was retired at T4.5 (D42); stage "
            "registration lives in the stage modules and the plan files (configs/plans/) — "
            "delete the file or point STACK_CONFIG_DIR at the live configs/"
        )

    # Fixed-name files: read bytes (for stamps) + validate.
    raws: dict[str, bytes] = {}
    fixed_data: dict[str, Any] = {}
    for name, filename in _FIXED_FILES.items():
        data, raw = _read_yaml(config_dir / filename, name)
        fixed_data[name] = data
        raws[name] = raw

    enums = _validate(EnumsConfig, fixed_data["enums"], "enums", "enums.yaml")
    registry = _validate(RegistryConfig, fixed_data["registry"], "registry", "registry.yaml")
    channels = _validate(ChannelsConfig, fixed_data["channels"], "channels", "channels.yaml")
    llm = _validate(LlmConfig, fixed_data["llm"], "llm", "llm.yaml")
    policies_cfg = _validate(PoliciesConfig, fixed_data["policies"], "policies", "policies.yaml")

    # Taxonomy pin: the file must resolve under the config dir (else we cannot stamp it).
    tax_file = registry.taxonomy.file
    tax_path = config_dir / tax_file
    if not tax_path.exists():
        raise ConfigError(
            f"taxonomy: registry pin file '{tax_file}' does not resolve under '{config_dir}'"
        )
    taxonomy = TaxonomyFile(tax_path, tax_file, _read_bytes(tax_path, "taxonomy"))  # parsed on first use

    # Stamps (20-technical §3): over raw file bytes, versioned by each file's own `version`.
    stamps: dict[str, str] = {
        "enums": _stamp(enums.version, raws["enums"]),
        "registry": _stamp(registry.version, raws["registry"]),
        "taxonomy": _stamp(taxonomy.version, taxonomy.raw),
        "channels": _stamp(channels.version, raws["channels"]),
        "llm": _stamp(llm.version, raws["llm"]),
        "policies": _stamp(policies_cfg.version, raws["policies"]),
    }

    problems = list(
        _cross_checks(
            enums=enums,
            registry=registry,
            taxonomy=taxonomy,
            llm=llm,
        )
    )
    if problems:
        joined = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"config cross-check failed ({len(problems)} issue(s)):\n{joined}")

    return StackConfig(
        config_dir=config_dir,
        enums=enums,
        registry=registry,
        taxonomy_file=taxonomy,
        channels=channels,
        llm=llm,
        policies_cfg=policies_cfg,
        stamps=stamps,
    )
