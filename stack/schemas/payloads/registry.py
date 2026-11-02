"""The payload registry (A.4, §3.5): one entry per annotation workflow, in registry order.

Registry order is the field order of the ``annotations`` block and of the derived ``<wf>`` /
``<wf>_evidence`` families, so appending a workflow is a schema bump of ``documents`` (D43) and
reordering one is too. ``text_bearing`` paths use ``a.b[].c`` (``[]`` = every list element) and
are relative to the payload struct.

**Annotation records** (``produced_by``). An entry whose ``produced_by`` names another workflow
is a *record*: a typed annotation that the producer's stage writes from the same invocation (one
screen call answers several questions; each answer is its own record). A record has its own
``annotations.<record>`` field and ``<record>`` family, but no evidence family: the producer's
``<producer>_evidence`` carries one nullable struct column per record (:func:`records_of`), and the
record's rows reference the producer's evidence rows. A record shares the producer's
``basis_kind`` and is document grain; ``evidence`` says whether its producer keeps evidence.
Which records one run writes depends on the producer's plan pin (``Workflow.records``).

**Basis reads** (``reads``, D235). An entry whose ask consumes other annotations' selected results
names them in ``reads`` (workflows or records, each an *earlier* entry, so registry order is a
topological order); its ``basis_kind`` is ``chained`` (and only such an entry's): the basis is the
current text plus each read's typed ``ANN`` status and payload (:func:`stack.fold.chained_basis`).
A plan stage line's own ``reads:`` joins annotations for the scope filter only (not the basis);
:func:`joined_reads` is the union the stage receives.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

from stack.schemas.payloads.affiliation import AFFILIATION, AFFILIATION_ORGS
from stack.schemas.payloads.doc_class import DOC_CLASS
from stack.schemas.payloads.model_roles import MODEL_REGEXP_MATCHES, MODEL_ROLES
from stack.schemas.payloads.relevance import RELEVANCE, SR_RELEVANCE
from stack.schemas.payloads.screen import SCREEN
from stack.schemas.payloads.technique_roles import TECHNIQUE_ROLES
from stack.schemas.payloads.technique_staged import TECHNIQUE_STAGED
from stack.schemas.payloads.translation import TRANSLATION
from stack.schemas.payloads.triage import TRIAGE
from stack.schemas.payloads.usability import USABILITY
from stack.schemas.vocab import BASIS_KINDS, GRAINS


@dataclass(frozen=True)
class PayloadSpec:
    """One registry entry: ``{workflow, version, payload_schema, grain, text_bearing, evidence,
    basis_kind, produced_by, reads}`` (A.4); ``produced_by`` defaults to ``workflow``, ``reads``
    to none (module docstring)."""

    workflow: str
    version: int
    payload_schema: pa.StructType
    grain: str
    text_bearing: tuple[str, ...]
    evidence: bool
    basis_kind: str
    produced_by: str = ""
    reads: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.produced_by:
            object.__setattr__(self, "produced_by", self.workflow)
        if bool(self.reads) != (self.basis_kind == "chained"):
            raise ValueError(f"{self.workflow}: basis_kind 'chained' iff the entry reads other annotations "
                             f"(basis_kind {self.basis_kind!r}, reads {list(self.reads)})")
        if len(set(self.reads)) != len(self.reads):
            raise ValueError(f"{self.workflow}: reads repeats a name: {list(self.reads)}")
        if self.is_record and self.grain != "document":
            raise ValueError(f"{self.workflow}: a record (produced_by {self.produced_by!r}) is document grain")
        if self.grain not in GRAINS:
            raise ValueError(f"{self.workflow}: grain {self.grain!r} not in {GRAINS}")
        if self.basis_kind not in BASIS_KINDS:
            raise ValueError(f"{self.workflow}: basis_kind {self.basis_kind!r} not in {BASIS_KINDS}")
        if self.grain == "item":
            # Item grain: the payload is exactly one list<struct> whose struct carries item_key.
            only = list(self.payload_schema)
            if (
                len(only) != 1
                or not pa.types.is_list(only[0].type)
                or "item_key" not in [f.name for f in only[0].type.value_type]
            ):
                raise ValueError(
                    f"{self.workflow}: an item-grain payload is one list<struct(item_key, ...)>"
                )

    @property
    def is_record(self) -> bool:
        """Written by another workflow's stage (``produced_by != workflow``)."""
        return self.produced_by != self.workflow

    @property
    def evidence_family(self) -> str:
        """The family holding this entry's evidence: ``<produced_by>_evidence``."""
        return f"{self.produced_by}_evidence"


REGISTRY: dict[str, PayloadSpec] = {
    p.workflow: p
    for p in (
        PayloadSpec(
            workflow="triage",
            version=1,
            payload_schema=TRIAGE,
            grain="document",
            text_bearing=(),
            evidence=True,
            basis_kind="fields",
        ),
        PayloadSpec(
            workflow="screen",
            version=1,
            payload_schema=SCREEN,
            grain="document",
            text_bearing=("quotes[].text", "reasoning"),
            evidence=True,
            basis_kind="text",
        ),
        PayloadSpec(
            workflow="affiliation",
            version=1,
            payload_schema=AFFILIATION,
            grain="item",
            # Surfaces copied out of the text (T1.1 builder's reading; the design names only
            # screen's paths).
            text_bearing=("items[].author_surface", "items[].affiliations[].surface"),
            evidence=True,
            basis_kind="text",
        ),
        # A record of screen since round 5 (T-SCREEN; ``screen-v12`` answers question D in the screen
        # call, doc_type fixed by the channel where it decides): was a pure fields-basis workflow
        # that no stage ran. Position and payload unchanged, so ``annotations.doc_class`` and the
        # ``doc_class`` family keep their schemas; ``screen_evidence`` gains the ``doc_class`` column.
        PayloadSpec(
            workflow="doc_class",
            version=1,
            payload_schema=DOC_CLASS,
            grain="document",
            text_bearing=(),
            evidence=True,
            basis_kind="text",
            produced_by="screen",
        ),
        PayloadSpec(
            workflow="stack_relevance",
            version=1,
            payload_schema=RELEVANCE,
            grain="document",
            text_bearing=("quotes[].text",),
            evidence=True,
            basis_kind="text",
            produced_by="screen",
        ),
        PayloadSpec(
            workflow="sr_relevance",
            version=1,
            payload_schema=SR_RELEVANCE,
            grain="document",
            text_bearing=("quotes[].text",),
            evidence=True,
            basis_kind="text",
            produced_by="screen",
        ),
        PayloadSpec(
            workflow="text_usability",
            version=1,
            payload_schema=USABILITY,
            grain="document",
            text_bearing=("note",),
            evidence=True,
            basis_kind="text",
            produced_by="screen",
        ),
        # Round 5, T-ANN (D235): ported from v3 ``tomas/model-roles``. D591: one stage detects the
        # vocabulary models (the LLM-free matcher over ``configs/models.yaml``, ex-``model_mentions``,
        # removed) and grades the roles of its menu with one judge call; basis ``text`` (was ``chained``
        # over model_mentions), version 2. Version 3 (2026-10-17): every document is judged, an empty or
        # weak-only menu included (no answer without a call any more); the judge may refine a menu row to
        # a more specific node (``models[].matched_node_id`` keeps the menu node); the matcher's hits are
        # the record ``model_regexp_matches`` (were ``matches``/``ambiguous`` in the payload, D591).
        PayloadSpec(
            workflow="model_roles",
            version=3,
            payload_schema=MODEL_ROLES,
            grain="document",
            text_bearing=("models[].evidence[].quote", "models[].note", "variant_models[].quote",
                          "extra_models[].quote"),
            evidence=True,
            basis_kind="text",
        ),
        # A record of model_roles (as doc_class is of screen, D257): the LLM-free matcher's hits over
        # ``configs/models.yaml`` for the same document, written from the same invocation. Informative.
        PayloadSpec(
            workflow="model_regexp_matches",
            version=1,
            payload_schema=MODEL_REGEXP_MATCHES,
            grain="document",
            text_bearing=("matches[].surfaces[]", "ambiguous[].text"),
            evidence=True,
            basis_kind="text",
            produced_by="model_roles",
        ),
        # Round 5, T-ANN: ported from the off-DB prototype ``tomas/technique-roles`` (approach A).
        # One judge call grades the roles of the techniques found against the whole taxonomy
        # (v0.8) as the menu; basis ``text`` (binding techniques to models, which would read
        # model_roles, is later work).
        PayloadSpec(
            workflow="technique_roles",
            version=2,  # 2: loose quote tier, pipeline-set no_techniques_found, menu_gap flags from sweeps
            payload_schema=TECHNIQUE_ROLES,
            grain="document",
            text_bearing=("techniques[].quotes[].text", "techniques[].note", "extra_techniques[].quotes[].text",
                          "extra_techniques[].gloss"),
            evidence=True,
            basis_kind="text",
        ),
        # techpipe (D602, D604): the staged technique annotation on taxonomy v0.9 (modes single, sweep,
        # staged, lexical; workflows/annotate/technique_staged/). TECHNIQUE_ROLES' rows plus the mode, the
        # candidate gaps, the unresolved attestations and the pre-pass record; no separate record.
        PayloadSpec(
            workflow="technique_staged",
            version=1,
            payload_schema=TECHNIQUE_STAGED,
            grain="document",
            text_bearing=("techniques[].quotes[].text", "techniques[].note", "extra_techniques[].quotes[].text",
                          "extra_techniques[].gloss", "candidate_gaps[].name", "candidate_gaps[].gloss",
                          "candidate_gaps[].quotes[].text", "unresolved[].surface", "unresolved[].gloss",
                          "unresolved[].snippet", "prepass.mentions[].surface", "prepass.mentions[].gloss",
                          "prepass.doc_level[].why"),
            evidence=True,
            basis_kind="text",
        ),
        # D736: the organisations of the affiliation payload, resolved by stack.orgvocab's deterministic
        # matcher over the pinned organisation vocabulary (no LLM; origin computed). Chained on ``affiliation``: a
        # changed affiliation result, or a vocabulary edit (its sha256 is the pin's template digest), recomputes
        # it; nothing is asked. The dataset release ships it as the kind affiliation_orgs and copies its
        # per-affiliation answer into affiliation (mapping.yaml org_fields).
        PayloadSpec(
            workflow="affiliation_orgs",
            version=1,
            payload_schema=AFFILIATION_ORGS,
            grain="document",
            text_bearing=(),
            evidence=True,
            basis_kind="chained",
            reads=("affiliation",),
        ),
        # D755: English machine translations of a non-English document's title and abstract, beside the originals
        # (one LLM call per document; basis the metadata fields, so an edited title or abstract re-asks). Text-bearing:
        # the translations are derived from third-party text, so a release cuts and withholds them as it does the
        # abstract.
        PayloadSpec(
            workflow="translation",
            version=1,
            payload_schema=TRANSLATION,
            grain="document",
            text_bearing=("title_en", "abstract_en"),
            evidence=True,
            basis_kind="fields",
        ),
    )
}


def _check_records() -> None:
    for spec in REGISTRY.values():
        if not spec.is_record:
            continue
        prod = REGISTRY.get(spec.produced_by)
        if prod is None or prod.is_record:
            raise ValueError(f"{spec.workflow}: produced_by {spec.produced_by!r} is not a registered producer")
        if (prod.basis_kind, prod.evidence) != (spec.basis_kind, spec.evidence):
            raise ValueError(f"{spec.workflow}: basis_kind/evidence differ from its producer {prod.workflow}'s")
        if spec.workflow in [f.name for f in prod.payload_schema]:
            raise ValueError(f"{spec.workflow}: the name is a payload field of {prod.workflow}")


def _check_reads() -> None:
    order = list(REGISTRY)
    for i, (w, spec) in enumerate(REGISTRY.items()):
        late = [r for r in spec.reads if r not in order[:i]]
        if late:
            raise ValueError(f"{w}: reads {late} are not earlier registry entries (registry order is "
                             f"the reads' topological order; earlier: {order[:i]})")


_check_records()
_check_reads()


def records_of(producer: str) -> tuple[str, ...]:
    """The records ``producer``'s stage may write (registry order; the producer itself excluded)."""
    return tuple(w for w, s in REGISTRY.items() if s.is_record and s.produced_by == producer)


def joined_reads(workflow: str, line_reads: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """The annotations ``annotate.<workflow>`` joins into its corpus rows (D235): the registry
    ``reads`` of ``workflow`` (when registered), then the stage line's ``reads:`` (scope only), in
    that order without repeats."""
    spec = REGISTRY.get(workflow)
    return tuple(dict.fromkeys([*(spec.reads if spec is not None else ()), *line_reads]))


def payload(workflow: str) -> PayloadSpec:
    """The registry entry for ``workflow``; ``KeyError`` names the registered set."""
    try:
        return REGISTRY[workflow]
    except KeyError:
        raise KeyError(f"unregistered workflow {workflow!r}; registered: {list(REGISTRY)}") from None
