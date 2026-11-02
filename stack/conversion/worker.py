"""The memory-capped, per-item conversion subprocess (10-design §5.3 poison-item quarantine).

Invoked by ``workflows/acquire/convert.py:convert_one`` as ``python worker.py <request.json>``. A *fresh* interpreter per
item is the isolation boundary: a crashing PDF (docling native ``bad_alloc``/segfault, a mupdf
abort) takes down THIS process only — never the runner — and the item it was converting is known
exactly, so quarantine attribution is trivial (no FIFO-prefix guessing; contrast v1's shared-queue
worker pool, where over-broad quarantine self-reinforced).

The heap cap is set with **RLIMIT_DATA (heap + private anon mmaps), NOT RLIMIT_AS**: torch/CUDA
reserves tens of GB of *address space* at init while touching almost none of it, so an
address-space cap left docling no headroom and every page falsely ``bad_alloc``'d (the v1
2026-07-13..16 regression). Under an RLIMIT_DATA cap a pathological allocation fails as a clean
per-page ``std::bad_alloc`` and docling degrades page-by-page instead of aborting the process.
The cap is set BEFORE any heavy import so it bounds torch/docling from first allocation.

**Thread caps** (T3.x-f4). Each pymupdf4llm child ran ~67 threads (pymupdf's layout models run in
onnxruntime sessions whose intra-op pool defaults to one thread per core, plus the BLAS/OpenMP
pools), so three workers put ~80 runnable threads on 22 cores. ``threads`` (``n >= 1``) caps them
before any heavy import: the native pools through :data:`THREAD_ENV` (read by OpenMP, OpenBLAS,
MKL, numexpr, Accelerate, BLIS and torch at initialisation; docling takes its default thread count
from ``OMP_NUM_THREADS``), and onnxruntime, which reads none of them, by wrapping
``InferenceSession.__init__`` so a session created without explicit counts gets
``intra_op_num_threads = n`` and ``inter_op_num_threads = 1`` (:func:`_cap_onnxruntime`). Measured
on a 1.7 MB paper PDF: 67 threads uncapped, 47 with the environment only, 5 with both.
``threads`` absent or ``0`` leaves the libraries' defaults.

Protocol: the request JSON carries ``{raw_path, media_type, mem_gb, out_path, origin, tier,
threads, latexml, pdf_raw}`` (``pdf_raw``: a PDF takes the raw-text tier alone, ``ladder.convert``).
``tier`` is the convert config's ladder entry: ``2`` disables tier 1 (docling) for every kind
before the ladder is imported (``STACK_CONVERT_NO_DOCLING``), ``1`` leaves it to the ladder. On
success (clean or a recorded conversion failure) the worker writes the
:class:`~ladder.ConversionResult` dict to ``out_path`` and exits 0. On a HARD crash the process
dies and ``out_path`` is absent — that absence is how the parent detects the crash and attributes
it to this one item.

Fault switches (tests; read from the environment, never set by the pipeline):
``STACK_CONVERT_ABORT`` aborts after the cap is set (a native crash: no result);
``STACK_CONVERT_HANG`` sleeps that many seconds before converting (a hang: the parent's timeout);
``STACK_CONVERT_REPORT`` names a file the child writes its applied thread caps to (the
:data:`THREAD_ENV` values it sees and the onnxruntime cap) before converting.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _apply_heap_cap(mem_gb: float) -> None:
    """RLIMIT_DATA cap (heap + anon mmap), set before any heavy import. ``mem_gb <= 0`` disables.
    Never break the worker over the guard (an unsupported platform just runs uncapped)."""
    if mem_gb <= 0:
        return
    try:
        import resource  # noqa: PLC0415 — POSIX-only; guarded

        cap = int(mem_gb * 1024**3)
        resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))
    except (ValueError, OSError, ImportError):
        pass


#: Environment variables that size native thread pools, set to the cap before any heavy import.
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
              "NUMEXPR_MAX_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS")


def _apply_thread_caps(threads: int) -> dict[str, object]:
    """Cap the child's thread pools at ``threads`` (module docstring); ``<= 0`` leaves them. Returns
    what was applied (``STACK_CONVERT_REPORT``)."""
    if threads <= 0:
        return {"threads": 0}
    for key in THREAD_ENV:
        os.environ[key] = str(threads)
    return {"threads": threads, "env": {k: os.environ[k] for k in THREAD_ENV},
            "onnxruntime": _cap_onnxruntime(threads)}


def _cap_onnxruntime(threads: int) -> str:
    """Wrap ``onnxruntime.InferenceSession.__init__`` so sessions created without explicit thread
    counts get ``intra_op = threads``, ``inter_op = 1``. ``'absent'`` when onnxruntime is not
    installed (it is then never imported here: the import is the cost only when it would be used)."""
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("onnxruntime") is None:
        return "absent"
    import onnxruntime as ort  # noqa: PLC0415

    original = ort.InferenceSession.__init__
    if getattr(original, "_stack_threads", None) is not None:
        return "capped"

    def init(self: object, path_or_bytes: object, sess_options: object = None, *args: object,
             **kwargs: object) -> None:
        so = sess_options if sess_options is not None else ort.SessionOptions()
        if not so.intra_op_num_threads:
            so.intra_op_num_threads = threads
        if not so.inter_op_num_threads:
            so.inter_op_num_threads = 1
        original(self, path_or_bytes, so, *args, **kwargs)

    init._stack_threads = threads  # type: ignore[attr-defined]
    ort.InferenceSession.__init__ = init  # type: ignore[method-assign]
    return "capped"


def main(argv: list[str]) -> int:
    request = json.loads(Path(argv[1]).read_text())
    mem_gb = float(request.get("mem_gb", 0) or 0)
    _apply_heap_cap(mem_gb)
    applied = _apply_thread_caps(int(request.get("threads", 0) or 0))
    report = os.environ.get("STACK_CONVERT_REPORT", "").strip()
    if report:
        Path(report).write_text(json.dumps(applied))

    # Test hook: prove the parent's hard-crash detection + single-item attribution without a real
    # poison PDF. Aborts AFTER the cap is set and BEFORE any output is written — exactly a native
    # crash mid-conversion (no out_path ⇒ the parent records status='crashed').
    if os.environ.get("STACK_CONVERT_ABORT", "").strip() not in ("", "0", "false", "False"):
        os.abort()
    hang = os.environ.get("STACK_CONVERT_HANG", "").strip()
    if hang:
        time.sleep(float(hang))
    if int(request.get("tier", 1) or 1) >= 2:
        os.environ["STACK_CONVERT_NO_DOCLING"] = "1"  # the config enters the ladder below docling

    out_path = Path(request["out_path"])
    # Import the sibling ladder AFTER the cap is applied (docling/torch allocate at import).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ladder  # noqa: PLC0415

    try:
        raw = Path(request["raw_path"]).read_bytes()
        result = ladder.convert(
            raw, request.get("media_type"), workdir=out_path.parent,
            origin=request.get("origin"), latexml=bool(request.get("latexml", False)),
            pdf_raw=bool(request.get("pdf_raw", False)),
        )
        payload = result.to_dict()
    except Exception as exc:  # a clean (non-crash) failure — recorded, retryable
        payload = ladder.ConversionResult(
            status="failed", error=f"{type(exc).__name__}: {exc}", error_class="parse_error"
        ).to_dict()
    out_path.write_text(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

