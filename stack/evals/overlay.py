"""The eval-only egress overlay (D173 (E)): ``evals/egress-overlay.yaml`` applied to an in-memory
copy of the loaded ``llm.yaml`` config; ``configs/llm.yaml`` is never written.

Format (two keys, both optional)::

    provider_prefixes: {deepseek: deepinfra}             # model-id prefix -> egress provider slug
    add_providers: {open: [deepinfra], public_web: [deepinfra]}   # licence class -> extra slugs

A prefix the production config already maps to a *different* slug, or a licence class the
production config has no rule for, is refused (the overlay only adds; it never redirects a
production route). The client's own egress gate (``LlmClient._gate_ok``) then decides, so an
item whose licence class the overlaid rule refuses for a model is never sent to it.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from stack import yamlio

if TYPE_CHECKING:
    from stack.config import LlmConfig, StackConfig

#: The repository's overlay whatever the cwd (``stack/evals/overlay.py`` → ``../../evals``).
DEFAULT_OVERLAY = Path(__file__).resolve().parents[2] / "evals" / "egress-overlay.yaml"
KEYS = frozenset({"provider_prefixes", "add_providers"})


class OverlayError(ValueError):
    """The overlay file is malformed or would change a production route."""


def load_overlay(path: Path) -> dict[str, Any]:
    """The overlay mapping at ``path`` (an empty overlay when the file is empty)."""
    try:
        data = yamlio.load(path.read_text("utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise OverlayError(f"egress overlay {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OverlayError(f"egress overlay {path}: expected a mapping, got {type(data).__name__}")
    extra = sorted(set(data) - KEYS)
    if extra:
        raise OverlayError(f"egress overlay {path}: keys {extra} not in {sorted(KEYS)}")
    return data


def overlay_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_overlay(llm: LlmConfig, overlay: dict[str, Any]) -> LlmConfig:
    """A copy of ``llm`` with the overlay's prefixes and per-class providers added."""
    egress = llm.egress.model_copy(deep=True)
    for prefix, slug in (overlay.get("provider_prefixes") or {}).items():
        have = egress.provider_prefixes.get(prefix)
        if have is not None and have != slug:
            raise OverlayError(f"egress overlay maps prefix {prefix!r} to {slug!r}, but llm.yaml "
                               f"already maps it to {have!r}; an overlay only adds routes")
        egress.provider_prefixes[prefix] = str(slug)
    for cls_, slugs in (overlay.get("add_providers") or {}).items():
        rule = egress.by_license_class.get(cls_)
        if rule is None:
            raise OverlayError(f"egress overlay adds providers to licence class {cls_!r}, which "
                               f"llm.yaml has no rule for (known: {sorted(egress.by_license_class)})")
        for s in slugs or []:
            if s not in rule.providers:
                rule.providers.append(str(s))
    return llm.model_copy(update={"egress": egress})


def overlaid_config(config: StackConfig, overlay: dict[str, Any]) -> StackConfig:
    """``config`` with :func:`apply_overlay` applied to its ``llm`` (a new frozen object)."""
    return dataclasses.replace(config, llm=apply_overlay(config.llm, overlay))


__all__ = ["DEFAULT_OVERLAY", "OverlayError", "apply_overlay", "load_overlay", "overlaid_config",
           "overlay_digest"]
