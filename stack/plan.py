"""The plan file (§3.5 as amended by D1, D16, D22, D31): one YAML per plan under
``configs/plans/``, loaded and validated here (pydantic, ``extra="forbid"`` everywhere).

```yaml
plan: dev-v2
schema_version: 2
series: dev-v2                     # default series of every line's in/out (configs/plans/README.md)
prompts: {…}                       # data only (M4 reads it)
gate: {<consumer>: [{sql: …} | {question: …, prompt: …, fetch_on: […]}], no_basis_fetches: true}
acquire: {convert: {config: …, expect_pin: …}, wants: {<channel>: [<content_role>…]}, recheck: {default: weekly}}
versions: {compaction: {max_partials: 12, max_partial_rows_ratio: 1.0}}   # a per-stage kind is refused (D378)
fold: {corpus: {filter: …}, build: {filter: …, annotations_folded: […]}, release: {config: …}, diagnostic: false}
recovery: {dead_run_charge: false}
stages: [{stage: …, prior: […], in: […], out: {paths: […], series: …, kind: full|part, comment: …},
          budget: {max_usd, max_minutes}, repeat: {until: scope_complete, max_runs},
          filter: …, reads: […], keys: […], miss_cap: n, require_complete: false,
          options: {…}}, …]
require_complete: [<family>…]
```

Refused: ``input_rule`` and ``compact_after`` (dropped by D22), ``recovery.dead_run_charge: true``
(D1: not implemented in v4 M1-M5). The gate is parsed and validated as data only (one of ``sql:``,
``question:`` or ``rule:`` per clause; a ``rule:`` names a triage source rule of
``stack/rules.py``, refused when unknown or of another stage); evaluating it is ``stack/gate.py``
(T2.9, M4). ``options`` on a stage
line is free-form stage configuration (collector windows and limits; proposed in T1.3) and is
part of that stage's ``config_digest``.

**Annotate reads** (D235). An ``annotate.<wf>`` line joins the annotations
:func:`stack.schemas.payloads.registry.joined_reads` names (the registry ``reads`` of ``<wf>`` plus
the line's ``reads:``, which only scope reads). Each must be registered and its producer must have
an ``annotate.<producer>`` line; the loader then orders ``stages`` stably so every producer line
precedes its readers (Kahn, smallest listed index first: listed order kept where no read forces a
move) and refuses a cycle. ``fold.build.annotations_folded`` naming a ``chained`` workflow must fold
its registry reads too. A line without ``reads:`` dumps without the key, so plans that use none
digest as before.

**A line not required complete** (round 5, R6 item 1). ``require_complete: false`` on a stage line
takes that line out of the plan's ``require_complete`` families: a run of it that ends ``ok`` with
``scope_complete = false`` does not make the build ``incomplete`` (``stack.build``, step 4: the
build record lists it under ``incomplete_allowed`` and warns). For enrichment that retries next
build (``collect.s2-lookup`` without an S2 key, std-v3). A failed run still fails the build. The
field is omitted from dumps at its default (``true``), so plans without it digest as before, and
it is not part of the line's ``digest()`` (the runs' ``config_digest``): it changes what the build
makes of a run, not what the stage does.

**Accepted configs** (D758). An ``annotate.<wf>`` line of a workflow that keeps evidence, and the ``triage`` line
per gate question, may name its target config and the earlier configs whose decided answers count for it::

    - stage: annotate.technique_staged
      target: 2c2dbd49…                     # the full config digest the line's pin gives today (checked)
      accepted:                             # preference order
        - {config: afc8e6f6…, name: technique-staged-v5-a1.1@techniques-1.0.0, decision: D758, note: "…"}
    - stage: triage
      target: {safety_relevance: 8842bf13…}  # per question: its question digest
      accepted: [{question: safety_relevance, config: …, name: safety-relevance-v3, decision: …}]

``target`` is required with ``accepted`` and refused without it; the stage (and the build at its start) refuses to
run when the config its pin gives is not ``target``, so a later pin change cannot inherit the list unseen. The
load refuses: ``accepted`` on another stage, malformed digests, ``target`` in ``accepted``, a config twice, an
entry's ``question`` on an annotate line or missing on the triage line, a triage question the gate does not ask or
that has no ``target``. ``name`` is checked at stage start against the stored pin of that config
(:func:`stack.annotate.stage.config_name`). Both keys are part of the plan (its digest), never of the line's
:meth:`StageLine.digest` (so of no config digest), and are omitted from dumps when unset, so plans without them
digest as before. Semantics: :mod:`stack.annotate.select`, :mod:`stack.gate` "Accepted answers".

Digests (D19): ``plan_digest`` = sha256 of the canonical JSON (JCS) of the validated plan
(``stages`` in the resolved order);
``gate_digest`` = the same over ``plan.gate`` in its file shape (consumers plus
``no_basis_fetches``; a ``rule:`` clause with its compiled ``rule_sql``, so both digests change
when a rule's lists in ``stack/rules.py`` change).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from stack import rules, yamlio
from stack.core.hashes import jcs
from stack.fold.configs import FoldPlan
from stack.schemas.payloads.registry import REGISTRY, joined_reads
from stack.schemas.vocab import CONTENT_ROLES, RECORD_ROLES
from stack.store.layout import COMMENT_RE, PLAN_RE, STAGE_RE, check_series, check_tail


class PlanError(Exception):
    """A plan file that does not load or validate; the message names the file."""


#: Keys the design's §3.5 example still shows but D22 dropped.
DROPPED_KEYS: dict[str, str] = {
    "input_rule": "dropped by D22 (the head pin wins)",
    "compact_after": "dropped by D22 (versions.compaction.max_partials stays)",
}
_DURATION_RE = re.compile(r"(?P<n>\d+(?:\.\d+)?)(?P<unit>[smhd])")
_NAMED_PERIODS: dict[str, dt.timedelta] = {
    "daily": dt.timedelta(days=1),
    "weekly": dt.timedelta(days=7),
    "monthly": dt.timedelta(days=30),
}
_STAGE_RE = STAGE_RE  # one definition with the job-name grammar (stack/store/layout.py, D320)


def parse_duration(text: str) -> dt.timedelta:
    """``90s``, ``10m``, ``2h``, ``7d`` (one unit), or ``daily|weekly|monthly``."""
    s = str(text).strip()
    if s in _NAMED_PERIODS:
        return _NAMED_PERIODS[s]
    m = _DURATION_RE.fullmatch(s)
    if m is None:
        raise ValueError(f"duration {text!r}: expected <n>[smhd] or one of {sorted(_NAMED_PERIODS)}")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m["unit"]]
    return dt.timedelta(**{unit: float(m["n"])})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- gate (data only until T2.9)
#: The stage whose source rules (``stack/rules.py``) a gate's ``rule:`` clauses name.
GATE_RULE_STAGE = "triage"
#: Validation context of a gate read back from the store (``stack.gate.load_gate``): its
#: ``rule_sql`` is the SQL the run evaluated and is kept as stored, whatever ``stack/rules.py``
#: now says (X14 recomputes the decisions of that run).
STORED_GATE = "stored_gate"


class GateClause(_Model):
    """One clause of a consumer's gate: exactly one of ``sql``, ``question`` or ``rule``.

    ``rule`` names a triage source rule (``stack/rules.py``); ``rule_sql`` is derived at load, the
    rule's compiled predicate (:meth:`stack.rules.SourceRule.sql`), and is part of the clause's
    file shape so every gate digest covers it. A plan file need not write it; one that does must
    write the current compile. An unknown rule or a rule of another stage is refused."""

    sql: str | None = None
    question: str | None = None
    prompt: str | None = None
    fetch_on: list[str] | None = None
    rule: str | None = None
    rule_sql: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _compile_rule(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict) or data.get("rule") is None:
            return data
        stored = bool((info.context or {}).get(STORED_GATE))
        if stored and data.get("rule_sql"):
            return data
        try:
            compiled = rules.rule(str(data["rule"]), GATE_RULE_STAGE).sql()
        except rules.RuleError as exc:
            raise ValueError(str(exc)) from exc
        given = data.get("rule_sql")
        if given is not None and given != compiled:
            raise ValueError(f"rule {data['rule']!r}: `rule_sql` is derived from stack/rules.py and differs "
                             "from the current compile; drop it from the plan")
        return {**data, "rule_sql": compiled}

    @model_validator(mode="after")
    def _one_kind(self) -> GateClause:
        if sum(x is not None for x in (self.sql, self.question, self.rule)) != 1:
            raise ValueError("a gate clause has exactly one of `sql:`, `question:` or `rule:`")
        if self.question is None and (self.prompt is not None or self.fetch_on is not None):
            raise ValueError("a `sql:`/`rule:` clause takes no `prompt`/`fetch_on`")
        if self.question is not None and (self.prompt is None or not self.fetch_on):
            raise ValueError(f"question {self.question!r} needs `prompt` and a non-empty `fetch_on`")
        if self.sql is not None and not self.sql.strip():
            raise ValueError("empty `sql:` clause")
        if (self.rule_sql is not None) != (self.rule is not None):
            raise ValueError("`rule_sql` belongs to a `rule:` clause")
        return self

    @property
    def predicate(self) -> str | None:
        """The SQL this clause evaluates: ``sql``, or a rule's ``rule_sql``; ``None`` for a question."""
        return self.sql if self.rule is None else self.rule_sql


class Gate(_Model):
    """``gate:``: per consumer a list of clauses, plus ``no_basis_fetches`` (D22)."""

    consumers: dict[str, list[GateClause]] = Field(default_factory=dict)
    no_basis_fetches: bool = True

    @model_validator(mode="before")
    @classmethod
    def _split(cls, data: Any) -> Any:
        if isinstance(data, dict) and "consumers" not in data:
            data = dict(data)
            nbf = data.pop("no_basis_fetches", True)
            return {"consumers": data, "no_basis_fetches": nbf}
        return data

    def file_shape(self) -> dict[str, Any]:
        """The gate as the plan file writes it, each ``rule:`` clause with its compiled
        ``rule_sql`` (``gate_digest``'s referent; a gate without rules is exactly its file)."""
        out: dict[str, Any] = {c: [cl.model_dump(mode="json", exclude_none=True) for cl in cls_]
                               for c, cls_ in self.consumers.items()}
        out["no_basis_fetches"] = self.no_basis_fetches
        return out

    @property
    def is_sql_only(self) -> bool:
        """No ``question:`` clause (``sql:`` and ``rule:`` clauses only)."""
        return all(cl.question is None for cls_ in self.consumers.values() for cl in cls_)


# ---------------------------------------------------------------- acquire, versions, recovery
# The `fold:` block is `stack.fold.configs.FoldPlan`, the one model of it (D66).
class ConvertConfig(_Model):
    """``acquire.convert``: the convert config's name (``workflows/acquire/convert.py`` ``CONFIGS``)
    and, optionally, ``expect_pin``, a hex prefix (≥ 8 chars) the computed convert pin must start
    with or acquire refuses to run (R4-1: a package upgrade must not change the pin silently). A
    guard, not configuration: excluded from every dump, so it is in no digest (``plan_digest``
    included) and a plan without it digests as before."""

    config: str
    expect_pin: str | None = Field(None, exclude=True)

    @field_validator("expect_pin")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[0-9a-f]{8,64}", v):
            raise ValueError(f"acquire.convert.expect_pin {v!r}: 8-64 lowercase hex characters (a pin prefix)")
        return v


class Recheck(_Model):
    """``acquire``'s refresh intervals of ``ok`` roles (D22): ``default`` for the roles that are
    rechecked at all (channel group ``url``, a lane with ``recheck_ok``); ``hosts`` (D701) overrides
    it per host for an ``ok`` episode whose fetch started on that host (the episode's fetch group:
    the URL's host for the http lane; ``arxiv.org`` for the arXiv lane's episodes, whose history
    stamps its own checks at ``default``), a key matching the host or any subdomain of it, the
    longest key winning (``openai.com`` covers ``www.openai.com``); ``article_in_press`` for a role
    whose current text is a publisher's Article in Press (round 4, P: refetched until the version of
    record replaces it; ``null`` = never; :mod:`workflows.acquire.quality`). None of these is in a
    line digest, a convert pin or a work key: changing them moves the plan digest only, re-fetches
    nothing by itself and re-dates a role's next check at its next ``ok`` episode."""

    default: str = "weekly"
    hosts: dict[str, str] = Field(default_factory=dict)
    article_in_press: str | None = "14d"

    @field_validator("default", "article_in_press")
    @classmethod
    def _dur(cls, v: str | None) -> str | None:
        if v is not None:
            parse_duration(v)
        return v

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, v: dict[str, str]) -> dict[str, str]:
        for host, period in v.items():
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", host):
                raise ValueError(f"acquire.recheck.hosts: {host!r} is not a lowercase host name")
            parse_duration(period)
        return v

    def for_host(self, host: str | None) -> dt.timedelta:
        """The recheck period of an ``ok`` episode that started on ``host`` (``hosts``, else
        ``default``)."""
        h = (host or "").lower()
        hits = [k for k in self.hosts if h == k or h.endswith("." + k)]
        return parse_duration(self.hosts[max(hits, key=len)] if hits else self.default)


class AcquireConfig(_Model):
    convert: ConvertConfig | None = None
    wants: dict[str, list[str]] = Field(default_factory=dict)  # channel -> content roles (D11)
    recheck: Recheck = Field(default_factory=Recheck)

    @field_validator("wants")
    @classmethod
    def _roles(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for ch, roles in v.items():
            records = [r for r in roles if r in RECORD_ROLES]
            if records:  # D522: a provider's record is in-band metadata, never fetched content
                raise ValueError(f"acquire.wants.{ch}: {records} is a record role, not content "
                                 f"(wants takes {CONTENT_ROLES})")
            bad = [r for r in roles if r not in CONTENT_ROLES]
            if bad:
                raise ValueError(f"acquire.wants.{ch}: unknown content roles {bad} (not in {CONTENT_ROLES})")
        return v


class Compaction(_Model):
    max_partials: int = Field(12, ge=1)
    max_partial_rows_ratio: float = Field(1.0, gt=0)


class Versions(_Model):
    """``compaction`` (§7.8). The per-stage version kinds of D3 moved to the line's
    ``out: {kind: part}`` (D378; ``configs/plans/README.md``); a ``versions: {<stage>: full|part}`` entry is refused."""

    compaction: Compaction = Field(default_factory=lambda: Compaction.model_validate({}))

    @model_validator(mode="before")
    @classmethod
    def _no_kinds(cls, data: Any) -> Any:
        if isinstance(data, dict):
            stale = sorted(k for k in data if k != "compaction")
            if stale:
                raise ValueError(f"versions: per-stage kinds {stale} are gone; a line says `out: {{kind: part}}` "
                                 "(D378, configs/plans/README.md)")
        return data


class Recovery(_Model):
    """D1, D16."""

    dead_run_charge: bool = False

    @field_validator("dead_run_charge")
    @classmethod
    def _no_charge(cls, v: bool) -> bool:
        if v:
            raise ValueError("recovery.dead_run_charge: true is not implemented in v4 M1-M5 (D1)")
        return v


# ---------------------------------------------------------------- stage lines
class Budget(_Model):
    max_usd: float | None = Field(None, gt=0)
    max_minutes: float | None = Field(None, gt=0)


class Repeat(_Model):
    until: Literal["scope_complete"] = "scope_complete"
    max_runs: int = Field(4, ge=1)


class OutLine(_Model):
    """A stage line's ``out:`` (``configs/plans/README.md``; ``docs/OPERATE.md``, "Data files: series
    and chains"): the run's ``--out`` paths (``data/<family>/[<series>/]``, one per family it
    overrides), ``--series`` (default: the plan's ``series:``), ``--full|--part`` (default full),
    ``--comment`` and ``--restart``."""

    paths: list[str] = Field(default_factory=list)
    series: str | None = None
    kind: Literal["full", "part"] | None = None
    comment: str | None = None
    restart: bool = False

    @field_validator("series")
    @classmethod
    def _series(cls, v: str | None) -> str | None:
        return None if v is None else check_series(v)

    @field_validator("comment")
    @classmethod
    def _comment(cls, v: str | None) -> str | None:
        if v is not None and not COMMENT_RE.fullmatch(v):
            raise ValueError(f"out.comment {v!r} must match {COMMENT_RE.pattern}")
        return v


DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class AcceptedConfig(_Model):
    """One entry of a line's ``accepted:`` (module docstring, Accepted configs)."""

    config: str = Field(pattern=DIGEST_PATTERN)
    name: str = Field(min_length=1)
    decision: str = Field(pattern=r"^D[0-9]+$")
    note: str | None = None
    question: str | None = None


def selection_digest(target: str | Mapping[str, str], accepted: Sequence[Mapping[str, Any]]) -> str:
    """A stage's selection digest (D758): sha256 of the JCS of ``{target, accepted}`` (the target config digest,
    per question for triage, and the ordered accepted entries with name, decision and note). Not a config digest:
    the config digest stays the identity of what produces an answer; this says which set of answers the stage
    keeps (in the run record's ``selection`` and ``release.json`` ``stages``)."""
    return hashlib.sha256(jcs({"target": dict(target) if isinstance(target, Mapping) else target,
                               "accepted": [dict(a) for a in accepted]}).encode()).hexdigest()


class StageLine(_Model):
    """One entry of ``stages:``. ``bump_on`` (v3's granular refusal list, ``name`` or
    ``name:major``; D42 moved it here from ``workflows.yaml``) is read by nothing, so a non-empty
    one is refused (D437); the empty default stays in :meth:`digest` until the next plan-digest
    bump removes the field. ``reads`` (``annotate.<wf>``
    only, D235): annotations joined for the line's ``filter`` (scope reads, not in the basis);
    omitted from dumps when empty. ``require_complete: false`` (R6 item 1, module docstring): the
    line's shortfall does not make the build ``incomplete``; omitted from dumps when ``true`` and
    left out of :meth:`digest`."""

    stage: str
    prior: list[str] = Field(default_factory=list)
    #: ``in:`` (``configs/plans/README.md``): store paths replacing the build's default inputs of their families
    in_: list[str] = Field(default_factory=list, alias="in")
    out: OutLine | None = None
    budget: Budget | None = None
    repeat: Repeat | None = None
    filter: str | None = None
    reads: list[str] = Field(default_factory=list)
    keys: list[str] | None = None
    miss_cap: int | None = Field(None, ge=1)
    bump_on: list[str] = Field(default_factory=list)
    require_complete: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    #: the config the line's pin gives (``triage``: per question), and the earlier configs whose decided answers
    #: count for it (module docstring, Accepted configs); in no :meth:`digest`
    target: str | dict[str, str] | None = None
    accepted: list[AcceptedConfig] = Field(default_factory=list)

    @field_validator("stage")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _STAGE_RE.fullmatch(check_tail(v, what="stage")):
            raise ValueError(f"stage {v!r} is not `<phase>` or `<phase>.<name>`")
        return v

    @model_serializer(mode="wrap")
    def _drop_empty_reads(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        out: dict[str, Any] = handler(self)
        if not self.reads:
            out.pop("reads", None)
        if self.require_complete:
            out.pop("require_complete", None)
        if not self.in_:
            out.pop("in_", None)
            out.pop("in", None)
        if self.out is None:
            out.pop("out", None)
        if self.target is None:
            out.pop("target", None)
        if not self.accepted:
            out.pop("accepted", None)
        return out

    @model_validator(mode="after")
    def _accepted_shape(self) -> StageLine:
        """The load-time refusals of the module docstring (Accepted configs) that need only the line."""
        if self.target is None and not self.accepted:
            return self
        where = f"stage {self.stage}"
        if not self.accepted:
            raise ValueError(f"{where}: `target` names the config `accepted` applies to; it needs an `accepted` list")
        if self.target is None:
            raise ValueError(f"{where}: `accepted` needs `target` (the config digest the line's pin gives)")
        if self.stage == "triage":
            if not isinstance(self.target, dict):
                raise ValueError(f"{where}: `target` is a mapping question -> question digest on the triage line")
            targets: dict[str | None, str] = {k: v for k, v in self.target.items()}
        else:
            wf = self.stage.removeprefix("annotate.")
            spec = REGISTRY.get(wf)
            if not self.stage.startswith("annotate.") or spec is None or wf == "triage" or spec.is_record \
                    or not spec.evidence:
                raise ValueError(f"{where}: `accepted` is for triage and annotate.<wf> lines of a workflow that keeps "
                                 "evidence")
            if not isinstance(self.target, str):
                raise ValueError(f"{where}: `target` is one config digest on an annotate line")
            targets = {None: self.target}
        for d in targets.values():
            if not re.fullmatch(DIGEST_PATTERN, d):
                raise ValueError(f"{where}: target {d!r} is not a full config digest (64 lowercase hex)")
        seen: set[tuple[str | None, str]] = set()
        for e in self.accepted:
            if (e.question is None) != (self.stage != "triage"):
                raise ValueError(f"{where}: accepted {e.config[:12]}…: `question` names a triage question, on the "
                                 "triage line only")
            if e.question not in targets:
                raise ValueError(f"{where}: accepted {e.config[:12]}…: question {e.question!r} has no `target`")
            if e.config == targets[e.question]:
                raise ValueError(f"{where}: accepted {e.config[:12]}… is the target itself")
            if (e.question, e.config) in seen:
                raise ValueError(f"{where}: accepted {e.config[:12]}… listed twice")
            seen.add((e.question, e.config))
        bare = [q for q in targets if all(e.question != q for e in self.accepted)]
        if bare:
            raise ValueError(f"{where}: target of {bare} with no accepted config")
        return self

    def accepted_configs(self, question: str | None = None) -> tuple[str, ...]:
        """The configs of ``accepted`` (for ``question`` on the triage line), in preference order."""
        return tuple(e.config for e in self.accepted if e.question == question)

    def selection(self, target: str | Mapping[str, str]) -> dict[str, Any]:
        """The run record's ``selection`` of this line (D758): the config the run produced answers under
        (``target``: the computed config digest; triage: per question) and the ``accepted`` entries, with their
        ``digest`` = :func:`selection_digest` (which set of answers the stage ships, on its own)."""
        tgt: Any = dict(target) if isinstance(target, Mapping) else target
        acc = [e.model_dump(mode="json", exclude_none=True) for e in self.accepted]
        return {"digest": selection_digest(tgt, acc), "target": tgt, "accepted": acc}

    def target_of(self, question: str | None = None) -> str | None:
        """``target`` (of ``question`` on the triage line), or ``None``."""
        if isinstance(self.target, dict):
            return self.target.get(question or "")
        return self.target if question is None else None

    @model_validator(mode="after")
    def _bump_on_refused(self) -> StageLine:
        if self.bump_on:
            raise ValueError(f"stage {self.stage}: `bump_on: {self.bump_on}` is read by nothing (no config stamp "
                             "is compared, D437); remove the key")
        return self

    @model_validator(mode="after")
    def _reads_on_annotate(self) -> StageLine:
        if self.reads and not self.stage.startswith("annotate."):
            raise ValueError(f"stage {self.stage}: `reads:` is for annotate.<wf> lines only")
        if len(set(self.reads)) != len(self.reads):
            raise ValueError(f"stage {self.stage}: reads repeats a name: {self.reads}")
        return self

    def digest(self) -> str:
        """sha256 of the line's canonical JSON (the default ``config_digest`` of its runs), without
        ``require_complete``, ``in`` and ``out`` (build policy and destinations, not stage
        configuration; ``configs/plans/README.md``) and ``target`` / ``accepted`` (which earlier answers count,
        not what the stage asks; module docstring)."""
        data = self.model_dump(mode="json")
        for k in ("require_complete", "in_", "in", "out", "target", "accepted"):
            data.pop(k, None)
        return hashlib.sha256(jcs(data).encode()).hexdigest()


class Plan(_Model):
    plan: str
    schema_version: Literal[2]
    prompts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    gate: Gate = Field(default_factory=Gate)
    acquire: AcquireConfig = Field(default_factory=AcquireConfig)
    versions: Versions = Field(default_factory=Versions)
    fold: FoldPlan = Field(default_factory=FoldPlan)
    recovery: Recovery = Field(default_factory=Recovery)
    stages: list[StageLine] = Field(default_factory=list)
    require_complete: list[str] = Field(default_factory=list)
    #: The default series of every line's ``in``/``out`` (``configs/plans/README.md``; ``docs/OPERATE.md``, "Data
    #: files: series and chains"); ``None``: the build refuses
    #: a line that needs one
    series: str | None = None

    @field_validator("series")
    @classmethod
    def _plan_series(cls, v: str | None) -> str | None:
        return None if v is None else check_series(v)

    @model_validator(mode="before")
    @classmethod
    def _refuse_dropped(cls, data: Any) -> Any:
        if isinstance(data, dict):
            hits = [f"{k} ({why})" for k, why in DROPPED_KEYS.items() if k in data]
            vers = data.get("versions")
            if isinstance(vers, dict) and "compact_after" in vers:
                hits.append(f"versions.compact_after ({DROPPED_KEYS['compact_after']})")
            if hits:
                raise ValueError(f"refused plan keys: {', '.join(hits)}")
        return data

    @field_validator("plan")
    @classmethod
    def _plan_name(cls, v: str) -> str:
        """A plan name names build directories (``builds/<head>.<plan>/``, D320)."""
        if not PLAN_RE.fullmatch(check_tail(v, what="plan")):
            raise ValueError(f"plan name {v!r} must match {PLAN_RE.pattern}")
        return v

    @model_validator(mode="after")
    def _unique_stages(self) -> Plan:
        names = [s.stage for s in self.stages]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"stages listed twice: {dup} (use `repeat:` for repeated runs)")
        return self

    @model_validator(mode="after")
    def _order_reads(self) -> Plan:
        """D235 (module docstring): check each ``annotate.<wf>`` line's joined reads, order the
        lines producer before reader, check the folded ``chained`` workflows' reads."""
        index = {s.stage: i for i, s in enumerate(self.stages)}
        edges: dict[int, set[int]] = {i: set() for i in range(len(self.stages))}  # reader -> producers
        for i, line in enumerate(self.stages):
            if not line.stage.startswith("annotate."):
                continue
            wf = line.stage.removeprefix("annotate.")
            own = REGISTRY.get(wf)
            if own is not None and own.is_record:
                raise ValueError(f"{line.stage}: {wf!r} is a record of annotate.{own.produced_by} (written by "
                                 f"that line under its pin), not a workflow with a stage")
            for name in joined_reads(wf, line.reads):
                spec = REGISTRY.get(name)
                if spec is None:
                    raise ValueError(f"{line.stage}: reads {name!r}, not a registered annotation "
                                     f"({list(REGISTRY)})")
                producer = f"annotate.{spec.produced_by}"
                if producer not in index:
                    raise ValueError(f"{line.stage}: reads {name!r}, but the plan has no {producer} line "
                                     f"to produce it")
                edges[i].add(index[producer])
        order: list[int] = []
        done: set[int] = set()
        while len(order) < len(self.stages):
            ready = [i for i in range(len(self.stages)) if i not in done and edges[i] <= done]
            if not ready:
                left = [i for i in range(len(self.stages)) if i not in done]
                raise ValueError(f"annotate reads form a cycle: {_cycle(left, edges, self.stages)}")
            order.append(ready[0])
            done.add(ready[0])
        if order != list(range(len(self.stages))):
            object.__setattr__(self, "stages", [self.stages[i] for i in order])
        folded = set(self.fold.build.annotations_folded)
        for w in self.fold.build.annotations_folded:
            reads = REGISTRY[w].reads if w in REGISTRY else ()
            lost = [r for r in reads if r not in folded]
            if lost:
                raise ValueError(f"fold.build.annotations_folded folds {w} (a chained basis over {list(reads)})"
                                 f" without its reads {lost}: every row would map stale")
        return self

    @model_validator(mode="after")
    def _accepted_questions(self) -> Plan:
        """A triage line's ``target`` names questions of the gate only (module docstring, Accepted configs)."""
        line = next((s for s in self.stages if s.stage == "triage"), None)
        if line is not None and isinstance(line.target, dict):
            asked = {cl.question for cls_ in self.gate.consumers.values() for cl in cls_ if cl.question}
            unknown = sorted(set(line.target) - asked)
            if unknown:
                raise ValueError(f"stage triage: target names questions {unknown} the gate does not ask")
        return self

    def stage_line(self, stage: str) -> StageLine | None:
        return next((s for s in self.stages if s.stage == stage), None)

    def to_json(self) -> dict[str, Any]:
        """The validated plan as JSON data, the gate in its file shape."""
        out = self.model_dump(mode="json", by_alias=True)
        if out.get("series") is None:
            out.pop("series", None)
        out["gate"] = self.gate.file_shape()
        return out

    @property
    def plan_digest(self) -> str:
        return hashlib.sha256(jcs(self.to_json()).encode()).hexdigest()

    @property
    def gate_digest(self) -> str:
        return hashlib.sha256(jcs(self.gate.file_shape()).encode()).hexdigest()


def _cycle(left: list[int], edges: dict[int, set[int]], stages: list[StageLine]) -> str:
    """One cycle among the unordered lines ``left``, as ``a -> b -> … -> a`` (producer to reader)."""
    cur, seen = left[0], []
    while cur not in seen:  # every unordered line has an unordered producer: walk them back
        seen.append(cur)
        cur = min(p for p in edges[cur] if p in left)
    loop = seen[seen.index(cur):][::-1]
    return " -> ".join(stages[i].stage for i in [*loop, loop[0]])


def load_plan(path: str | Path) -> Plan:
    """Load and validate a plan file; any failure is a :class:`PlanError` naming the file."""
    p = Path(path)
    try:
        data = yamlio.load(p.read_text("utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlanError(f"{p}: cannot read plan: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"{p}: a plan is a mapping, got {type(data).__name__}")
    try:
        return Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"{p}: invalid plan:\n{exc}") from exc


__all__ = [
    "DROPPED_KEYS",
    "GATE_RULE_STAGE",
    "STORED_GATE",
    "AcceptedConfig",
    "AcquireConfig",
    "Budget",
    "FoldPlan",
    "Gate",
    "GateClause",
    "Plan",
    "PlanError",
    "Recovery",
    "Repeat",
    "StageLine",
    "Versions",
    "load_plan",
    "selection_digest",
    "parse_duration",
]
