"""Progress behaviour of the annotate ask substage (v3's screen ``_Progress``, now
``stack.annotate.stage.AskProgress``, which drives every ``annotate.<wf>`` worker pool)."""
from __future__ import annotations

from typing import Any

from stack.annotate import stage


def test_progress_has_heartbeat_without_completions(capsys: Any, monkeypatch: Any) -> None:
    now = [0.0]
    monkeypatch.setattr(stage.time, "monotonic", lambda: now[0])
    monkeypatch.delenv("STACK_PROGRESS", raising=False)
    progress = stage.AskProgress(100, 10, "screen")
    progress.active = 10
    progress.show(0, 0, 0)
    now[0] = 11
    progress.show(0, 0, .1)
    output = capsys.readouterr().err
    assert output.count("0/100 saved") == 2
    assert "10/10 in flight" in output and "ETA --" in output
