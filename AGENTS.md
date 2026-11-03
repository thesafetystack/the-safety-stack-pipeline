# the-safety-stack-pipeline — AGENTS.md

The pipeline that builds The Safety Stack dataset: collect documents on technical AI safety, triage, fetch, screen,
annotate, release. Read this before editing anything. Humans start at `README.md`; the docs' map is
`docs/README.md`, the design `docs/DESIGN.md`.

This repository is public, and production runs from it: a weekly cycle on Railway (`tools/scheduled_run.py cycle`,
`docs/DEPLOY-railway.md`) built from branch `deploy/railway`, which the maintainers fast-forward to reviewed commits
of `main`. Work on a branch off `main`; push (to `deploy/railway` or anywhere) only on the maintainer's explicit
instruction.

## Architecture

- **`stack/`** — the engine and the only installed package (`pyproject.toml` → `packages = ["stack"]`; `workflows/`
  is imported by name from the repo root in a few places, which the editable install allows): the file store
  (`stack/store/`), the stage runner (`stack/stage_runner.py`), plans, builds, folds (`stack/fold/`), checks, the
  LLM client and its per-run ledger (`stack/llm/`), collect engines, the CLI (`stack/cli/`).
- **`workflows/`** — the stages. `stack/stage_runner.py` (`stage_module_path`) loads `workflows/<phase>/<name>/main.py`
  by path for stage `<phase>.<name>`; a stage exposes `run(ctx) -> StageResult` and optionally `SUBSTAGES`
  (`workflows/README.md`). Order: `collect`, `gather`, `triage`, `acquire`, `fold.corpus`, `annotate`, `fold.build`,
  `fold.release`, `fold.compact` (`PHASE_ORDER`). There is no stage registry: a stage is its module plus a line in a
  plan (`configs/plans/*.yaml`; production: `batch-2026-10a`). Prompts are co-located as `<name>.md` and pinned by
  the plan's `prompts:`.
- **`configs/`** — the versioned YAML registry, plans, release configs; `configs/releases/dataset/` holds the dataset
  card and FORMAT templates (`README.card.md`, `FORMAT.head.md`), filled by `stack/fold/dataset.py` at export.
- Also: `evals/` (frozen eval sets, the judge evaluation), `spec/schemas/` and `spec/types/` (still read;
  `spec/README.md`), `tests/`, `tools/`, `docs/`.

**Authority.** The code and the schema artifacts it reads (`stack/schemas/`, the `configs/` keys that code reads,
`spec/schemas/config/`) are normative for anything mechanical; `docs/DESIGN.md` and `docs/DECISIONS.md` for
semantics. Where they disagree on a mechanic, the code wins. Not normative: `docs/DESIGN.md` App. A (a copy, patched
to the code on 2026-10-13) and most of `spec/types/interfaces.py` (`spec/README.md` says which parts are implemented).

**Taxonomies.** `data-repo/` is the data repository (thesafetystack/the-safety-stack) as a squashed git subtree; the
files the pipeline reads are relative symlinks from `taxonomy/` and `configs/models.yaml` into its `taxonomies/`,
pinned by tag and sha256 in `taxonomy/vendored.lock.yaml` (`tests/test_vendored_taxonomies.py`), and
`stack/catalogue.py` reads its JSON Schemas. Do not edit `data-repo/` here: change the data repository and `git
subtree pull` (`taxonomy/README.md`). The other files in `taxonomy/` are older or draft catalogues still read by
eval sets and non-production stages.

## Commands

```bash
uv sync --frozen --group convert-pdf --group release      # never a plain `uv sync` (it drops these groups)
uv run --no-sync tools/check_all.py --no-sync             # the gate: configs, payloads, mypy (one file), full suite. No CI.
uv run --no-sync python tools/devtest.py                  # per edit: touched paths' tests + smoke (docs/SETUP.md §7)
uv run --no-sync pytest -m "not slow" -q -n 2             # the fast tier (~3 min) at a sub-task's end
uv run --no-sync pytest -m "e2e" -q -n 2                  # an area (e2e, checks, evalsets): its fast and slow tests
uv run --no-sync stack build <plan> --dry-run             # the usual entry; also: run, fold, verify, head, status,
                                                          #   stop, ls, show, release, export, diff, chain, eval,
                                                          #   migrate, acquire, overrides, store
uv run --no-sync python tools/probe_collect.py ...        # one live collect query, nothing written (also probe_filters.py)
```

The full gate takes ~13 minutes at the default workers; without the eval-text cache (`docs/SETUP.md` §7) the
eval-set tests skip, which is expected. Run it before committing anything under `stack/`, `workflows/` or `configs/`.

Env: `STACK_STORE`, `STACK_PLAN` (CLI defaults; no default plan) and `STACK_ENV_FILE` (the path of the key file).
Keys and the S3 store settings are read by name from the environment, else from that file: the LLM keys
(`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`), the provider keys (`OPENALEX_API_KEY`,
`SEMANTIC_SCHOLAR_API_KEY`, `X_API_BEARER_TOKEN`), `STACK_S3_ENDPOINT_URL`, `STACK_S3_ACCESS_KEY_ID`,
`STACK_S3_SECRET_ACCESS_KEY`, `STACK_S3_DEFAULT_REGION` (older names still accepted, D726) and `STACK_MAILTO`.
Never source `.env`. The scheduled cycle's settings are `STACK_SCHED_*` (`tools/scheduled_run.py`). Each run's
spend is in its run record. Full list: `.env.example`; details: `docs/SETUP.md`.

## Conventions

- Stages import `stack.*` and not each other. One exception: `collect.openalex-lookup` and `collect.s2-lookup` import
  their shared code from `workflows/fetch/metadata/main.py` (D92). There is no `workflows/common/`.
- Data between stages flows only through the store: immutable parquet files published by `stack.store.chain` through
  the stage runner; a stage never writes into `data/` itself.
- A new stage is `workflows/<phase>/<name>/main.py` plus a plan line (`workflows/README.md` "Writing a stage"); a
  new family is a schema in `stack/schemas/families/`, a new annotate payload an entry in
  `stack/schemas/payloads/registry.py`; either is a family format change, a new version in
  `stack/schemas/upcasters.py` (`docs/howto/change-family-format.md`, `docs/howto/add-annotator.md`).
- A changed prompt gets a new file name and a new pin; editing a pinned file re-asks every document in scope
  (`docs/howto/change-prompt-pin.md`). A plan change that production runs moves its plan digest, which the cycle
  checks (`STACK_SCHED_EXPECT_PLAN_DIGEST`, `docs/DEPLOY-railway.md`).
- Test on the synthetic world (`tests/synth/world.py`, `STACK_WORKFLOWS_ROOT=tests/synth/stages`) and the fakes in
  `tests/fakes/`; no test touches the network. Experiment on a copy of a store, never on one a cycle or a build uses.
- The dataset release has a format version (`format_version` in `configs/releases/dataset/mapping.yaml`, D803). A
  change to what the export writes (a field, a type, a list value, a file) is a format change: run
  `uv run --no-sync python tools/release_format.py check`, which says the bump it needs; the new version's schemas
  are archived in the data repository (`data-repo/formats/`, written there by `tools/release_format.py archive`, never
  here) and pulled back by subtree (`docs/howto/dataset-release.md`, "The release format").
- A decision that changes behaviour gets a row in `docs/DECISIONS.md` (the next free `D<n>`, appended; a later row
  amends an earlier one, decided rows are not edited); code comments and docs cite it as `D<n>`. D0-D800 are in
  `docs/repo-history/decisions-D1-D800.md`.

## Keeping the repository publishable

- No secrets or key values anywhere, error bodies and logs included; no bucket names (write `s3://<bucket>/<prefix>`);
  no local or private filesystem paths.
- No links to link-shared documents or chats (Google Docs and Drive, chat shares, Notion, Dropbox):
  `tests/test_public_tree.py` scans every tracked text file for them (`stack.fold.dataset.SHARED_LINKS`).
- No third-party document text in the tree: eval texts and long excerpts live outside it, pinned by sha256 (D790,
  D793, D795; `tools/fetch_eval_texts.py`).
- Material removed from the tree is described in `README.md` ("What is not in this repository"); a name of it in an
  older comment or `DECISIONS.md` row is provenance, not a path to follow.
