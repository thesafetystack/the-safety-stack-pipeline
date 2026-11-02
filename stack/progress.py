"""Live progress reporting for long-running workflow units (30-stack-module §9).

A collect sweep is minutes-to-hours of politeness-delayed HTTP with nothing to show for it until
the run closes — a 20-minute arXiv pass is indistinguishable from a hang. This is the one place
that renders "unit k of n" so every workflow reports the same way and no workflow has to decide
how to print.

Design constraints, in the order that mattered:

* **Never on stdout.** The CLI's stdout is its result line, which people pipe and parse. Progress
  goes to stderr.
* **Never a hazard.** A workflow that reports progress must behave identically when nothing is
  listening: :func:`resolve` hands out a :class:`NullProgress` when nothing is attached, so a
  caller constructed directly by a test reports into the void without a branch at the call site
  (v3's runner ``Services`` carried it as a default; retired at T4.5).
* **Readable when captured.** A carriage-return bar is right for a terminal and garbage in a log
  file or a background-task capture, which is exactly where a long run ends up. On a non-TTY the
  reporter emits periodic newline-terminated lines instead, throttled by time rather than by
  every step, so a 2,269-unit sweep leaves a readable trail rather than 2,269 lines.
* **A unit in flight is visible.** Units are coarse (a forum query can be 5,000 hits, each a few
  database round trips), so a bar that only moves between units sits at 0/N for half an hour on
  a first pass and reads as a hang. ``tick`` counts the items inside the current unit and the
  line shows them with a rate; ``step`` always redraws on a terminal, so the first unit's label
  appears the moment it starts rather than when the second one does.
* **The network phase reports too.** Half of a collect unit's wall-clock is a paginated HTTP pull
  with politeness sleeps in it (3.0 s per arXiv page), and it happens BEFORE there is a single
  item to tick — the worst silence in the stack. ``note`` re-labels the line without touching
  any counter, so a fetcher can say which page it is on while it sleeps.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Protocol, TextIO

ENV_DISABLE = "STACK_PROGRESS"  # '0'/'off'/'false' silences it; unset = auto by TTY

_BAR_WIDTH = 24
_TTY_MIN_INTERVAL_S = 0.1   # redraw cap on a terminal
_LOG_MIN_INTERVAL_S = 15.0  # line cadence when captured to a file/pipe


class ProgressReporter(Protocol):
    """What a workflow may call. Implemented by :class:`NullProgress` and :class:`Progress`."""

    def start(self, total: int, *, unit: str = "units", sub_unit: str = "items") -> None: ...
    def step(self, note: str = "", n: int = 1) -> None: ...
    def tick(self, n: int = 1) -> None: ...
    def note(self, text: str) -> None: ...
    def finish(self) -> None: ...


class NullProgress:
    """The no-op default: reporting is free and safe when nothing is listening."""

    def start(self, total: int, *, unit: str = "units", sub_unit: str = "items") -> None:
        return None

    def step(self, note: str = "", n: int = 1) -> None:
        return None

    def tick(self, n: int = 1) -> None:
        return None

    def note(self, text: str) -> None:
        return None

    def finish(self) -> None:
        return None


def _fmt_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class Progress:
    """Renders one workflow's unit progress to a stream.

    ``start`` sets the denominator, ``step`` advances by ``n`` with an optional label for the unit
    in flight (a query, an author, a URL), ``tick`` counts items processed INSIDE that unit (a hit,
    a work, an entry — reset on every ``step``), ``note`` re-labels it without counting anything,
    ``finish`` closes the line. ETA is a running
    average over the elapsed run, which is the right estimator here: sweep units are
    politeness-bound and therefore close to constant-cost, so the average predicts better than a
    decaying window.
    """

    def __init__(
        self,
        label: str,
        *,
        stream: TextIO | None = None,
        isatty: bool | None = None,
        min_interval_s: float | None = None,
    ) -> None:
        self._label = label
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._tty = self._stream.isatty() if isatty is None else isatty
        # Redraw cadence: fast on a terminal, sparse when captured. Injectable so a test can
        # observe an intermediate render without sleeping through the throttle.
        self._interval = (
            min_interval_s
            if min_interval_s is not None
            else (_TTY_MIN_INTERVAL_S if self._tty else _LOG_MIN_INTERVAL_S)
        )
        self._total = 0
        self._done = 0
        self._outcome = 'done'
        self._unit = "units"
        self._sub_unit = "items"
        self._note = ""        # the unit in flight (set by step)
        self._phase = ""       # what that unit is doing right now (set by note, cleared by tick)
        self._sub = 0          # items ticked inside the unit in flight
        self._sub_t0 = 0.0     # when that unit started (for the per-item rate)
        self._t0 = 0.0
        self._last_render = 0.0
        self._open = False

    # ── ProgressReporter ──────────────────────────────────────────────────────────────────────
    def start(self, total: int, *, unit: str = "units", sub_unit: str = "items") -> None:
        self._total = max(0, int(total))
        self._done = 0
        self._unit = unit
        self._sub_unit = sub_unit
        self._note = ""
        self._phase = ""
        self._sub = 0
        self._t0 = time.monotonic()
        self._sub_t0 = self._t0
        self._last_render = 0.0
        self._open = True
        if self._total:
            self._render(force=True)

    def step(self, note: str = "", n: int = 1) -> None:
        if not self._open:
            return
        self._done += n
        self._note = note
        self._phase = ""
        self._sub = 0
        self._sub_t0 = time.monotonic()
        # A new unit in flight is always worth a redraw on a terminal (steps are coarse and rare;
        # without this the first step lands inside start()'s throttle window and the bar shows
        # 0/N until the SECOND unit begins). Captured output keeps its time cadence.
        self._render(force=self._tty)

    def tick(self, n: int = 1) -> None:
        """Count ``n`` items processed inside the unit in flight; throttled redraw."""
        if not self._open:
            return
        # Processing has started, so a "fetching…" phase label from the network half of this unit
        # is now stale — and a stale phase is worse than none, it misreports what is happening.
        self._phase = ""
        self._sub += n
        self._render()

    def note(self, text: str) -> None:
        """Re-label the unit in flight WITHOUT advancing any counter; throttled redraw.

        For the phase of a unit that has no countable items yet — a paginated HTTP pull with a
        politeness sleep between pages. ``step`` would lie about progress and ``tick`` would
        double-count the items the processing loop is about to tick, so neither fits.

        The text is a PHASE ("fetching, 1,400 works"), shown after the unit's own label; it is
        cleared by the next ``tick`` or ``step``, so it can never outlive the phase it describes.
        """
        if not self._open:
            return
        self._phase = text
        self._render()

    def finish(self) -> None:
        if not self._open:
            return
        self._render(force=True, final=True)
        self._open = False

    # ── rendering ─────────────────────────────────────────────────────────────────────────────
    def _render(self, *, force: bool = False, final: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_render) < self._interval:
            return
        self._last_render = now
        elapsed = now - self._t0
        line = self._line(elapsed, now - self._sub_t0, final=final)
        try:
            if self._tty:
                # \r + clear-to-EOL: one line that rewrites itself; \n only when finishing.
                self._stream.write("\r\x1b[2K" + line + ("\n" if final else ""))
            else:
                self._stream.write(line + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            # A closed or broken stream must never take a run down mid-sweep.
            self._open = False

    def _line(self, elapsed: float, sub_elapsed: float, *, final: bool) -> str:
        done, total = self._done, self._total
        pct = (done / total) if total else 0.0
        head = f"{self._label} {done}/{total} {self._unit}"
        if self._tty:
            filled = int(pct * _BAR_WIDTH)
            head = f"{self._label} [{'█' * filled}{'·' * (_BAR_WIDTH - filled)}] {done}/{total}"
        parts = [head, f"{pct * 100:.0f}%"]
        if final:
            outcome = getattr(self, '_outcome', 'done')
            parts.append(f"{outcome} in {_fmt_duration(elapsed)}")
            return "  ".join(parts)
        parts.append(f"{_fmt_duration(elapsed)} elapsed")
        # ETA counts the unit in flight as started, so it is defined from the first unit on; on
        # a 1/N first pass it is a guess, but a moving guess beats a blank field for 25 minutes.
        if done and total:
            rate = elapsed / done
            parts.append(f"eta {_fmt_duration(rate * (total - done))}")
        if self._note:
            parts.append(_truncate(self._note, 40))
        if self._phase:
            parts.append(_truncate(self._phase, 32))
        if self._sub:
            per_s = self._sub / sub_elapsed if sub_elapsed > 0 else 0.0
            parts.append(f"{self._sub:,} {self._sub_unit} ({per_s:.1f}/s)")
        return "  ".join(parts)


def _truncate(text: str, width: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def from_env(label: str, *, stream: TextIO | None = None) -> ProgressReporter:
    """The reporter for one run: a live :class:`Progress` unless ``$STACK_PROGRESS`` disables it.

    Enabled by default in both modes (TTY bar / throttled log lines) — a silent long run is the
    problem this exists to fix, so the useful behaviour is the one you get without opting in.
    """
    setting = (os.environ.get(ENV_DISABLE) or "").strip().lower()
    if setting in ("0", "off", "false", "no"):
        return NullProgress()
    return Progress(label, stream=stream)


def resolve(services: Any) -> ProgressReporter:
    """The reporter on ``services``, or a no-op when the attribute is absent.

    Workflows call this instead of touching the attribute, so a ``Services`` built by older code
    (or by a test that constructs it positionally) can never raise ``AttributeError`` mid-sweep.
    """
    reporter = getattr(services, "progress", None)
    return reporter if reporter is not None else NullProgress()
