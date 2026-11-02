"""``stack.annotate.prompt_files`` (D591): a workflow's prompt file selected by the plan pin's name.

The split into system and user parts at the one line that is, or ends in, ``{{split:user}}``; includes (expanded, one
trailing newline dropped, no nesting, no traversal); placeholders (every one filled, nothing else,
fills never rescanned); the client's ``{{ref:}}``/``{{input:}}`` untouched; unknown directives and
Jinja-style expressions refused; the name refused when it has a path separator or no file; the digest
moving with the file and with an included file. Each test names the mutation that turns it red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stack.annotate.prompt_files import SPLIT, clear_cache, load_prompt, prompts_dir
from stack.stage_runner import StageError

WHAT = "annotate.test"
BASIC = ("System prefix {{ref:taxonomy}} and {{input:doc}}.\n"
         "{{include:shared.md}}\n"
         f"{SPLIT}\n"
         "Doc: {{document}}\nMenu:{{menu}}\nRules:\n{{include:shared.md}}\nEnd.\n")


def write(d: Path, files: dict[str, str]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text, "utf-8")
    return d


@pytest.fixture(autouse=True)
def _fresh() -> None:
    clear_cache()


def test_split_include_and_fill(tmp_path: Path) -> None:
    # Red under: the split keeping the marker line or dropping the system part's last newline; an include
    # not stripping its one trailing newline; fills rescanned (the document's "{{menu}}" replaced).
    d = write(tmp_path / "prompts", {"p.md": BASIC, "shared.md": "SHARED RULES\n"})
    pf = load_prompt(d, "p", what=WHAT)
    assert pf.system() == "System prefix {{ref:taxonomy}} and {{input:doc}}.\nSHARED RULES\n"
    assert pf.user_placeholders == {"document", "menu"} and pf.system_placeholders == frozenset()
    assert pf.includes == ("shared.md",)
    got = pf.user({"document": "a text naming {{menu}} literally", "menu": "\n- x\n"})
    assert got == "Doc: a text naming {{menu}} literally\nMenu:\n- x\n\nRules:\nSHARED RULES\nEnd.\n"
    assert load_prompt(d, "p", what=WHAT) is pf  # read once per process


def test_system_part_placeholders_are_filled_too(tmp_path: Path) -> None:
    d = write(tmp_path / "prompts", {"p.md": f"Menu: {{{{menu}}}}\n{SPLIT}\n{{{{document}}}}\n"})
    pf = load_prompt(d, "p", what=WHAT)
    assert pf.system({"menu": "M"}) == "Menu: M\n" and pf.user({"document": "D"}) == "D\n"
    with pytest.raises(StageError, match=r"unfilled \['menu'\]"):
        pf.system()


def test_unfilled_and_unknown_placeholders_are_refused(tmp_path: Path) -> None:
    # Red under: _fill substituting what it has and leaving the rest literal (or ignoring an extra fill).
    d = write(tmp_path / "prompts", {"p.md": BASIC, "shared.md": "S\n"})
    pf = load_prompt(d, "p", what=WHAT)
    with pytest.raises(StageError, match=r"unfilled \['menu'\]"):
        pf.user({"document": "x"})
    with pytest.raises(StageError, match=r"not in the file \['truncation'\]"):
        pf.user({"document": "x", "menu": "y", "truncation": ""})


@pytest.mark.parametrize("name", ["sub/p", "..\\p", ".hidden", "", None, 3])
def test_a_name_with_a_path_separator_or_not_a_name_is_refused(tmp_path: Path, name: object) -> None:
    # Red under: load_prompt joining the name into a path unchecked.
    d = write(tmp_path / "prompts", {"p.md": BASIC, "shared.md": "S\n"})
    write(d / "sub", {"p.md": BASIC})
    with pytest.raises(StageError, match="is not a prompt name"):
        load_prompt(d, name, what=WHAT)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    d = write(tmp_path / "prompts", {"p.md": BASIC, "shared.md": "S\n"})
    with pytest.raises(StageError, match=r"annotate\.test: plan pin name 'nope': no prompt file"):
        load_prompt(d, "nope", what=WHAT)


def test_a_line_ending_in_the_split_closes_the_system_part_without_a_newline(tmp_path: Path) -> None:
    # Red under: the split matching whole lines only (refused: no split line), or keeping the text before the
    # marker out of the system part, or adding the line's newline to it (technique_roles' prefix ends in its
    # {{input:document}} marker with no newline after; the golden test there pins the bytes).
    d = write(tmp_path / "prompts", {"p.md": f"Prefix {{{{menu}}}}\n\n{{{{input:document}}}}{SPLIT}\nDoc: {{{{document}}}}\n"})
    pf = load_prompt(d, "p", what=WHAT)
    assert pf.system({"menu": "M"}) == "Prefix M\n\n{{input:document}}"
    assert pf.user({"document": "D"}) == "Doc: D\n"
    two = write(tmp_path / "two", {"p.md": f"a{SPLIT}\nb{SPLIT}\nc\n"})
    with pytest.raises(StageError, match=r"exactly one .* line \(found 2\)"):
        load_prompt(two, "p", what=WHAT)


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_split_line(tmp_path: Path, count: int) -> None:
    body = "".join(f"part {i}\n{SPLIT}\n" for i in range(count)) + "tail\n"
    d = write(tmp_path / "prompts", {"p.md": body})
    with pytest.raises(StageError, match=f"exactly one .* line \\(found {count}\\)"):
        load_prompt(d, "p", what=WHAT)


@pytest.mark.parametrize("inc", ["../outside.md", "sub/x.md", "x.txt", "/etc/passwd"])
def test_an_include_outside_the_prompts_directory_is_refused(tmp_path: Path, inc: str) -> None:
    # Red under: expand() resolving the include name as a path without the name check.
    write(tmp_path, {"outside.md": "secret\n"})
    d = write(tmp_path / "prompts", {"p.md": f"{{{{include:{inc}}}}}\n{SPLIT}\nu\n", "x.txt": "t\n"})
    write(d / "sub", {"x.md": "x\n"})
    with pytest.raises(StageError, match="is not a .md file name"):
        load_prompt(d, "p", what=WHAT)


def test_a_missing_or_nested_include_is_refused(tmp_path: Path) -> None:
    d = write(tmp_path / "prompts", {"p.md": f"{{{{include:none.md}}}}\n{SPLIT}\nu\n"})
    with pytest.raises(StageError, match="no file"):
        load_prompt(d, "p", what=WHAT)
    d2 = write(tmp_path / "p2", {"p.md": f"s\n{SPLIT}\n{{{{include:a.md}}}}\n", "a.md": "{{include:b.md}}\n",
                                 "b.md": "b\n"})
    with pytest.raises(StageError, match="no nesting"):
        load_prompt(d2, "p", what=WHAT)


@pytest.mark.parametrize("bad", ["{{ document }}", "{{split:user}} inline", "{{other:x}}"])
def test_unknown_directives_and_jinja_are_refused(tmp_path: Path, bad: str) -> None:
    # Red under: dropping the _DIRECTIVE scan (a v1.md.j2-style template would load and send its braces).
    d = write(tmp_path / "prompts", {"p.md": f"s\n{SPLIT}\nu {bad}\n"})
    with pytest.raises(StageError, match="unrecognised"):
        load_prompt(d, "p", what=WHAT)


def test_digest_moves_with_the_file_and_with_an_include(tmp_path: Path) -> None:
    # Red under: the digest covering the prompt file only (an edit of the shared rubric would not re-ask).
    d = write(tmp_path / "prompts", {"p.md": BASIC, "shared.md": "S\n", "unused.md": "u\n"})
    base = load_prompt(d, "p", what=WHAT).digest
    write(d, {"unused.md": "changed\n"})
    clear_cache()
    assert load_prompt(d, "p", what=WHAT).digest == base  # a file it does not include is not covered
    write(d, {"shared.md": "S2\n"})
    clear_cache()
    inc = load_prompt(d, "p", what=WHAT).digest
    write(d, {"p.md": BASIC + "more\n"})
    clear_cache()
    edited = load_prompt(d, "p", what=WHAT).digest
    assert len({base, inc, edited}) == 3
    other = write(tmp_path / "copy", {"p.md": BASIC + "more\n", "shared.md": "S2\n"})
    assert load_prompt(other, "p", what=WHAT).digest == edited  # content, not location


def test_prompts_dir_is_beside_main(tmp_path: Path) -> None:
    assert prompts_dir(tmp_path / "wf" / "main.py") == (tmp_path / "wf" / "prompts").resolve()
