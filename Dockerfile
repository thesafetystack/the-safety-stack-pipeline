# The Stack v4 scheduled cycle (tools/scheduled_run.py) as a transient container: everything it keeps lives on the
# S3 store and its ops prefix; the container's disk is scratch (the store's mirror, exports, temp files).
# Setup and operation: docs/DEPLOY-railway.md. Local build:
#   docker build --build-arg RAILWAY_GIT_COMMIT_SHA=$(git rev-parse HEAD) -t stack-cycle .
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

# Debian 13 (trixie), as the machine the production builds ran on; Python 3.12.15, as its venv.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# git: uv fetches fetchpdf from its pinned commit (pyproject.toml). poppler-utils: pdftotext, which
# collect.card-dates-lookup runs on card PDFs. tini: PID 1, forwards SIGTERM to the cycle and reaps converter
# children (TINI_SUBREAPER=1 below: also when it is not PID 1). No browser: the http lane's headless rung is off
# in every plan (docs/SETUP.md §1).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git poppler-utils tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12

WORKDIR /app
# The groups: convert-pdf (acquire's converters, held at the convert pin, D696), s3 (boto3), release (the Hugging
# Face client for the gated upload). --frozen: exactly uv.lock, never a re-resolve; --no-dev: no test tools.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --group convert-pdf --group s3 --group release
COPY . .
RUN uv sync --frozen --no-dev --group convert-pdf --group s3 --group release \
 && .venv/bin/python -c "import pymupdf4llm, trafilatura, boto3, huggingface_hub" \
 && command -v pdftotext

# The image's code commit, for the release's release_code_commit (the build context has no .git). Railway passes
# RAILWAY_GIT_COMMIT_SHA to the build when the Dockerfile declares it.
ARG RAILWAY_GIT_COMMIT_SHA=""
ENV STACK_CODE_COMMIT=${RAILWAY_GIT_COMMIT_SHA}

# Scratch on the container's ephemeral disk; nothing here outlives the run. /app (the working directory of every
# stack process) is root-owned and not writable by `stack`: every write goes under /work. TMPDIR: Python's tempfile
# and duckdb's spill directories (stack/duck.py; duckdb's default `.tmp` is relative to the working directory, D732).
# HF_HOME: the Hugging Face client's cache and token files (it would default to $XDG_CACHE_HOME/huggingface).
RUN useradd --create-home --uid 1000 stack \
 && mkdir -p /work/mirror /work/scratch /work/cache /work/state /work/providers \
 && chown -R stack:stack /work
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    STACK_MIRROR=/work/mirror \
    STACK_SCHED_WORK=/work \
    STACK_STATE_DIR=/work/state \
    STACK_PROVIDER_STATE=/work/providers \
    XDG_CACHE_HOME=/work/cache \
    HF_HOME=/work/cache/huggingface \
    TMPDIR=/work/scratch \
    STACK_PROGRESS=0 \
    TINI_SUBREAPER=1
USER stack

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/.venv/bin/python", "/app/tools/scheduled_run.py", "cycle"]
