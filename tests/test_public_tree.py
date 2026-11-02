"""The repository is public (D796): no tracked text file links to a link-shared document or chat.

Every tracked text file (code, docs, configs, the eval sets' READMEs, scripts and pins) is scanned for
:data:`stack.fold.dataset.SHARED_LINKS` (Google Docs/Drive document ids, Claude/ChatGPT/Gemini share, chat or
artifact links, Notion pages, Dropbox shares). The eval sets' data files (``evals/**/*.jsonl``, ``*.csv``) are left
out: they hold ingested third-party document text, which the release's own checks treat separately. A host name
alone (the URL rules of :mod:`stack.urls`) is not a link; tests build such URLs from parts and synthetic ids.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from stack.fold.dataset import SHARED_LINKS

REPO = Path(__file__).resolve().parents[1]
DATA = re.compile(r"^evals/.*\.(?:jsonl|csv)$")


def tracked_text_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout: the tracked files are unknown")
    return [REPO / f for f in out.decode().split("\0") if f and not DATA.match(f)]


def test_no_tracked_file_links_to_a_shared_document() -> None:
    # Red under: a Google Doc, Drive file, chat share or Notion page link in any tracked text file.
    pats = [re.compile(p) for p in SHARED_LINKS]
    hits = []
    for p in tracked_text_files():
        try:
            text = p.read_text("utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        hits += [f"{p.relative_to(REPO)}: {m.group(0)[:40]}" for pat in pats for m in pat.finditer(text)]
    assert hits == []


@pytest.mark.parametrize("url", [
    "https://docs.google.com/" + "document/d/1" + "A" * 43 + "/edit",
    "https://docs.google.com/" + "spreadsheets/d/1" + "B" * 43,
    "https://drive.google.com/" + "file/d/1" + "C" * 32 + "/view",
    "https://claude.ai/" + "share/0123abcd-" + "e" * 12,
    "https://chatgpt.com/" + "share/0123abcd" + "f" * 8,
    "https://www.notion.so/" + "Team-page-" + "0" * 32,
    "https://www.dropbox.com/" + "s/abcdef123/file.pdf",
])
def test_the_patterns_see_each_kind(url: str) -> None:
    assert any(re.search(p, url) for p in SHARED_LINKS)


def test_a_host_name_alone_is_not_a_link() -> None:
    assert not any(re.search(p, "docs.google.com drive.google.com claude.ai dropbox.com") for p in SHARED_LINKS)
