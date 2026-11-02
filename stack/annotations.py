"""Reading annotation records off a ``documents``/``corpus`` row (A.4; track S, S1).

One screen call yields several **annotation records** (registry ``produced_by``): ``screen``
(the call-level summary), ``stack_relevance``, ``sr_relevance``, later ``text_usability``. Each is
an ``ANN`` in its own typed ``annotations.<record>`` field (or, for a version the reader's
registry does not type, an ``annotations.other[]`` entry). These helpers read them so consumers do
not hand-parse:

* :func:`annotation` — the record as a dict (ANN common fields + typed ``payload``), from the typed
  field or ``other[]``; with ``config_digest``, only a record of that config;
* :func:`pinned_digest` — the **plan-pinned** version of a record: its producer workflow's
  ``config_digest`` under the plan's pin (``prompts.annotate.<producer>``), the digest every record
  the pinned prompt writes carries. ``prompt_name`` and ``model`` in a payload are for humans; the
  config digest is what identifies a version (it covers the prompt name, template digests, models
  and the registry version);
* :func:`ann_expr` — a polars expression for a field of a record in an ``annotations`` column
  (polars is not a dependency of the pipeline; imported on call);
* the duckdb macros ``ann_ok(a, name)``, ``ann_verdict(a, name)``, ``ann_pinned(a, name, digest)``
  live in :data:`stack.query.MACROS` (``a`` = the ``annotations`` column, ``name`` a string
  literal).

Example (python)::

    digest = pinned_digest(load_plan(find_plan("dev-v2-m4")), "stack_relevance")
    rec = annotation(row, "stack_relevance", config_digest=digest)
    verdict = rec["payload"]["verdict"] if rec is not None and rec["status"] == "ok" else None
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from stack.schemas.blocks import ANN_COMMON
from stack.schemas.payloads.registry import payload as registry_entry

if TYPE_CHECKING:
    from stack.config import StackConfig
    from stack.plan import Plan

ANN_FIELDS: tuple[str, ...] = tuple(f.name for f in ANN_COMMON)


def _typed_payload(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """A parsed ``payload_json`` in the registry payload's shape (unknown keys dropped, missing
    ones NULL, values cast through the payload type: a value the type refuses raises)."""
    spec = registry_entry(name)
    if spec.grain == "item":
        (items_field,) = list(spec.payload_schema)
        item_names = [f.name for f in items_field.type.value_type]
        shaped: dict[str, Any] = {items_field.name: [{n: raw.get(n) for n in item_names}]}
    else:
        shaped = {f.name: raw.get(f.name) for f in spec.payload_schema}
    out: dict[str, Any] = pa.array([shaped], type=spec.payload_schema).to_pylist()[0]
    return out


def _from_other(name: str, entries: list[Mapping[str, Any]]) -> dict[str, Any]:
    """One typed record from the ``other[]`` entries of one ``(workflow, config_digest)`` (item
    grain: one entry per item, merged into ``payload.items``)."""
    spec = registry_entry(name)
    first = entries[0]
    out = {k: first.get(k) for k in ANN_FIELDS}
    payloads = [_typed_payload(name, json.loads(e["payload"]["payload_json"])) for e in entries]
    if spec.grain == "item":
        (items_field,) = list(spec.payload_schema)
        items = [it for p in payloads for it in p[items_field.name] if it.get("item_key")]
        out["payload"] = {items_field.name: sorted(items, key=lambda it: it["item_key"])}
    else:
        out["payload"] = payloads[0]
    return out


def annotation(row: Mapping[str, Any], name: str, *, config_digest: str | None = None) -> dict[str, Any] | None:
    """Record ``name`` of a ``documents``/``corpus`` row dict as ``{ANN common fields…, payload}``,
    or None when absent.

    Without ``config_digest``: the typed field ``annotations.<name>`` when non-NULL, else the
    newest (``decided_at``) ``other[]`` record with ``workflow == name``. With ``config_digest``:
    only a record of that config, looked up in the typed field and then in ``other[]``; None when
    no record matches. An ``other[]`` payload (``payload_json``) is parsed back into the registry
    payload's shape. ``name`` must be registered (``KeyError`` otherwise)."""
    registry_entry(name)
    anns = row.get("annotations") or {}
    typed = anns.get(name)
    if typed is not None and (config_digest is None or typed.get("config_digest") == config_digest):
        return dict(typed)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for o in anns.get("other") or []:
        if o.get("workflow") == name and (config_digest is None or o.get("config_digest") == config_digest):
            groups.setdefault(o.get("config_digest") or "", []).append(o)
    if not groups:
        return None
    newest = max(groups.values(), key=lambda es: (es[0].get("decided_at") is not None, es[0].get("decided_at")))
    return _from_other(name, newest)


def pinned_digest(plan: Plan | str | Path, name: str,
                  config: StackConfig | Callable[[], StackConfig] | None = None) -> str:
    """The plan-pinned version of record ``name``: the ``config_digest`` of its producer workflow
    (registry ``produced_by``) under ``plan``'s pin, as the stage computes it
    (:func:`stack.annotate.resolve_pin`, :func:`stack.annotate.config_digest`). ``plan`` is a
    :class:`~stack.plan.Plan`, a plan file or a plan name (``configs/plans/<name>.yaml``);
    ``config`` the stack config (default: the validated config, loaded once per process). Loads the
    producer's workflow module, no other I/O. A record the pinned prompt does not produce raises
    ``ValueError`` (no version of it is pinned)."""
    from stack.annotate.stage import config_digest, resolve_pin, stack_config
    from stack.plan import Plan as PlanModel
    from stack.stage_runner import load_stage_module

    if not isinstance(plan, PlanModel):
        from stack.build import find_plan
        from stack.plan import load_plan

        plan = load_plan(find_plan(plan))
    spec = registry_entry(name)
    producer = registry_entry(spec.produced_by)
    module = load_stage_module(f"annotate.{producer.workflow}")
    wf = module.WORKFLOW
    plan_pin = dict(plan.prompts.get(f"annotate.{producer.workflow}", {}))
    if spec.is_record and name not in wf.produced(plan_pin):
        raise ValueError(f"plan {plan.plan}: annotate.{producer.workflow} pin {plan_pin.get('name')!r} "
                         f"does not produce {name!r} ({list(wf.produced(plan_pin))})")
    cfg: Callable[[], StackConfig]
    if config is None:
        cfg = stack_config
    elif callable(config):
        cfg = config
    else:
        def cfg() -> StackConfig:
            return config
    pin = resolve_pin(wf, plan_pin, cfg)
    return config_digest(producer, plan_pin, pin)


def ann_expr(name: str, *fields: str, column: str = "annotations") -> Any:
    """A polars expression for ``<column>.<name>.<fields…>`` (struct fields), e.g.
    ``ann_expr("stack_relevance", "payload", "verdict")``; with no ``fields``, the record struct.
    ``name`` must be registered. Returns a ``polars.Expr``; polars is imported on call (not a
    pipeline dependency, so not typed here)."""
    import polars as pl  # type: ignore[import-not-found,unused-ignore]

    registry_entry(name)
    expr = pl.col(column).struct.field(name)
    for f in fields:
        expr = expr.struct.field(f)
    return expr


__all__ = ["ANN_FIELDS", "ann_expr", "annotation", "pinned_digest"]
