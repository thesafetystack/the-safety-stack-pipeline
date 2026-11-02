"""Named checks (Appendix B): findings, results and the check-name registry.

T1.1 shipped the types, the registry skeleton and S1-S7 (:mod:`stack.checks.schema`, D45); T1.4
completes the package: all 47 names of Appendix B are registered here from :data:`CHECK_TABLE`
(D33), and the modules that implement a check attach its function with :func:`register`:

* :mod:`stack.checks.schema` — S1-S10 (the parquet writer runs them and refuses the file);
* :mod:`stack.checks.family` — V1-V4 (per family file, cross-row allowed, D20);
* :mod:`stack.checks.chain` — V11, V12, X1, X10, X22, X23 (and :class:`StoreView`, the store as
  the store-scope checks see it);
* :mod:`stack.checks.blocks` — X4, X15, X17, X24;
* :mod:`stack.checks.verify` — :func:`run_checks`, the library entry point of ``stack verify``.

A check is a function returning a list of :class:`Finding`; no finding means the check passed.
``scope = "file"`` checks take ``(table, header, spec)`` for one family file; ``scope = "store"``
checks take a :class:`~stack.checks.chain.StoreView`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LEVELS: tuple[str, ...] = ("hard", "warn")
SCOPES: tuple[str, ...] = ("file", "store")
#: Items a :class:`CheckResult` shows (the verify table prints these).
MAX_ITEMS = 5

#: Appendix B in order: ``(code, name, level)``. 10 S + 12 V + 25 X = 47; X11 is the one warn.
CHECK_TABLE: tuple[tuple[str, str, str], ...] = (
    ("S1", "schema_equal", "hard"),
    ("S2", "nonnull", "hard"),
    ("S3", "key_unique", "hard"),
    ("S4", "status_pairing", "hard"),
    ("S5", "nested_unique", "hard"),
    ("S6", "id_formats", "hard"),
    ("S7", "kv_complete", "hard"),
    ("S8", "state_blocks", "hard"),
    ("S9", "digest_agreements", "hard"),
    ("S10", "payload_typed", "hard"),
    ("V1", "V-sources", "hard"),
    ("V2", "V-coverage", "hard"),
    ("V3", "V-state", "hard"),
    ("V4", "V-digests", "hard"),
    ("V5", "V-annotations", "hard"),
    ("V6", "V-content", "hard"),
    ("V7", "V-wf", "hard"),
    ("V8", "V-evidence", "hard"),
    ("V9", "V-fold", "hard"),
    ("V10", "V-release", "hard"),
    ("V11", "V-chain-linear", "hard"),
    ("V12", "V-compaction-equal", "hard"),
    ("X1", "finished_files", "hard"),
    ("X2", "lineage_equality", "hard"),
    ("X3", "expected_scope", "hard"),
    ("X4", "cardinalities", "hard"),
    ("X5", "blob_resolution", "hard"),
    ("X6", "policy_pins", "hard"),
    ("X7", "basis_currency", "hard"),
    ("X8", "selection_recompute", "hard"),
    ("X9", "fold_recompute", "hard"),
    ("X10", "ordering_pinned", "hard"),
    ("X11", "dead_runs_seen", "warn"),
    ("X12", "state_propagation", "hard"),
    ("X13", "gate_obeyed", "hard"),
    ("X14", "gate_union", "hard"),
    ("X15", "block_ownership", "hard"),
    ("X16", "lookup_gate_current", "hard"),
    ("X17", "created_preserved", "hard"),
    ("X18", "tombstone_closure", "hard"),
    ("X19", "state_monotone", "hard"),
    ("X20", "triage_current", "hard"),
    ("X21", "carry_regrouped", "hard"),
    ("X22", "full_covers_base", "hard"),
    ("X23", "head_descends", "hard"),
    ("X24", "attempts_monotone", "hard"),
    ("X25", "same_raw_reported", "hard"),
)


@dataclass(frozen=True)
class Finding:
    """One failure of a check: ``check`` is the Appendix B name (``schema_equal``), ``n`` the
    number of offending values or rows, ``items`` a few examples (paths, ids, values)."""

    check: str
    level: str
    message: str
    n: int = 1
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check over its input: ``status`` is ``ok``, ``failed``, ``warn`` (a
    warn-level check with findings) or ``skipped`` (``error_class`` then says why:
    ``not_implemented`` (D33), ``no_builds``, ``no_input``). ``code`` is the Appendix B code
    (``S1``); ``kind`` its letter (``S``, ``V``, ``X``)."""

    check: str
    level: str
    status: str
    findings: tuple[Finding, ...] = ()
    error_class: str | None = None
    code: str = ""

    @property
    def name(self) -> str:
        return self.check

    @property
    def kind(self) -> str:
        return self.code[:1]

    @property
    def n(self) -> int:
        return sum(f.n for f in self.findings)

    @property
    def all_items(self) -> list[str]:
        return [i for f in self.findings for i in (f.items or (f.message,))]

    @property
    def items(self) -> list[str]:
        """The first :data:`MAX_ITEMS` examples over all findings."""
        return self.all_items[:MAX_ITEMS]

    def as_record(self) -> dict[str, Any]:
        """The build record's ``verify.checks[]`` entry (A.7): ``{name, level, status, n, items}``
        (plus ``code`` and ``error_class`` when skipped)."""
        rec: dict[str, Any] = {"name": self.check, "level": self.level, "status": self.status,
                               "n": self.n, "items": self.all_items}
        if self.code:
            rec["code"] = self.code
        if self.error_class is not None:
            rec["error_class"] = self.error_class
        return rec


@dataclass(frozen=True)
class CheckSpec:
    """A registered check: Appendix B code (``S1``), name, level, scope, and its function once
    implemented (``None`` = registered, not implemented, D33)."""

    code: str
    name: str
    level: str = "hard"
    fn: Callable[..., list[Finding]] | None = field(default=None, compare=False)
    scope: str = "file"

    @property
    def implemented(self) -> bool:
        return self.fn is not None


#: Check name → spec; all 47 names from :data:`CHECK_TABLE`, ``fn`` filled by :func:`register`.
REGISTRY: dict[str, CheckSpec] = {name: CheckSpec(code, name, level)
                                  for code, name, level in CHECK_TABLE}
_CODES: dict[str, str] = {name: code for code, name, _ in CHECK_TABLE}


def register(code: str, name: str, level: str = "hard", *, scope: str = "file") -> Callable[
    [Callable[..., list[Finding]]], Callable[..., list[Finding]]
]:
    """Decorator: attach ``fn`` as the implementation of check ``code``/``name``. The name must be
    in :data:`CHECK_TABLE` with this code and level; implementing a name twice is an error."""
    if level not in LEVELS:
        raise ValueError(f"check {name}: level {level!r} not in {LEVELS}")
    if scope not in SCOPES:
        raise ValueError(f"check {name}: scope {scope!r} not in {SCOPES}")
    if _CODES.get(name) != code or REGISTRY[name].level != level:
        raise ValueError(f"check {code}/{name}/{level} is not a row of Appendix B (CHECK_TABLE)")

    def deco(fn: Callable[..., list[Finding]]) -> Callable[..., list[Finding]]:
        if REGISTRY[name].fn is not None:
            raise ValueError(f"check {name!r} implemented twice")
        REGISTRY[name] = CheckSpec(code, name, level, fn, scope)
        return fn

    return deco


def result(check: str, findings: list[Finding]) -> CheckResult:
    """Wrap a registered check's findings as a :class:`CheckResult`: ``failed`` when a hard
    check has a hard finding, ``warn`` when its findings are all warn-level (round 5, R1-1: X6's
    "the plan's pin moved since the run") or the check is a warn check."""
    spec = REGISTRY[check]
    hard = spec.level == "hard" and any(f.level == "hard" for f in findings)
    status = "ok" if not findings else ("failed" if hard else "warn")
    return CheckResult(check, spec.level, status, tuple(findings), code=spec.code)


def skipped(check: str, why: str) -> CheckResult:
    """A :class:`CheckResult` with ``status = skipped`` and ``error_class = why``."""
    spec = REGISTRY[check]
    return CheckResult(check, spec.level, "skipped", (), why, spec.code)
