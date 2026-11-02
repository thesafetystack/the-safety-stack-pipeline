"""The four fold configs of §3.2's table as data, and the plan's ``fold:`` section (§3.5).

``FOLD_CONFIGS`` says, per config, what the fold reads, which steps run and what it writes;
:func:`stack.fold.run_fold` dispatches on it. ``FoldPlan`` is the plan file's ``fold:`` mapping
(``corpus.filter``, ``build.filter``, ``build.annotations_folded``, ``release.config``,
``diagnostic``), validated with ``extra="forbid"`` like the rest of the plan; the plan loader
(``stack/plan.py``) embeds it and the stage adapters read it.

All four are implemented (``corpus``, ``compact`` in M1; ``build``, ``release`` in M5, T5.2).
``release`` is not a stage: it writes ``releases/<tag>/`` (D21) through
:func:`stack.fold.release.run_release`, reached by ``stack fold release`` and ``stack release``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stack.query import resolve_filter
from stack.schemas.families import CHAIN_WORKFLOWS
from stack.schemas.vocab import FOLD_CONFIGS as FOLD_CONFIG_NAMES


@dataclass(frozen=True)
class FoldConfigSpec:
    """One row of §3.2's table.

    ``input_kind``: ``documents`` = a ``documents`` chain, unioned (§7.3); ``corpus`` = one
    ``corpus`` file read as is. ``default_filter``: a named filter, or ``None`` = no filter step.
    ``output``: ``chain_of_one`` = a ``corpus`` file with ``chain_id = file_id`` and base ``none``
    (D14); ``supersede_head`` = a ``documents`` full file whose base is the head it replaced
    (§7.4); ``release_dir`` = ``releases/<tag>/`` (D21)."""

    name: str
    input_kind: Literal["documents", "corpus"]
    default_filter: str | None
    joins: bool
    release: bool
    output_family: str
    output: Literal["chain_of_one", "supersede_head", "release_dir"]
    implemented: bool

    @property
    def stage(self) -> str:
        return f"fold.{self.name}"


FOLD_CONFIGS: dict[str, FoldConfigSpec] = {
    "corpus": FoldConfigSpec("corpus", "documents", "has_text", joins=False, release=False,
                             output_family="corpus", output="chain_of_one", implemented=True),
    "build": FoldConfigSpec("build", "corpus", "has_text", joins=True, release=False,
                            output_family="corpus", output="chain_of_one", implemented=True),
    "release": FoldConfigSpec("release", "corpus", None, joins=False, release=True,
                              output_family="release", output="release_dir", implemented=True),
    "compact": FoldConfigSpec("compact", "documents", None, joins=False, release=False,
                              output_family="documents", output="supersede_head", implemented=True),
}
if tuple(FOLD_CONFIGS) != FOLD_CONFIG_NAMES:
    raise RuntimeError(f"FOLD_CONFIGS {list(FOLD_CONFIGS)} must follow vocab.FOLD_CONFIGS {FOLD_CONFIG_NAMES}")


def fold_config(name: str) -> FoldConfigSpec:
    """The spec of fold config ``name`` (``corpus``, ``build``, ``release``, ``compact``)."""
    try:
        return FOLD_CONFIGS[name]
    except KeyError:
        raise KeyError(f"unknown fold config {name!r}; configs: {list(FOLD_CONFIGS)}") from None


# ---------------------------------------------------------------- the plan's fold: section
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_filter(v: str) -> str:
    resolve_filter(v)  # raises on an empty or non-string filter
    return v


class FoldCorpusPlan(_Model):
    """``fold.corpus``: the corpus fold's filter (a named filter or a SQL predicate)."""

    filter: str = "has_text"

    @field_validator("filter")
    @classmethod
    def _filter(cls, v: str) -> str:
        return _check_filter(v)


class FoldBuildPlan(_Model):
    """``fold.build``: the build filter and the ``<wf>`` blocks folded into ``annotations``."""

    filter: str = "has_text"
    annotations_folded: tuple[str, ...] = ()

    @field_validator("filter")
    @classmethod
    def _filter(cls, v: str) -> str:
        return _check_filter(v)

    @field_validator("annotations_folded")
    @classmethod
    def _workflows(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError(f"annotations_folded repeats a workflow: {list(v)}")
        bad = [w for w in v if not w or w in CHAIN_WORKFLOWS]
        if bad:
            raise ValueError(f"annotations_folded: {bad} are not <wf> workflows "
                             f"({sorted(CHAIN_WORKFLOWS)} live in the documents chain)")
        return v


class FoldReleasePlan(_Model):
    """``fold.release``: the release config path (``configs/releases/<name>.yaml``)."""

    config: str


class FoldPlan(_Model):
    """The plan's ``fold:`` mapping (§3.5). ``diagnostic: true`` adds ``files.corpus_everything``
    (a second ``fold.corpus`` with filter ``everything``)."""

    corpus: FoldCorpusPlan = Field(default_factory=FoldCorpusPlan)
    build: FoldBuildPlan = Field(default_factory=FoldBuildPlan)
    release: FoldReleasePlan | None = None
    diagnostic: bool = False

    def filter_for(self, config: str) -> str | None:
        """The plan's filter for ``config`` (``None`` where the config has no filter step)."""
        fold_config(config)
        return {"corpus": self.corpus.filter, "build": self.build.filter}.get(config)


def load_fold_plan(raw: Mapping[str, Any] | None) -> FoldPlan:
    """Validate a plan's ``fold:`` mapping (``None`` or absent = the defaults). Raises
    ``pydantic.ValidationError`` on an unknown key or a bad value."""
    return FoldPlan.model_validate(dict(raw or {}))


__all__ = [
    "FOLD_CONFIGS",
    "FoldBuildPlan",
    "FoldConfigSpec",
    "FoldCorpusPlan",
    "FoldPlan",
    "FoldReleasePlan",
    "fold_config",
    "load_fold_plan",
]
