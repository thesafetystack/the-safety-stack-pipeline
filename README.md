# the-safety-stack-pipeline

The pipeline that builds **The Safety Stack** dataset (https://thesafetystack.ai/): public documents on technical AI
safety (papers, preprints, forum posts, lab blogs, model and system cards), each annotated by a language model with
the safety techniques it engages with and in what role, the AI models it names, its document class and, for papers,
its authors' affiliations. The pipeline collects candidate documents, triages and fetches them, screens their texts
for relevance, annotates them and releases the dataset.

| You want to | Go to |
|---|---|
| use the dataset | the dataset card on Hugging Face, https://huggingface.co/datasets/thesafetystack/the-safety-stack; every release ships its own `FORMAT.md` and `VOCABULARY.md` |
| report a wrong annotation, a missing work, a takedown, or suggest a taxonomy change | the issue forms of the data repository, https://github.com/thesafetystack/the-safety-stack, or info@thesafetystack.ai |
| run or change the pipeline | Setup below, then `docs/README.md` |
| work in this repository as a coding agent | `AGENTS.md` |

## Status

The pipeline runs in production: a scheduled cycle on Railway every Sunday at 00:00 UTC (`tools/scheduled_run.py
cycle`, `docs/DEPLOY-railway.md`) collects, builds, verifies and, when the build is releasable, exports two variants
of the dataset and uploads each to its Hugging Face dataset repository (`configs/releases/dataset-*.yaml`):

- **public**: `thesafetystack/the-safety-stack`; no document texts, abstracts withheld, quotes capped;
- **internal**: `thesafetystack/the-safety-stack-private`; abstracts, uncapped quotes and full texts, private to the
  maintainers and not distributed.

Each release is dated (`release-<YYYY-MM-DD>`, a tag of the Hugging Face repository). Branches: `main` is
development; `deploy/railway` is what production runs, fast-forwarded to reviewed commits of `main` by hand.

## How it works

The `stack` CLI builds a plan (`configs/plans/*.yaml`; production: `batch-2026-10a`) into a corpus over a store of
immutable parquet files and content-addressed blobs, verifies it (47 checks) and exports a release; consumers read
the corpus files with duckdb. There is no database. A store is a directory, or the local mirror of an S3-compatible
store (`--store s3://<bucket>/<prefix>`). The stages are `workflows/<phase>/<name>/main.py`, run in the order
collect, gather, triage, acquire, the corpus fold, annotate, the build and release folds. `docs/CONCEPTS.md` explains
the vocabulary; `docs/DESIGN.md` the design ("v4" is the working name of this file-based design).

## Setup

Python 3.12 or newer and `uv`.

```bash
uv sync --frozen --group convert-pdf --group release   # never a plain `uv sync`: it removes the groups again
cp .env.example .env              # fill in the keys you need; the CLI reads it via STACK_ENV_FILE=.env
uv run --no-sync stack --help
```

`convert-pdf` (pymupdf, pymupdf4llm, trafilatura) is needed by every acquire run of the current plans; without it
`acquire` refuses to run (`failed[environment]`) rather than convert at another pin. `release` adds the Hugging Face
client for uploads. `uv sync` installs exactly the groups it is given, so name them every time, and run commands with
`uv run --no-sync` so that running never re-syncs. Every environment variable, the spend caps and the test tiers:
`docs/SETUP.md`.

## First run, offline

A clone has no store. The synthetic world from the tests gives one with no network and no LLM calls:

```bash
M=$(mktemp -d)/stack-mock
uv run --no-sync python -c "from tests.synth.world import build_world; build_world('$M')"
uv run --no-sync stack --store $M verify                     # 47 checks: 43 ok, 4 skipped[structural]
STACK_WORKFLOWS_ROOT=tests/synth/stages \
  uv run --no-sync stack --store $M build tests/synth/plans/synth-m5.yaml   # stand-in stages
uv run --no-sync stack --store $M head data/corpus.build/main/           # the corpus file a consumer reads
```

`STACK_WORKFLOWS_ROOT` swaps the real stages for the test stand-ins in `tests/synth/stages/`; `build` takes a plan
name under `configs/plans/` or a path. A real build calls the data providers and the LLM APIs, needs keys and a
contact address in `.env` and spends up to the plan's caps: `docs/SETUP.md` §6.

Checks: `uv run --no-sync tools/check_all.py --no-sync` is the gate (config checks, mypy on one file, the full
suite); there is no CI. Without the eval-text cache (below) the eval-set tests skip.

## Where to read next

| Doc | For |
|---|---|
| `docs/README.md` | The map of the docs, how they cite (D-numbers, check ids), and words that mean several things. |
| `docs/CONCEPTS.md` | Store, families, series and chains, runs and builds, identity, pins and digests, folds. |
| `docs/SETUP.md`, `docs/OPERATE.md` | Install, keys, spend caps, tests; running builds and stages, the store, verify. |
| `docs/CONSUME.md` | Reading a build or a release: the files, the duckdb macros, annotation records. |
| `docs/DEPLOY-railway.md` | The production cycle: what it does, the image, the services and variables, operating it. |
| `docs/howto/` | Task pages: inspect a build, why a document is missing, S3 stores, overrides, add a collector or an annotator, change a prompt pin or a family format, the dataset release. |
| `workflows/README.md`, `configs/plans/README.md` | How a stage is written and which prompts the plans pin; the plan file and the plans in the tree. |
| `docs/DESIGN.md`, `docs/DECISIONS.md` | The design, and every implementation decision (`D<n>`). Where the design and the code disagree, the code wins. |

## Layout

| Dir | What it is |
|---|---|
| `stack/` | The engine and the only installed package: store, stage runner, plans, builds, folds, checks, the LLM client, collect engines, the CLI (`stack/cli/`). |
| `workflows/` | The stages, phase-first (`collect/<name>/`, `gather/`, `triage/`, `acquire/`, `annotate/<name>/`), each a `main.py` loaded by path, prompts beside it. |
| `configs/` | The YAML registry (enums, channels, LLM roles, policies, the model vocabulary), the plans, the release configs and the dataset card templates (`configs/releases/`). |
| `taxonomy/`, `data-repo/` | The taxonomies the pipeline reads (below and `taxonomy/README.md`). |
| `evals/` | Frozen eval sets and the judge evaluation behind each release's quality table (`stack eval`). |
| `spec/` | Config and payload schemas still read by the code (`spec/README.md`). |
| `tests/`, `tools/` | The suite (no database, no network) and the tools: the check gate, the dev-test runner, the scheduled cycle, operator probes. |
| `docs/` | The documentation (`docs/README.md`). |

The default places of local stores (`v4/store-dev/`, the S3 mirrors `v4/mirror/`, eval runs `v4/evals-store/`)
are gitignored.

## The data repository

The techniques taxonomy, the models catalogue and the organisation vocabulary are released in the data repository
https://github.com/thesafetystack/the-safety-stack, with their own version tags (`techniques-1.1.0`, `models-1.1.0`,
`orgs-0.2.2`, ...). This repository carries it as a squashed git subtree at `data-repo/`: the files the pipeline reads
are symlinks from `taxonomy/` (and `configs/models.yaml`) into `data-repo/taxonomies/`, pinned by tag and sha256 in
`taxonomy/vendored.lock.yaml`. Taxonomy changes are made in the data repository and pulled here with `git subtree
pull` (`taxonomy/README.md`). `git rev-parse HEAD:data-repo` here equals `git rev-parse <commit>^{tree}` there for the
data-repository commit the subtree holds.

## What is not in this repository

This repository starts from a cleaned tree (2026-11): earlier pipeline generations and prototypes, the taxonomy's
working history, run logs and status notes are not included. Comments, docstrings and older `docs/DECISIONS.md` rows
still name some of that material (an `attic/` path, a `RUNLOG` entry, a `v4/<doc>` path, a commit id that does not
resolve here); the sentence around such a name states what matters. The one live read of the removed material, the
v1 search config the OpenAlex parity tests compare against, is `tests/fixtures/v1-search_config.yaml`.

Third-party text is not in the tree either: the frozen eval sets' document texts and the eval files with long
excerpts are kept in the maintainers' private storage (D790, D793, D795), and the items pin each text by document id
and sha256. Without them the suite still runs: the eval tests and eval runs that need the texts skip or refuse; `tools/fetch_eval_texts.py --from-tar` takes a copy of
the archive (`docs/SETUP.md` §7).

## Licence

The code is under the MIT licence (`LICENSE`). The released dataset and the taxonomies carry their own licences:
`configs/releases/dataset/LICENSE` (shipped with each release) and `data-repo/LICENSE`.

The optional `convert-pdf` dependency group installs PyMuPDF (`pymupdf`, `pymupdf4llm`), which is licensed under
AGPL-3.0 (or Artifex's commercial licence); an environment or container image built with that group, the
`Dockerfile`'s image among them, is subject to the AGPL's terms. `pymupdf4llm` also pulls in `pymupdf-layout`,
whose package metadata gives PolyForm Noncommercial (or Artifex's commercial licence).

## Citation and contact

Cite the dataset as its card on Hugging Face says ("Cite as", with the release id), and the taxonomies by
`data-repo/CITATION.cff`. Contact: info@thesafetystack.ai.
