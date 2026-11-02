"""Prompt files of an annotate workflow, selected by the plan pin's ``name`` (D591; screen's
convention, D157): ``workflows/annotate/<wf>/prompts/<name>.md``.

**One file holds the whole prompt.** A line that is exactly :data:`SPLIT` (``{{split:user}}``)
splits it: the text above it (its last newline included) is the **system part**, the fixed prefix
the client sends as ``template`` (hashed, cached by the provider); the text below that line, to the
end of the file, is the **user part**, the per-document message. A line that ENDS in :data:`SPLIT`
splits the same way, except that the text before the marker on that line closes the system part,
with no newline after it (so a system part can end in ``{{input:document}}`` without a trailing
newline: technique_roles' prefix). A file with no such line, or two, is refused. Inside either part:

* ``{{include:<file>}}`` is replaced by the text of ``<file>`` (a ``.md`` file in the same
  ``prompts/`` directory; no path separator, no ``..``), minus one trailing newline, so a shared
  rubric lives in one file. An included file may not include or split itself (no nesting);
* ``{{<placeholder>}}`` (``[a-z][a-z0-9_]*``) is a per-call fill the workflow supplies
  (:meth:`PromptFile.system`, :meth:`PromptFile.user`): every placeholder of the part must be
  filled and nothing else may be (both refused, :class:`~stack.stage_runner.StageError`). Fills are
  inserted once, never rescanned, so a document that contains ``{{menu}}`` stays as written;
* ``{{ref:<kind>}}`` and ``{{input:<name>}}`` are the client's markers (D23, :mod:`stack.llm`):
  left untouched here;
* an unknown directive (``{{<word>:``, ``{{split:`` off its line included) or a Jinja-style
  ``{{ x }}`` is refused at load; any other ``{{`` is text.

**Digest** (:attr:`PromptFile.digest`): sha256 over the file's bytes and every included file's
bytes (by name), so an edit of the prompt or of a shared include changes it; a workflow puts it in
its ``PromptPin.template_digests`` (the config digest, so every work key). Fills are not covered:
what a workflow fills in from configuration (a vocabulary, a taxonomy) needs its own digest there.

**Name** (:func:`load_prompt`): a string without a path separator, not starting with ``.``, naming
an existing ``<name>.md``; otherwise :class:`~stack.stage_runner.StageError` before any call. Files
are read once per process and directory (so a run's pin digests and its calls see the same text);
:func:`clear_cache` forgets them (tests).
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from stack.core.hashes import jcs
from stack.stage_runner import StageError

#: The line that splits a prompt file into its system and user parts.
SPLIT = "{{split:user}}"
#: The directory of a workflow's prompt files, beside its ``main.py``.
PROMPTS_DIR = "prompts"

_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_INCLUDE = re.compile(r"\{\{include:([^{}]*)\}\}")
#: A ``{{`` that opens a directive (``{{<word>:``) or a Jinja-style expression (``{{ x }}``).
_DIRECTIVE = re.compile(r"\{\{(?:\s|([a-z]+):)")
#: The directives a part may hold after include expansion: the client's (D23).
_CLIENT_DIRECTIVES = frozenset({"ref", "input"})
_INCLUDE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")


@dataclass(frozen=True)
class PromptFile:
    """One loaded prompt file (module docstring): its ``name``, ``path``, the two parts with includes
    expanded (``system_template``, ``user_template``, placeholders intact), the included file names
    and the ``digest``."""

    name: str
    path: Path
    system_template: str
    user_template: str
    includes: tuple[str, ...]
    digest: str

    @property
    def system_placeholders(self) -> frozenset[str]:
        return frozenset(_PLACEHOLDER.findall(self.system_template))

    @property
    def user_placeholders(self) -> frozenset[str]:
        return frozenset(_PLACEHOLDER.findall(self.user_template))

    def system(self, fills: Mapping[str, str] | None = None) -> str:
        """The system part with ``fills`` (default none) put in."""
        return self._fill(self.system_template, dict(fills or {}), "system")

    def user(self, fills: Mapping[str, str]) -> str:
        """The user part with ``fills`` put in."""
        return self._fill(self.user_template, dict(fills), "user")

    def _fill(self, template: str, fills: dict[str, str], part: str) -> str:
        want = set(_PLACEHOLDER.findall(template))
        missing, unknown = sorted(want - set(fills)), sorted(set(fills) - want)
        if missing or unknown:
            raise StageError(f"prompt {self.name} ({self.path}): {part} part placeholders {sorted(want)}; "
                             f"unfilled {missing}, not in the file {unknown}")
        bad = sorted(k for k, v in fills.items() if not isinstance(v, str))
        if bad:
            raise StageError(f"prompt {self.name}: fills {bad} are not strings")
        return _PLACEHOLDER.sub(lambda m: fills[m.group(1)], template)


def prompts_dir(main_file: str | Path) -> Path:
    """``prompts/`` beside a workflow's ``main.py`` (pass its ``__file__``)."""
    return Path(main_file).resolve().parent / PROMPTS_DIR


def load_prompt(directory: Path, name: object, *, what: str) -> PromptFile:
    """The prompt file ``<directory>/<name>.md`` (module docstring); ``what`` (the stage, e.g.
    ``annotate.model_roles``) names the refusals. Cached per process by directory and name."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith("."):
        raise StageError(f"{what}: plan pin name {name!r} is not a prompt name "
                         f"({directory.name}/<name>.md beside the workflow)")
    path = directory / f"{name}.md"
    if not path.is_file():
        raise StageError(f"{what}: plan pin name {name!r}: no prompt file {path}")
    return _load(str(directory.resolve()), name, what)


def clear_cache() -> None:
    """Forget every loaded prompt file (the next :func:`load_prompt` reads the files again)."""
    _load.cache_clear()


@functools.lru_cache(maxsize=64)
def _load(directory: str, name: str, what: str) -> PromptFile:
    root = Path(directory)
    path = root / f"{name}.md"
    raw = path.read_text("utf-8")
    lines = raw.splitlines(keepends=True)
    at = [i for i, line in enumerate(lines) if line.rstrip("\r\n").endswith(SPLIT)]
    if len(at) != 1:
        raise StageError(f"{what}: prompt file {path} needs exactly one {SPLIT} line (found {len(at)})")
    tail = lines[at[0]].rstrip("\r\n")[:-len(SPLIT)]  # "" for a line that is exactly SPLIT
    system, user = "".join(lines[:at[0]]) + tail, "".join(lines[at[0] + 1:])
    included: dict[str, str] = {}

    def expand(part: str, where: str) -> str:
        def one(m: re.Match[str]) -> str:
            inc = m.group(1)
            if not _INCLUDE_NAME.fullmatch(inc) or ".." in inc:
                raise StageError(f"{what}: {path} ({where}): include {inc!r} is not a .md file name in {root}")
            target = root / inc
            if target.resolve().parent != root or not target.is_file():
                raise StageError(f"{what}: {path} ({where}): include {inc!r}: no file {target}")
            text = included.get(inc)
            if text is None:
                text = included[inc] = target.read_text("utf-8")
            if _INCLUDE.search(text) or SPLIT in text:
                raise StageError(f"{what}: {path}: included file {inc} includes or splits (no nesting)")
            return text[:-1] if text.endswith("\n") else text

        out = _INCLUDE.sub(one, part)
        for d in _DIRECTIVE.finditer(out):
            if d.group(1) not in _CLIENT_DIRECTIVES:
                raise StageError(f"{what}: {path} ({where}): unrecognised {out[d.start():d.start() + 40]!r} "
                                 f"(placeholders are {{{{name}}}}; directives include:, ref:, input:)")
        return out

    system_t, user_t = expand(system, "system part"), expand(user, "user part")
    digest = hashlib.sha256(jcs({"file": raw, "includes": dict(sorted(included.items()))})
                            .encode("utf-8")).hexdigest()
    return PromptFile(name=name, path=path, system_template=system_t, user_template=user_t,
                      includes=tuple(sorted(included)), digest=digest)


__all__ = ["PROMPTS_DIR", "SPLIT", "PromptFile", "clear_cache", "load_prompt", "prompts_dir"]
