# the-safety-stack-pipeline

The pipeline that builds **The Safety Stack** (https://thesafetystack.ai/), a dataset of AI-safety techniques and
the public evidence of their deployment at frontier labs: which techniques (evaluations, red-teaming,
interpretability, control, …) the labs use, read off papers, posts, model cards and blogs. This repository collects
those documents, screens them for relevance, extracts what they say and releases the dataset
(https://huggingface.co/datasets/thesafetystack/the-safety-stack). The taxonomies of techniques, models and
organisations it pins live in the data repository https://github.com/thesafetystack/the-safety-stack, carried here
as the `data-repo/` subtree.

**Status:** the v4 pipeline is the production pipeline; its cycle runs weekly on Railway since 2026-10-31
(`docs/DEPLOY-railway.md`).

The **v4 pipeline**: the `stack` CLI builds a plan (`configs/plans/*.yaml`) into a corpus over a
store of immutable parquet files and content-addressed blobs, verifies it (47 checks), and releases it; consumers
read the corpus files with duckdb. There is no database. A store is a directory, or the local mirror of an S3 store
(`--store s3://<bucket>/<prefix>`) whose blobs are flat `blobs/<sha256>` in its own prefix (D360).

## Setup

Python 3.12 or newer (`pyproject.toml`) and `uv`.

```bash
uv sync                          # base dependencies + the dev group (pytest, mypy, boto3 for S3 stores)
uv sync --group convert-pdf      # pymupdf, pymupdf4llm, trafilatura: needed by every acquire run of the
                                 #   dev-v2-m4 and std-v3 plans (they convert with pymupdf4llm-v1)
cp .env.example .env             # fill in the keys you need; the CLI reads it via STACK_ENV_FILE=.env
uv run --no-sync stack --help
```

`uv sync` without `--group convert-pdf` removes the converter tools again (uv syncs the venv exactly), and every
command here uses `uv run --no-sync` so that running never re-syncs. Without the tools, `acquire` refuses to run
(`failed[environment]`) rather than converting at another pin. `--group convert` adds docling (for `docling-v2`,
which `std-v2` pins; it pulls torch, several GB). Details, every environment variable and the spend caps: `docs/SETUP.md`.

## First run, offline

A clone has no store (`v4/store-dev/` is gitignored and local to the machine that built it). The synthetic world from
the tests gives one with no network and no LLM calls:

```bash
M=$(mktemp -d)/stack-mock
uv run --no-sync python -c "from tests.synth.world import build_world; build_world('$M')"
uv run --no-sync stack --store $M verify                     # 47 checks: 43 ok, 4 skipped[structural]
STACK_WORKFLOWS_ROOT=tests/synth/stages \
  uv run --no-sync stack --store $M build tests/synth/plans/synth-m5.yaml   # stand-in stages
uv run --no-sync stack --store $M head data/corpus.build/main/           # the corpus file a consumer reads
```

`STACK_WORKFLOWS_ROOT` swaps the real stages for the test stand-ins in `tests/synth/stages/`; `build` takes a plan
name under `configs/plans/` or a path. A real build (`stack build dev-v2-m4`) calls the providers and OpenRouter:
it needs `OPENROUTER_API_KEY` and `STACK_MAILTO` in `.env`, a store (`--create` for a new one), and spends up to
the plan's caps (dev-v2-m4: triage $1.50, screen $3, affiliation $1.50 per build). See `docs/SETUP.md` §6.

## Where to read next

| Doc | For |
|---|---|
| `docs/README.md` | The map of the docs, how they cite (D-numbers, check ids), and the words that mean several things (`tier`, `skipped`, `corpus`, `build`). |
| `docs/CONCEPTS.md` | The concepts the other docs assume: store, families, series and chains, runs and builds, identity, roles and snapshots, pins and digests, folds. |
| `docs/SETUP.md` | Install, environment variables, keys, spend caps, the offline route, the first real build. |
| `docs/OPERATE.md` | Running builds and stages: plans, series and chains, passes, `incomplete` builds, the store and jobs, spend, verify, family format changes. |
| `docs/CONSUME.md` | Reading a build or release: the files, the duckdb macros, annotation records, text quality, the site query. |
| `docs/howto/` | Task pages: inspect what a build fetched; why a document is missing; the full run; S3 stores; containers; concurrency; overrides; duplicates and URL rules; add a collector, a feed, an annotator; change a prompt pin or a family format; the dataset release. |
| `workflows/README.md` | How stages are laid out and how to write one; the prompts in the tree and which plan pins each. |
| `configs/plans/README.md` | The plan file, field by field, and the plans in the tree: production, dev, scratch. |
| `docs/DESIGN.md` | The design (families, blocks, stage contracts, checks), with "Stale" notes where the code moved on. Where it disagrees with the code, the code wins. |
| `docs/DECISIONS.md` | Every implementation decision (`D<n>`). A run's record is its run record in the store and, for a scheduled cycle, the cycle's summary on the store's ops prefix (`docs/DEPLOY-railway.md`). |
| `docs/FUTURE.md` | Postponed work. |
| `docs/development-history.md` | How the pipeline was developed (June to November 2026) and who did the work, including work no longer in the tree. |

## Layout

| Dir | What it is |
|---|---|
| `stack/` | The engine and the only installed package (`workflows/` is imported from the repo root, which the editable install puts on the path): store (`stack/store/`), stage runner, plans, builds, folds, checks, the LLM client and its per-run ledger, collect engines, the CLI (`stack/cli/`). |
| `workflows/` | The stages, phase-first (`collect/<name>/`, `gather/`, `triage/`, `acquire/`, `annotate/{screen,translation,affiliation,affiliation_orgs,technique_staged,model_roles,technique_roles}/`), each a `main.py` loaded by path, prompts co-located. `fetch/metadata/` is not a stage: it is the shared code of `collect.openalex-lookup` and `collect.s2-lookup`. See `workflows/README.md`. |
| `configs/` | The versioned YAML registry (enums, model/org registry, taxonomy pin, channels, llm, policies; the model vocabulary `models.yaml`), the plans (`configs/plans/`), the release configs (`configs/releases/`), the roster. |
| `evals/` | Frozen eval sets, rubrics and calibration for `stack eval` (screen, triage, affiliation, model_roles, technique_roles, technique_staged). |
| `spec/` | Only the machine-readable artifacts still read: config schemas, the screen verdict and doc_class payload schemas with their examples, `types/interfaces.py`; see `spec/README.md`. The v3 prose spec and the other v3 schemas were in `attic/v3/spec/` (removed; see below). |
| `tests/`, `tools/` | The suite (no database, no network; three tiers in `docs/SETUP.md` §7) and the tools: the dev tier `tools/devtest.py` (with `tests/smoke.txt`, `tests/modules.toml`), the gate `tools/check_all.py`, `tools/check_configs.py`, `tools/llm_smoke.py`, and the operator probes `tools/probe_collect.py` (one live collect query, nothing written) and `tools/probe_filters.py`; `tools/resolve_openalex_authors.py` (roster OpenAlex author ids by a written rule, metered; report `docs/design/openalex-author-ids.md`). |
| `docs/` | The documentation (`docs/README.md` is its map): concepts, setup, operate, consume, the Railway deploy, how-tos (`howto/`), design, decisions, future work; `design/` (design notes: URL normalisation, link crawling, collection yield, OpenAlex author ids, the catalogue envelope, short-field translation, the technique-quality round of 2026-10, the channels' license classes), `scope/` (scope notes: the Shallow Review and its criteria). The v3-era plans and status were in `attic/v3/docs/history/` (removed; see below). |
| `taxonomy/` | `taxonomy-v0.8.yaml` (Jake's catalogue, 292 entries, 10 facets; read by `annotate.technique_roles`; not the pin: `TaxonomyConfig` rejects its `aliases`), the v0.9 catalogues of `annotate.technique_staged` and the production `techniques-1.1.0.yaml` (D738), the organisation vocabularies (production `orgs-0.2.2.yaml`, D778) and the model vocabularies; see `taxonomy/README.md`. The registry's taxonomy pin is the copy `configs/taxonomy-v0.6.yaml` (`configs/registry.yaml`). |
| `data-repo/` | The data repository thesafetystack/the-safety-stack as a git subtree (squashed): the taxonomy releases the pipeline pins (`taxonomies/`, read through symlinks in `taxonomy/` and `configs/models.yaml`), their schemas and validator. Update and push-back commands: `taxonomy/README.md`. Do not edit taxonomies here. |
| `v4/` | Not tracked: the default place of local stores (`v4/store-dev/`, the S3 mirrors `v4/mirror/`, eval runs `v4/evals-store/`; all gitignored). The v4 docs that lived here moved to `docs/` on 2026-11-01 (D791). |

## Removed, and where it lives

This repository starts from a cleaned tree (2026-11); the project's working history stays private. Not included:
the earlier pipeline generations and prototypes (the v1 pipeline, the `fabled` prototype, v3, the v4 M1-M5 plan,
implementation boards and design mocks, the frozen demo apps, once under `attic/`), `dan_experiment/`, the
taxonomy's history and source corpus (`taxonomy/README.md`), the run log of 2026-10 (`RUNLOG.md`; the decisions it
backed are in `docs/DECISIONS.md`), the dated status `HISTORY.md`, a generated pipeline diagram, and third-party
text: the eval sets' whole texts (D790; `tools/fetch_eval_texts.py` fetches them into a local cache,
`docs/SETUP.md` §7) and eval files with long excerpts, among them eight sets' items (D793, D795; the same tool). The docs that lived in `v4/` are in `docs/` (D791). Comments, docstrings and older
`DECISIONS.md` rows still name some of this material as provenance (an `attic/` path, a `RUNLOG (X)` entry, a
`v4/<doc>` path, a commit id); the sentence around such a name states what matters. Commit ids cited in
`docs/DECISIONS.md` and in data and run records refer to the archived pre-publication repository
(`docs/development-history.md`); they do not resolve in this one. The one live read of the
removed material, the v1 search config the OpenAlex parity tests compare against, is
`tests/fixtures/v1-search_config.yaml`.

The eval texts and the archived eval items are in the maintainers' private storage, so a clone cannot fetch them
with `tools/fetch_eval_texts.py`. Without them the CI subset runs in full
(`uv run --no-sync pytest -m "not slow and not evalsets" -q -n 2`) and the eval tests and runs that need them skip
or refuse; the items in the tree pin each text by document id and sha256, and `--from-tar` takes a copy of the
archive (`docs/SETUP.md` §7).

## Licence

The code is under the MIT licence (`LICENSE`). The released dataset and the taxonomies carry their own licences:
`configs/releases/dataset/LICENSE` (shipped with each release) and `data-repo/LICENSE`.

The optional `convert-pdf` dependency group installs PyMuPDF (`pymupdf`, `pymupdf4llm`), which is licensed under
AGPL-3.0 (or Artifex's commercial licence); an environment or container image built with that group, the
`Dockerfile`'s image among them, is subject to the AGPL's terms. `pymupdf4llm` also pulls in `pymupdf-layout`,
whose package metadata gives PolyForm Noncommercial (or Artifex's commercial licence).

## Citation

There is no `CITATION.cff` for this repository. Cite the dataset as its card on Hugging Face says ("Cite as", with
the release id; the card's template is `configs/releases/dataset/README.card.md`), and the taxonomies by
`data-repo/CITATION.cff`.

## Contact

info@thesafetystack.ai
