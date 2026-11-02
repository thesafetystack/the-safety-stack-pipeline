# the-safety-stack-pipeline — AGENTS.md

Reconstructing the AI safety "stack" deployed at frontier labs from public evidence.

Read this before editing anything. Human-facing detail is in `README.md` (setup, first offline run); the v4 docs'
map is `docs/README.md` (operate, consume, how-tos), the design `docs/DESIGN.md`.

v4 is the only supported code in this repository and the production pipeline: its cycle runs weekly on Railway
since 2026-10-31 (`docs/DEPLOY-railway.md`).

## Current — this is v4

- **`stack/`** — the engine, and the only installed package (`pyproject.toml` → `packages = ["stack"]`; `workflows/`
  is imported by name from the repo root in a few places, which the editable install allows): the file
  store (`stack/store/`), the stage runner (`stack/stage_runner.py`), plans, builds, folds (`stack/fold/`), checks,
  the LLM client and its per-run ledger (`stack/llm/`), collect engines, the CLI (`stack/cli/`).
- **`workflows/`** — the stages. `stack/stage_runner.py` (`stage_module_path`) loads `workflows/<phase>/<name>/main.py`
  by path for stage `<phase>.<name>`; a stage exposes `run(ctx) -> StageResult` and optionally `SUBSTAGES`
  (`workflows/README.md`). Order: `collect`, `gather`, `triage`, `acquire`, `fold.corpus`, `annotate`, `fold.build`,
  `fold.release`, `fold.compact` (`PHASE_ORDER`). There is no stage registry: a stage is its module plus a line in a
  plan (`configs/plans/*.yaml`). Prompts are co-located as `<name>.md` and pinned by the plan's `prompts:`.
- Also current: `configs/` (versioned YAML registry, plans, release configs), `evals/`, `spec/schemas/` and
  `spec/types/` (still read; `spec/README.md`), `tests/`, `tools/`, `docs/` (the documentation; `docs/README.md` is its map).

Authority rule: the code and the schema artifacts it reads (`stack/schemas/`, the `configs/` keys that code reads,
`spec/schemas/config/`) are normative for anything mechanical; `docs/DESIGN.md` and `docs/DECISIONS.md` for semantics.
Where they disagree on a mechanic, the code wins. Not normative: `docs/DESIGN.md` App. A (a copy, patched to the
code on 2026-10-13), most of `spec/types/interfaces.py` (v3's DB layer; `spec/README.md` says which parts are
implemented). v3's other payload and aggregate schemas, the `enums.yaml` vocabularies only they named, and v3's
`policies.yaml` keys other than `anchoring` were retired with v3 (read by nothing; removed, see below).

## Removed from this repository

What was removed, and what remains of it: `README.md`, "Removed, and where it lives".

## Taxonomy files

| Dir | What it is |
|---|---|
| `taxonomy/` | `taxonomy-v0.8.yaml` (read by `annotate.technique_roles`; not yet graduated into the pin), the v0.9 catalogues `annotate.technique_staged` and the eval sets pin, `techniques-1.1.0.yaml` (the production pin, D738), the organisation vocabularies (`orgs-0.2.2.yaml` the production one, D778; `stack/orgvocab.py`) and `vendored.lock.yaml`; see `taxonomy/README.md`. The registry's taxonomy pin is the **copy in `configs/`**, per `configs/registry.yaml`. |
| `data-repo/` | The data repository (thesafetystack/the-safety-stack) as a git subtree: `taxonomy/techniques-1.1.0.yaml`, `taxonomy/techniques-1.0.0.yaml`, `taxonomy/taxonomy-v0.9.1.yaml`, `taxonomy/orgs-0.2.0.yaml`, `taxonomy/orgs-0.2.2.yaml`, `taxonomy/models-{1.0.0,1.1.0}.yaml` and `configs/models.yaml` (models 1.1.0) are relative symlinks into its `taxonomies/`, and `stack/catalogue.py` reads its JSON Schemas (`taxonomy/vendored.lock.yaml`, `tests/test_vendored_taxonomies.py`). Do not edit it here; update with `git subtree pull` (`taxonomy/README.md`); orgs 0.2.2 is the one exception, written here first (D778), which the data repository takes byte for byte. |

## Commands

```bash
uv sync --group convert-pdf                                # once; acquire needs the converter tools (docs/SETUP.md)
uv run tools/check_all.py                                  # the gate: configs, payloads, mypy (one file), full suite -n 2. No CI.
uv run --no-sync python tools/devtest.py                   # per edit: touched paths' tests + smoke (docs/SETUP.md §7)
uv run --no-sync pytest -m "not slow" -q -n 2              # the fast tier (~3 min) at a sub-task's end; the full suite at the end
uv run --no-sync pytest -m "e2e" -q -n 2                   # an area (e2e, checks, evalsets): its fast and slow tests (SETUP §7)
uv run --no-sync stack build <plan> --dry-run              # the usual entry; also: run, fold, verify, head, status,
                                                           #   ls, release, diff, chain, eval, migrate, acquire,
                                                           #   overrides, store
uv run --no-sync python tools/probe_collect.py ...         # one live collect query, nothing written (also probe_filters.py)
```

Env: `STACK_STORE`, `STACK_PLAN` (CLI defaults; no default plan), `STACK_ENV_FILE` (path of the key file: keys
`OPENROUTER_API_KEY`, `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (direct LLM providers, D714 / D722),
`X_API_BEARER_TOKEN`, `OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, the S3 store settings `STACK_S3_ENDPOINT_URL`,
`STACK_S3_ACCESS_KEY_ID`, `STACK_S3_SECRET_ACCESS_KEY`, `STACK_S3_DEFAULT_REGION` (older names `STACK_S3_ENDPOINT`,
`AWS_*`, `STACK_S3_REGION` still accepted, a new name in either source winning, D726) and `STACK_MAILTO` are read from
it by name, an environment variable winning).
Never source `.env`. Each run's spend is in its run record (no host-wide ledgers). Full list: `.env.example`; details
`docs/SETUP.md`.

## Conventions

- Stages import `stack.*` and not each other. One exception: `collect.openalex-lookup` and `collect.s2-lookup` import
  their shared code from `workflows/fetch/metadata/main.py` (D92). There is no `workflows/common/`.
- Data between stages flows only through the store: immutable parquet files published by `stack.store.chain` through
  the stage runner; a stage never writes into `data/` itself.
- A new stage is `workflows/<phase>/<name>/main.py` plus a plan line (`workflows/README.md` "Writing a stage"); a
  new family is a schema in `stack/schemas/families/`, a new annotate payload an entry in
  `stack/schemas/payloads/registry.py`; either is a family format change, a new version in
  `stack/schemas/upcasters.py` (`docs/howto/change-family-format.md`, `docs/howto/add-annotator.md`).
- Test on the synthetic world (`tests/synth/world.py`, `STACK_WORKFLOWS_ROOT=tests/synth/stages`) and the fakes in
  `tests/fakes/`; experiment on a reflink copy of a store (`v4/store-run-*/` is gitignored), never on `v4/store-dev`.
