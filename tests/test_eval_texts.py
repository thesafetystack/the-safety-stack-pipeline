"""The eval-text cache (D790): where a frozen set's whole text is looked up, and ``tools/fetch_eval_texts.py``'s
pin checks, offline (a synthetic archive; the tool's S3 path is not exercised here)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from collections import defaultdict
from pathlib import Path

import pytest

from stack.evals import freeze as F
from stack.evals import harness


def _gz(text: str) -> bytes:
    return F.gzip_text(text)


def _set(root: Path, task: str, set_id: str, texts: dict[str, str], manifest: bool = True) -> Path:
    d = root / "evals" / task / set_id
    d.mkdir(parents=True)
    rows = [{"item_id": i, "input": {"full_text_digest": hashlib.sha256(t.encode()).hexdigest()}} for i, t in texts.items()]
    (d / "items.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    if manifest:
        (d / "manifest.json").write_text(json.dumps({"task": task, "set_id": set_id}), "utf-8")
    return d


def test_cache_root_follows_the_env_then_xdg_then_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Red under: the env var not taking precedence, or the XDG cache directory ignored.
    monkeypatch.setenv("STACK_EVAL_TEXTS_DIR", str(tmp_path / "c"))
    assert F.texts_cache_root() == tmp_path / "c"
    monkeypatch.delenv("STACK_EVAL_TEXTS_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x"))
    assert F.texts_cache_root() == tmp_path / "x" / "stack" / "eval-texts"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert F.texts_cache_root() == tmp_path / "h" / ".cache" / "stack" / "eval-texts"


def test_a_text_is_read_from_the_set_first_then_from_the_cache(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    # Red under: find_text not falling back to the cache, the cache keyed by the directory name instead of the
    # manifest's (a copied set), or the set's own file not taking precedence (a test's edited copy).
    cache = tmp_path / "cache"
    monkeypatch.setenv("STACK_EVAL_TEXTS_DIR", str(cache))
    d = _set(tmp_path / "copy", "screen", "2026-10-12a", {"a": "alpha"})
    d = d.rename(d.parent / "renamed")
    item = json.loads((d / "items.jsonl").read_text("utf-8"))
    assert F.find_text(d, "a") is None
    with pytest.raises(harness.EvalError, match=r"a: whole text not found .*fetch_eval_texts\.py"):
        harness.read_frozen_text(d, item)
    (cache / "screen" / "2026-10-12a").mkdir(parents=True)
    (cache / "screen" / "2026-10-12a" / "a.txt.gz").write_bytes(_gz("alpha"))
    assert harness.read_frozen_text(d, item) == "alpha"
    (d / "texts").mkdir()
    (d / "texts" / "a.txt.gz").write_bytes(_gz("own"))
    assert harness.read_frozen_text(d, item) == "own"
    bare = _set(tmp_path / "bare", "model_roles", "2026-10-13a", {"b": "beta"}, manifest=False)
    assert F.cached_text_path(bare, "b") == cache / "model_roles" / "2026-10-13a" / "b.txt.gz"


def _tar_zst(members: dict[str, bytes]) -> bytes:
    import zstandard

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(raw.getvalue())


@pytest.mark.tools
def test_fetch_writes_only_texts_that_match_their_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: a text written without its digest checked, an item of another set accepted, or the exit status
    # not reporting an incomplete set.
    import fetch_eval_texts as fe

    _set(tmp_path, "screen", "s1", {"a": "alpha", "b": "beta"})
    _set(tmp_path, "technique_staged", "s2", {"c": "gamma"})
    pins = fe.pins(tmp_path / "evals")
    assert sorted(pins) == [("screen", "s1"), ("technique_staged", "s2")]
    dest = tmp_path / "cache"
    tally: dict = defaultdict(fe.Tally)
    fe.ingest(_tar_zst({"evals/screen/s1/texts/a.txt.gz": _gz("alpha"),
                        "evals/screen/s1/texts/b.txt.gz": _gz("not beta"),
                        "evals/screen/other/texts/z.txt.gz": _gz("zeta"),
                        "evals/screen/s1/items.jsonl": b"{}"}), pins, dest, tally)
    assert (dest / "screen" / "s1" / "a.txt.gz").read_bytes() == _gz("alpha")
    assert not (dest / "screen" / "s1" / "b.txt.gz").exists() and not (dest / "screen" / "other").exists()
    assert fe.report(pins, dest, tally) == 1
    out = capsys.readouterr().out
    assert "screen/s1: 1/2 verified (1 written, 0 already there, 1 refused) INCOMPLETE" in out
    assert "technique_staged/s2: 1 items, no texts found" in out and "refused b: text hashes to" in out
    tally = defaultdict(fe.Tally)
    fe.ingest(_tar_zst({"x/screen/s1/texts/a.txt.gz": _gz("alpha"), "x/screen/s1/texts/b.txt.gz": _gz("beta")}),
              pins, dest, tally)
    assert fe.report(pins, dest, tally) == 0
    assert "screen/s1: 2/2 verified (1 written, 1 already there) ok" in capsys.readouterr().out
    (dest / "screen" / "s1" / "b.txt.gz").write_bytes(gzip.compress(b"moved", mtime=0))
    tally = defaultdict(fe.Tally)
    fe.check(pins, dest, tally)
    assert fe.report(pins, dest, tally) == 1
    assert "refused b: cached text does not match its pin" in capsys.readouterr().out


@pytest.mark.tools
def test_archived_eval_files_are_fetched_against_their_pins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: a fetched file written without its pin checked, find_eval_file not falling back to the cache, or
    # the repository's evals/archived.sha256 naming a file that is still in the tree.
    import fetch_eval_texts as fe

    monkeypatch.setenv("STACK_EVAL_TEXTS_DIR", str(tmp_path / "cache"))
    evals = tmp_path / "evals"
    evals.mkdir()
    good, bad = b"one\n", b"two\n"
    (evals / F.ARCHIVED_PINS).write_text(f"{hashlib.sha256(good).hexdigest()}  s/a/labels/d.jsonl\n"
                                         f"{hashlib.sha256(b'other').hexdigest()}  calibration/x.md\n", "utf-8")
    pins = F.archived_pins(evals)
    store = {"s/a/labels/d.jsonl": good, "calibration/x.md": bad}
    assert fe.excerpts(store.__getitem__, pins, tmp_path / "cache") == 1
    assert "calibration/x.md: refused" in capsys.readouterr().out
    assert F.find_eval_file(evals, "s/a/labels/d.jsonl") == tmp_path / "cache" / "s" / "a" / "labels" / "d.jsonl"
    assert F.find_eval_file(evals, "calibration/x.md") is None
    assert fe.excerpts(None, pins, tmp_path / "cache") == 1  # --check: the refused one is missing
    real = F.archived_pins(Path(fe.EVALS))
    assert real and not [r for r in real if (Path(fe.EVALS) / r).exists()]


def test_archived_items_are_read_from_the_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # D795. Red under: items_file not falling back to the cache, or the set's own items.jsonl not taking precedence.
    monkeypatch.setenv("STACK_EVAL_TEXTS_DIR", str(tmp_path / "cache"))
    d = _set(tmp_path, "triage", "2026-10-12a", {"a": "alpha"})
    (d / "items.jsonl").rename(tmp_path / "items.jsonl")
    assert F.items_file(d) == d / "items.jsonl" and not F.items_file(d).is_file()
    with pytest.raises(harness.EvalError, match="neither the set nor the eval-text cache"):
        harness.load_set(tmp_path / "evals", "triage", "2026-10-12a")
    (tmp_path / "cache" / "triage" / "2026-10-12a").mkdir(parents=True)
    (tmp_path / "items.jsonl").rename(tmp_path / "cache" / "triage" / "2026-10-12a" / "items.jsonl")
    assert [i["item_id"] for i in harness.load_set(tmp_path / "evals", "triage", "2026-10-12a").items] == ["a"]
