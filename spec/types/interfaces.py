"""Stack pipeline v3 — shared-service interfaces (normative signatures, no implementation).

This is the ``types/interfaces.py`` artifact of 20-technical §10: the Python ``Protocol``
signatures for the §9 shared services, plus the dataclasses / typed dicts for the value
types that cross those boundaries. It specifies *shapes*, not behaviour — every method body
is ``...``. An implementation is any object that structurally satisfies the Protocol
(``typing.Protocol``, so no inheritance is required); the contracts these encode are in
10-design §9 and 20-technical §5-§9.

Type-check with::

    uv run --with mypy mypy spec/types/interfaces.py

Nothing here imports a runtime dependency; the module is import-safe and pure typing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Literal,
    Mapping,
    NotRequired,
    Protocol,
    Required,
    Sequence,
    TypedDict,
    overload,
    runtime_checkable,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shared literal domains (mirror enums.yaml; the writing library validates values)
# ─────────────────────────────────────────────────────────────────────────────

Bucket = Literal["raw", "text", "derived"]
"""The three content-addressed S3 buckets (10-design §4.6)."""

AnchorMethod = Literal["exact", "ws", "fold", "fuzzy"]
"""Anchoring tier that located a quote, in strength order (20-technical §6)."""

RunStatus = Literal["running", "ok", "failed", "halted"]
Phase = Literal["collect", "fetch", "convert", "annotate", "aggregate"]
Origin = str  # per-table domains: annotations/metadata/aggregates use
# 'ai' | 'rule' | 'auto' | 'human:<who>' | 'check:<wf>'; sources.duplicate_info['origin']
# uses 'human:<who>' | 'wf:<id@ver>'. masks/reviews are always 'human:<who>'.
LicenseClass = Literal["open", "public_web", "restricted", "shadow", "api_tos"]
ReceiptOutcome = Literal["ok", "empty", "failed"]


# ─────────────────────────────────────────────────────────────────────────────
# Value types — payload fragments that cross a service boundary
# ─────────────────────────────────────────────────────────────────────────────


class SelectionItem(TypedDict):
    """One entry of a subject-axis receipt's ``selection`` (receipt.schema.json).
    ``snapshot_id``/``reason`` are required (schema), the rest optional."""

    snapshot_id: Required[str]
    reason: Required[str]
    subject: NotRequired[str]
    rank: NotRequired[int]
    tier: NotRequired[str]


class Receipt(TypedDict):
    """The mandatory processing receipt payload, exactly 20-technical §5.

    ``n_records``/``outcome``/``attempts`` are required; ``error``/``selection`` optional —
    expressed with ``Required``/``NotRequired`` (available at the pinned requires-python
    >= 3.11). The schema is the normative gate; this is the ergonomic shape a Runner.receipt
    caller passes.
    """

    n_records: Required[int]
    outcome: Required[ReceiptOutcome]
    attempts: Required[int]
    error: NotRequired[str]
    selection: NotRequired[list[SelectionItem]]


@dataclass(frozen=True)
class Anchor:
    """A located quote (20-technical §6). Offsets are ``[start, end)`` in Unicode code
    points into the exact text named by a snapshot's ``text_digest``."""

    span: tuple[int, int]
    method: AnchorMethod
    score: float
    occurrences: int


@dataclass(frozen=True)
class Grounding:
    """One envelope grounding entry (annotations.grounding element; 10-design §6.2).
    Payloads stay quote-free and reference these by position (``grounding_idx``)."""

    quote: str
    span: tuple[int, int]
    text_digest: str
    method: AnchorMethod
    score: float
    occurrences: int


@dataclass(frozen=True)
class CanonResult:
    """Output of a canonicalizer (§9.3): the matched registry slug, or ``None`` when the
    raw string matched no ``registry.yaml`` entry (caller then raises a ``proposal``)."""

    slug: str | None
    matched: bool
    rule: str | None = None  # the ordered first-match alias regex that fired, for provenance


@dataclass(frozen=True)
class LlmResult:
    """The result of one logical LLM completion (§9.1 / 20-technical §8).

    Carries the parsed structured output plus the ledger fields the client writes to
    ``llm_calls``: the two hashes (``rendered_hash`` keys the response cache, ``template_hash``
    feeds the prompt-drift guard — 20-technical §3, §8), the EXEC-6 token split, and cost.
    """

    parsed: Mapping[str, Any]
    model: str
    ok: bool
    cached: bool
    attempt: int
    cost_usd: float
    tokens_in: int
    tokens_in_fixed: int
    tokens_out: int
    rendered_hash: str
    template_hash: str
    input_digest: str
    response_digest: str | None = None


@dataclass(frozen=True)
class Doc:
    """One row of the ``corpus`` view (0002_views.sql): a live representative source
    (``duplicate_of IS NULL``) joined to its usable latest snapshot and effective metadata.
    The default annotate-scope atom. Group identity is display-only (10-design §3): the
    group is named by this row's ``source_id``; there is deliberately no group_id field."""

    source_id: str
    snapshot_id: str
    channel: str
    license_class: str
    text_digest: str
    text_cp: int
    text_quality: str
    seq: int
    observed_at: str
    doc_type_eff: str | None = None
    title: str | None = None
    pub_date: str | None = None
    venue_type: str | None = None
    sensitivity: str | None = None
    self_published: bool | None = None
    canonical_url: str | None = None  # exposed for explain()/debug (D-NIT-9)
    external_id: str | None = None


@dataclass(frozen=True)
class EffectiveMeta:
    """Read-time merge of a source's metadata records (10-design §6.5). Common fields are
    surfaced explicitly; ``extra`` holds any further merged fields per policies.yaml."""

    source_id: str
    title: str | None = None
    pub_date: str | None = None
    venue_type: str | None = None
    doc_type: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Annotation:
    """A read-facing annotation record (canonical row from ``annotations_canon`` unless a
    filter asks otherwise). A read-facing subset of the durable columns (omits e.g.
    ``template_hash``, ``stamps``, ``cost_usd``); ``payload`` is the validated per-ann_type
    body, ``grounding`` the verified envelope entries."""

    ann_id: str
    snapshot_id: str
    source_id: str
    workflow: str
    workflow_version: int
    ann_type: str
    item_key: str
    subjects: Sequence[str]
    payload: Mapping[str, Any]
    origin: Origin
    run_id: str
    created_at: str
    grounding: Sequence[Grounding] = ()
    flags: Mapping[str, Any] | None = None
    model: str | None = None
    confidence: float | None = None
    overrides: str | None = None
    call_id: str | None = None  # the llm_calls row that produced this annotation (G-S7; None for rule/human/receipt)


@dataclass(frozen=True)
class Aggregate:
    """A read-facing aggregate record (canonical row from ``aggregates_canon``)."""

    agg_id: str
    workflow: str
    workflow_version: int
    subject_key: str
    value: Mapping[str, Any]
    inputs_digest: str
    change_cause: str | None
    origin: Origin
    run_id: str
    created_at: str


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer to a member manifest when an admitted set exceeds 5,000 rows (§7.1) — the
    normal path for corpus-wide rollups, not an overflow accident."""

    artifact: str  # s3://stack-derived/... key
    n: int


Member = tuple[str, Origin]
"""One admitted input as ``[id, origin]`` (7.1 ``input_members`` element)."""


@dataclass(frozen=True)
class AdmitResult:
    """Output of the single admission choke point (§7.2). ``rows`` are the admitted
    annotations with their computed badges; ``members`` is the verbatim ``[[id, origin], …]``
    for the inputs digest, or an ``ArtifactRef`` to the member manifest."""

    rows: Sequence[Annotation]
    members: Sequence[Member] | ArtifactRef
    badges: Mapping[str, Sequence[str]] = field(default_factory=dict)  # ann_id -> badges


@dataclass(frozen=True)
class Explanation:
    """The ``explain(workflow, subject_key)`` trace surface (§9.4): every hop as a recorded
    field — admitted members with origins, their groundings, the masks/reviews/overrides that
    bit, and the config stamps in force."""

    workflow: str
    subject_key: str
    members: Sequence[Member]
    groundings: Mapping[str, Sequence[Grounding]]  # ann_id -> its groundings
    overrides_applied: Sequence[tuple[str, str]]  # (winning_id, overridden_id)
    masks_applied: Sequence[str]  # source_ids hidden by an active mask
    reviews_applied: Sequence[str]  # review_ids that set a human_vetted badge
    stamps: Mapping[str, str]  # config ref -> "<semver>+<hash8>"


@dataclass(frozen=True)
class RunCounts:
    """The per-step funnel counters written onto the ``runs`` row (§10 stats)."""

    eligible: int
    processed: int
    ok_n: int
    skipped: int
    failed: int


# ─────────────────────────────────────────────────────────────────────────────
# Service protocols (§9)
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class BlobStore(Protocol):
    """Content-addressed blob storage over the three S3 buckets (§4.6). Keys are the
    sha256 hex digest of the bytes; writes are idempotent and precede the DB rows that
    reference them (§4.1)."""

    def put(self, data: bytes, *, bucket: Bucket) -> str:
        """Store ``data`` content-addressed; return its sha256 hex digest (the key)."""
        ...

    def get(self, digest: str, *, bucket: Bucket) -> bytes:
        """Fetch the bytes for ``digest`` (via the mandatory local cache for text)."""
        ...

    def exists(self, digest: str, *, bucket: Bucket) -> bool:
        """Whether an object with ``digest`` is present in ``bucket``."""
        ...


@runtime_checkable
class Writer(Protocol):
    """The minimal shared write surface (§9, G-S1): id minting, the content hashes, the BlobStore,
    and schema+enum-validated inserts — the "writer core" the README build order puts first.

    EXEC-4 blesses an ALTERNATIVE legal write path: direct SQL guarded by the DDL's own triggers
    (append-only + write-once field groups) plus a ``stack lint-db`` sweep validating recent rows
    (enums, schemas, orphan refs, denorm consistency). A workflow need not import this Protocol; but
    a library ``Writer`` implements it once so every phase mints ids, hashes text, and inserts
    validated rows identically instead of four divergent copies (the whole point of the spec)."""

    def mint_id(self, prefix: str) -> str:
        """A fresh row id: the type-letter ``prefix`` + 12 Crockford base32 chars (§1); relies on the
        PK constraint and regenerates on the (vanishingly rare) collision."""
        ...

    def content_hashes(self, text: str) -> tuple[str, str]:
        """``(text_digest, content_key)`` for one converted text — the exact §3 recipes (text_digest
        over the stored bytes; content_key over the normalized text for the materiality axis)."""
        ...

    @property
    def blobs(self) -> BlobStore:
        """The content-addressed blob store (§4.6). Blob writes precede the DB rows referencing them;
        an orphaned object after a failed insert is benign (content-addressed, rewritten idempotently)."""
        ...

    def insert(self, table: str, row: Mapping[str, Any]) -> str:
        """Insert one row into ``table`` after validating its enum-valued columns against
        ``enums.yaml`` and any JSONB payload against the schema registered in ``workflows.yaml``;
        return the row id. The DDL guards still enforce append-only / write-once underneath."""
        ...


@runtime_checkable
class LlmClient(Protocol):
    """Role-based LLM client (§9.1 / 20-technical §8): egress gate by ``license_class`` →
    cache lookup → budget pre-flight → schema-constrained call with ≤2 recorded retries →
    ledger + body artifact. Raises a fatal error on auth/credit failures (abort, resumable)."""

    def complete(
        self,
        *,
        role: str,
        template: str,
        refs: Mapping[str, str],
        user: str,
        schema: Mapping[str, Any],
        license_class: LicenseClass,
        max_tokens: int | None = None,
    ) -> LlmResult:
        """Run one logical completion for ``role`` with a structured-output ``schema``.

        The client owns rendering AND both hashes (D-M8): ``template`` is the fixed-prefix
        TEMPLATE form (system + config-rendered menus/rubrics with ``{{ref:<kind>}}``
        placeholders, from ``workflows/<phase>/<name>/<role>.md``); ``refs`` maps each placeholder kind
        to its rendered config text. The client substitutes ``refs`` into ``template`` to get the
        rendered system prefix (stable within a run for provider prompt caching), computes
        ``rendered_hash`` over the rendered prefix and ``template_hash`` over the unrendered
        template, and returns both on ``LlmResult``. ``user`` is the per-document content, fenced
        as data. ``license_class`` selects allowed providers via llm.yaml (each attempted model,
        including fallbacks, re-gated by its provider prefix); a violation refuses before any
        bytes leave.
        """
        ...


@runtime_checkable
class Anchorer(Protocol):
    """Weighted-cost quote anchorer (§9.2 / 20-technical §6)."""

    def anchor(self, quote: str, text: str) -> Anchor | None:
        """Locate ``quote`` in ``text`` by the exact→ws→fold→fuzzy tiers with category-weighted
        edit costs and the hard digit-run rule; return the ``Anchor`` or ``None`` on rejection
        (score below ``anchor_min_score``, digit-run mismatch, or ``quote_too_long``)."""
        ...


@runtime_checkable
class Canonicalizer(Protocol):
    """Config-pure entity canonicalizers (§9.3): first-match over ``registry.yaml`` regexes.
    Referentially transparent — safe to re-run on any registry change."""

    def canon_model(self, raw: str) -> CanonResult:
        """Canonicalize a raw model surface to a registry slug (or unmatched)."""
        ...

    def canon_org(self, raw: str) -> CanonResult:
        """Canonicalize a raw org surface to a registry slug (or unmatched)."""
        ...


@runtime_checkable
class ReadApi(Protocol):
    """The corpus read API (§9.4): the canonical read surface, SQL-view-backed so non-Python
    consumers get identical semantics."""

    def docs(self, filter: Mapping[str, Any] | None = None) -> Sequence[Doc]:
        """Rows of the ``corpus`` view under an optional filter."""
        ...

    def text(self, snapshot_id: str) -> str:
        """The snapshot's canonical Markdown (via the mandatory local content-addressed
        cache); the grounding frame that spans address."""
        ...

    def raw(self, snapshot_id: str) -> tuple[bytes, str] | None:
        """The snapshot's raw bytes + media_type, or None when unavailable (D-RAW1).

        A reasoning workflow reads only its OWN in-scope unit's raw. None covers absent or
        pruned raw and a missing blob, and must degrade the caller's tier — never fail the
        unit. Never a network fetch."""
        ...

    def effective_meta(self, source_id: str) -> EffectiveMeta:
        """The read-time metadata merge for a source (§6.5)."""
        ...

    def annotations(self, filter: Mapping[str, Any] | None = None) -> Sequence[Annotation]:
        """Canonical annotations under an optional filter. Reads the ``annotations_live`` view
        (0002_views.sql, G-S5), NOT raw ``annotations_canon``: it is the canonical row projected onto
        current snapshots and live representative sources (``duplicate_of IS NULL``) — so a subject
        query never silently returns superseded snapshots, masked sources, or duplicate rows.
        ``point_in_time`` ann_types (historical facts) are the exception and are read from
        ``annotations_canon`` directly."""
        ...

    def pending_units(self, workflow: str, version: int, current_run: str) -> Sequence[str]:
        """Snapshot ids still needing processing at (workflow@version) — wraps the
        ``pending_units()`` SQL function (0002_views.sql; 20-technical §5), supplying
        ``quarantine_after`` from policies.yaml. Doc-axis form; subject-axis workflows derive
        their own (subject, snapshot) candidates per §5."""
        ...

    def aggregates(self, workflow: str, subject: str) -> Sequence[Aggregate]:
        """Canonical aggregates for a workflow and subject key."""
        ...

    def admit(self, scope: str, policy: str) -> AdmitResult:
        """The one reduce-input choke point (§7.2): scope SQL → latest-snapshot projection →
        mask filter → canonical-record rule → policy admission with badges → members. Receipts
        (``ann_type='receipt'``) are excluded unless the scope names them. The §7 step-2
        ``point_in_time`` exemption is resolved per row via ``row.workflow`` against the
        workflow registry (the producing workflow's registration), not from this signature."""
        ...

    @overload
    def explain(self, workflow: str, subject_key: str) -> Explanation: ...
    @overload
    def explain(self, *, agg_id: str) -> Explanation: ...
    def explain(
        self,
        workflow: str | None = None,
        subject_key: str | None = None,
        *,
        agg_id: str | None = None,
    ) -> Explanation:
        """Full provenance trace (§9.4). Two targets: ``(workflow, subject_key)`` replays ``admit``
        against the CURRENT canonical row; ``agg_id=…`` traces a specific STORED aggregate from its
        recorded ``input_members`` (inline or via the member manifest), so a historical/published aggregate stays
        auditable even after the live row moved on (G-S6). An export bundle embeds the same trace —
        members, groundings, config stamps — so a DB-less auditor can verify it offline."""
        ...


@runtime_checkable
class Runner(Protocol):
    """Run-lifecycle hooks (§10 / 20-technical §9). A non-library implementation honouring
    these plus the DDL is a conformant workflow; the sequencer only checks exit codes and a
    ``runs`` row."""

    def open_run(
        self,
        *,
        workflow: str,
        phase: Phase,
        workflow_version: int | None,
        stamps: Mapping[str, Any],
        sweep_id: str | None = None,
        gated: bool = False,
    ) -> str:
        """Take the session-scoped advisory lock, then: any ``running|halted`` run at (workflow,
        workflow_version) is the RESUME target (re-open the same row — the free lock proves no
        live process; G-S3); open a fresh ``runs`` row (``status='running'``,
        ``accepted = NOT gated``) only when none exists. Refuses to open when a ``bump_on``
        config's stamp differs from the most recent accepted run at this version, unless
        ``--accept-config-drift`` (G-M3). Returns the ``run_id``."""
        ...

    def close_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        counts: RunCounts,
        cost_usd: float | None = None,
        tokens: Mapping[str, int] | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Set terminal ``status`` + ``finished_at`` and the funnel counters. For a gated run,
        print the closing stats plus the ADVISORY ``generation_gate`` warnings (count ratio,
        void rate) and leave the run inert — acceptance is the separate manual operator action
        ``stack accept <run>``, whose one ``runs.accepted`` UPDATE makes the whole generation
        visible (§9)."""
        ...

    def receipt(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        source_id: str,
        workflow: str,
        workflow_version: int,
        payload: Receipt,
        item_key: str = "",
    ) -> str:
        """Write the mandatory ``ann_type='receipt'`` row for one processed unit and return its
        ``ann_id``. Doc-axis: ``item_key=''`` (identity ``(snapshot, workflow, 'receipt', '')``).
        Subject-axis: ``item_key`` = the subject atom, one receipt per (snapshot, subject) so two
        techniques selecting the same snapshot do not collide (§5, D-M5)."""
        ...
