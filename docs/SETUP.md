# Setup: from a fresh clone to a first build

Checked against the code on 2026-11-03: §5 was run offline on the synthetic world and §6's `--dry-run` on an empty
store. The real build in §6 was not run.

## 1. Install

Python 3.12 or newer (`pyproject.toml` `requires-python`) and `uv`.

```bash
uv sync --frozen --group convert-pdf --group release   # never a plain `uv sync`
```

That is the base install, the default `dev` group (pytest, xdist, mypy, jsonschema, and the `s3` group's boto3 for an
S3 store), `convert-pdf` (pymupdf, pymupdf4llm, trafilatura) and `release` (the Hugging Face client for uploads).
`--frozen` installs the lock as it is. `uv sync` is exact: a later `uv sync` without the groups uninstalls them again.
Run commands with `uv run --no-sync …` (as every doc here does) so that running never re-syncs.

- The base install includes `fetchpdf` from a pinned git commit (acquire's oa lane), so the first sync needs
  network access to GitHub.
- `convert-pdf` is what every current plan needs to *acquire*: the production plan `batch-2026-10a` and the dev and
  scratch plans convert with `pymupdf4llm-v1`. Only the earlier production plan `std-v2` pins `docling-v2`, which
  needs `--group convert` (`convert-pdf` plus docling). The convert pin hashes the installed converter versions, so
  the venv decides the pin. A venv without the tools makes `acquire` end `failed[environment]` with a line naming
  the missing tools and this remedy; a plan line may opt out with `options.allow_degraded_convert: true` (D250).
  `batch-2026-10a` (like `std-v3`) also pins `expect_pin: 625b4004…`, so another pymupdf4llm version is refused the
  same way. `uv lock --upgrade` therefore leaves them where they are: `[tool.uv] constraint-dependencies` in
  `pyproject.toml` holds pymupdf4llm (and with it pymupdf and pymupdf-layout), lxml, onnxruntime and playwright
  (D696); lift a hold deliberately, with a new `expect_pin` (or, for playwright, the Chromium install below).
- Stages other than `acquire`, the store, `verify`, the folds and the consumer queries need only the base install.
- The http lane's headless rung (stage option `headless: true`, off in every plan) needs a browser: the Playwright
  package comes with the base install (through fetchpdf), its Chromium does not. `uv run --no-sync playwright
  install --with-deps chromium` (the system libraries through sudo apt; about 650 MB in `~/.cache/ms-playwright`
  and 330 MB of packages on Debian 13), else an installed Google Chrome is used. Re-run it after a Playwright
  upgrade (each release pins one Chromium build). Without a browser the rung warns once and counts
  `http.headless.unavailable` per role in the run record (D590).

## 2. Environment and keys

```bash
cp .env.example .env             # .env is gitignored; fill in what you need
```

`.env.example` lists every variable the code reads, with what reads it. The ones a newcomer needs:

| Variable | Needed for | Read how |
|---|---|---|
| `OPENROUTER_API_KEY` | every LLM call (triage questions, screen, affiliation, the other annotators, `stack eval`) | environment, else the `STACK_ENV_FILE` file |
| `OPENAI_API_KEY` | the roles with `provider: openai` (`configs/llm.yaml` `providers.openai`, D714): technique_staged and model_roles in plan batch-2026-10a, on the OpenAI API directly | environment, else the `STACK_ENV_FILE` file |
| `ANTHROPIC_API_KEY` | the calls routed to the Anthropic API (`providers.anthropic`, D722); no production plan pins one yet | environment, else the file |
| `STACK_MAILTO` | a contact address for arXiv, OpenAlex, Crossref, Unpaywall (acquire's oa lane halts without it) | environment, else seeded from the `STACK_ENV_FILE` file at CLI start |
| `OPENALEX_API_KEY` | optional; keyed OpenAlex requests (metered, see §3); without it OpenAlex runs keyless with a warning | environment, else the file |
| `SEMANTIC_SCHOLAR_API_KEY` | optional; the S2 collector and lookup, and acquire's oa lane's S2 requests (added by its request gate, D592), off the throttled keyless pool | environment, else the file |
| `X_API_BEARER_TOKEN` | optional; `collect.x-lookup` (in no plan) | environment, else the file |
| `STACK_S3_ENDPOINT_URL`, `STACK_S3_ACCESS_KEY_ID`, `STACK_S3_SECRET_ACCESS_KEY` (`STACK_S3_DEFAULT_REGION` optional) | an S3 store, `--store s3://<bucket>/<prefix>` (D321); its blobs are in its own prefix (D360). The older names `STACK_S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `STACK_S3_REGION` are still accepted; per setting the new name wins (D726) | the new name (environment, else the file), else the old name (environment, else the file) |
| `STACK_ENV_FILE` | the path of that file, e.g. `STACK_ENV_FILE=.env` (relative to the current directory) | environment |
| `STACK_STORE`, `STACK_PLAN` | defaults for `--store` and `--plan` | environment |
| `STACK_MIRROR` | where `s3://` stores are mirrored (default `v4/mirror/` under the repository) | environment |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | optional, unset by default: an outbound proxy for every fetch (D680), read natively by urllib, `requests` (the oa lane, fetchpdf) and Chromium; for a later filtering proxy (`docs/FUTURE.md`, "Egress filtering") | environment only (not the file) |

How keys get in: the code reads a key from the environment first and then, for that one name, from the file
`STACK_ENV_FILE` names. The file is parsed, never sourced, and nothing from it enters `os.environ` but `STACK_MAILTO`
(seeded at CLI start when unset); a variable set in your shell therefore wins over the file. Do not `source .env` or
`set -a; . .env`: that exports every key to every process (D160). Plain settings that are not secrets (for example
`STACK_STORE`) can be exported normally.
`STACK_METADATA_MAILTO` (v3's name) is read before `STACK_MAILTO` by the provider transport; leave it unset.

**Which LLM provider a stage calls** (D714, D722). The default is OpenRouter (`configs/llm.yaml` `provider`); an
`llm.providers` entry (`openai`, `anthropic`) is used by a role that names it (`provider: anthropic` with the model id
at that provider, e.g. `claude-sonnet-5-5`) or by a provider-qualified model reference anywhere a model is named (a
pin's `model: "anthropic:claude-opus-5-5"`, `adjudicate_model`, `prepass_model`, `select_model`, a role's `fallbacks`).
To switch a stage, point its plan pin at such a role (`role: <r>`) or qualify its `model`; the comment above
`providers:` in `configs/llm.yaml` has both shapes. Model ids are provider-specific (OpenRouter
`anthropic/claude-sonnet-4.6` is the Anthropic API's `claude-sonnet-4-6`); a changed pin is a new config digest, so the
stage re-asks its documents. The egress gate checks the direct provider (Anthropic is not sanctioned for `shadow`
documents), batch mode sends only to OpenRouter, the model needs a `pricing` entry (a direct API reports no cost: an unpriced model's
spend is recorded at the $3 / $15 fallback), and the deploy needs the provider's key.

## 3. Spend

Every paid call reserves against its run's in-memory ledger before it is sent, capped per run (LLM: the line's
`budget.max_usd`, else `configs/llm.yaml` `budget.per_run_usd`; X: `budget.max_usd`, at most $10, `STACK_X_CAP_USD`
can only lower it; OpenAlex keyed: `max_usd`/`max_requests`, at most $10, `STACK_OPENALEX_CAP_USD` can only lower
it). Each run's spend is in its run record (`spend`: settled micro-USD per provider and model, open reservations,
in-flight and abandoned batch ids; D328, D364), written at every tick (`docs/OPERATE.md`, "Spend in run records").
There is no cap across runs; the provider keys' own credit limits are the outer bound.

One more host-wide directory, shared by every store: `~/.cache/stack/providers` (`STACK_PROVIDER_STATE`, the
providers' 429 cooldown state).

## 4. Stores

A store is a directory: `builds/` (build records, each build's run records under it), `runs/` (standalone run
records), `data/` (parquet; overrides are a family here, `docs/howto/overrides.md`), `blobs/`, `releases/`, `cache/`.
Or it is `s3://<bucket>/<prefix>`, worked on through a local mirror under `$STACK_MIRROR` (`docs/howto/s3-store.md`).
`--store` (env `STACK_STORE`) defaults to `v4/store-dev` under the repository whatever the current directory. A
clone has no store; `v4/store-dev/` (like the mirrors under `v4/mirror/`) is gitignored.

- A writing command (`build`, `run`, `release`, `overrides add`) refuses a store directory that does not exist
  unless given `--create`; `fold`, `migrate`, `acquire reopen`, `verify` and `status` refuse it outright. An `s3://`
  store's mirror directory is created on first use.
- To experiment on an existing store, copy it first, on the same filesystem so the copy is a reflink:
  `cp -a --reflink=always <store> <copy>`. Never experiment on a store a cycle or a build uses.
- There is no default plan: `run`, `release`, `fold`, the `eval` verbs `triage`, `screen`, `affiliation`,
  `model_roles`, `technique_roles` and `audit-prompts`, `diff` with `latest` and `acquire reopen` (except
  `--dry-run`) need `--plan` or `STACK_PLAN`; `build` takes the plan as its argument (a name under `configs/plans/` or a path).

## 5. The offline route

The test suite's synthetic world is a 15-document store (arXiv, OpenAlex, a LessWrong/AF crosspost, an X thread,
web pages; merged, masked, skipped and failed rows on purpose; `tests/synth/world.py`) with one `ok` build of plan
`dev-v2`. `STACK_WORKFLOWS_ROOT=tests/synth/stages` makes the runner load test stand-ins instead of the real
`collect.arxiv`, `gather`, `triage`, `acquire` and `annotate.screen` stages (they know no providers and make no calls;
`fold.*` stays real). What was run:

```bash
M=$(mktemp -d)/stack-mock
uv run --no-sync python -c "from tests.synth.world import build_world; build_world('$M')"
uv run --no-sync stack --store $M verify                  # 47 checks: 43 ok, 4 skipped[structural]
uv run --no-sync stack --store $M ls                      # each series directory and its head
export STACK_WORKFLOWS_ROOT=tests/synth/stages
uv run --no-sync stack --store $M build tests/synth/plans/synth-m5.yaml --dry-run   # what each line would pin
uv run --no-sync stack --store $M build tests/synth/plans/synth-m5.yaml             # build_id …, status ok
uv run --no-sync stack --store $M head data/corpus.build/main/   # the corpus file the build wrote (series main)
uv run --no-sync stack --store $M --plan tests/synth/plans/synth-m5.yaml \
    release --in data/corpus.build/main/ --tag try-1
uv run --no-sync stack --store $M verify                  # 47 checks: 42 ok, 1 skipped[no_gate], 4 skipped[structural]
unset STACK_WORKFLOWS_ROOT                                # echo_test is a real stage, not a stand-in
uv run --no-sync stack --store $M --plan tests/synth/plans/synth-m5.yaml \
    run echo_test --in data/documents/main/       # one stage alone (warns: not in the plan)
```

The build spends nothing. Reading the result: `docs/CONSUME.md` (run from `$M`, its entry query with the glob
`builds/*.synth-m5/build.json.zst` returns the screened rows; the install gives the Python `duckdb` package, not
the `duckdb` CLI: `docs/howto/inspect.md`).

## 6. A first real build

A real build calls the providers and OpenRouter. Start with `dev-v2-m4` (the small dev plan: short windows,
`max_rows` caps), in a store of its own; the production plan `batch-2026-10a` is a full weekly cycle
(`docs/OPERATE.md`, `docs/DEPLOY-railway.md`).

```bash
uv run --no-sync stack --store v4/store-dev build dev-v2-m4 --dry-run    # resolves; creates nothing
STACK_ENV_FILE=.env uv run --no-sync stack --store v4/store-dev build dev-v2-m4 --create
```

- Needs `OPENROUTER_API_KEY` and `STACK_MAILTO`, network access to arXiv, OpenAlex, Semantic Scholar, the forums,
  LessWrong/AF, five blog channels and the model-card pages, and the `convert-pdf` group (§1).
- Spends at most the plan's line caps per build: triage $1.50, screen $3, affiliation $1.50 (with
  `OPENALEX_API_KEY` set, also up to $0.50 each for `collect.openalex` and `collect.openalex-lookup`, their default
  `max_usd`), each run's spend in its record. The spend of a first build from an empty store has not been measured.
- `--skip 'collect.*'` leaves the collectors out (to rework what is already collected); `--max-usd
  annotate.screen=0.5` lowers one annotate line's cap. A capped stage leaves an `incomplete` build (exit 3) that
  the next build resumes (`docs/OPERATE.md`).

A store written by an older version of the code reads its older-format files upcast (D295); nothing needs
migrating first. `stack migrate upcast --family <family>` (with `--file <file_id>` for a chain-of-one family such as
`screen_evidence`) materialises them (`docs/howto/change-family-format.md`).

## 7. Tests and checks

```bash
uv run --no-sync python tools/devtest.py               # dev tier, per edit: touched paths' tests + the smoke set, -n 2
uv run --no-sync python tools/devtest.py --smoke       # the smoke set only
uv run --no-sync pytest -m "not slow" -q -n 2          # the fast tier, at a sub-task's end (~3 min)
uv run --no-sync pytest -m "e2e" -q -n 2               # an area marker: both tiers' tests of one area (table below)
uv run --no-sync tools/check_all.py --no-sync          # the gate: configs, payloads, mypy (one file), the full suite (~13 min)
uv run --no-sync tools/check_all.py --no-sync --tier fast   # the same legs with the fast tier (says it is not the gate)
uv run --no-sync python tools/check_configs.py
```

The gate's `--no-sync` runs every leg with `uv run --no-sync`, so it keeps the venv synced with the groups of §1.
There is no CI: run the gate before committing anything under `stack/`, `workflows/` or `configs/`.

**Three tiers** (D490, D729). *Dev*: `tools/devtest.py` maps the touched paths (`git diff --name-only HEAD`
plus untracked files; `--base REF` adds a branch's commits; or pass paths) to test files through
`tests/modules.toml`, adds every touched `tests/test_*.py` and the smoke set `tests/smoke.txt`, and runs them with
`-m "not slow and not tools" -n 2`; `--touched` drops the smoke set, `--with-slow` adds each module's heavy files,
`--list` prints the selection, `--fast-budget S` reports tests slower than S seconds, anything after `--` goes to
pytest. *Fast*: `-m "not slow" -n 2`. *Full*: everything (`tools/check_all.py`; workers default to
min(6, MemAvailable / 1.5 GB), at least 2, about 1 GB per worker; `-n N` or `$STACK_TEST_WORKERS` override; more than
6 gains nothing). Never `-n auto` while an e2e or a build runs on the machine.

**What is `slow`.** `slow` is the only tier marker. A test is `slow` when it takes over ~1.5 s at `-n 2`, unless it is its family's representative in the fast tier (the fast tier keeps one case of each heavy path:
e.g. `FAST_IDS` in `tests/test_checks_fixtures.py`, `FAST_BATCHED` in `tests/test_checks_rows.py`,
`_sibling_fast` in `tests/test_verify_projected.py`), and crash/restart subprocess tests and scenario worlds are
`slow` whatever their time. Most of a slow test's cost is a full verify (every check over a store, ~5-7 s on a
fixture store) or whole builds (each runs the structural verify at start and the three parts at finish).

**Area markers** (orthogonal to the tier: `-m e2e` runs the area's fast and slow tests; `-m "slow and checks"` its slow
ones). Set per file (`pytestmark`); run the area after touching its code:

| marker | what the tests do | run it after touching |
|---|---|---|
| `e2e` | whole builds, runner chains, scenario worlds (`run_build`, passes, drain, crash/restart, the CLI over a store) | `stack/build.py`, `plan.py`, `stage_runner.py`, `runs.py`, `store/job.py`, `stack/cli/`, the build-finish verify, a stage's runner glue |
| `checks` | the whole check suite, or a check against its reference implementation, over the fixture stores of `tests/fixtures/checks` | `stack/checks/`, `stack/store/chain.py`, `parquet.py`, `layout.py`, `stack/schemas/`, `tests/fixtures/checks/` |
| `evalsets` | the frozen eval sets under `evals/`: byte-for-byte freezes, input checks over the real items | `evals/`, `stack/evals/`, an annotator's prompt or menu rendering, a taxonomy file a set pins |
| `tools` | one-off tools outside `stack/` and `workflows/` | `tools/` (the dev tier leaves these out) |

The area markers do not replace `tests/modules.toml`: its `slow` lists name the heavy files of each module for
`devtest.py --with-slow`. A new module under `stack/` or `workflows/` needs a line in `tests/modules.toml`
(`tests/test_devtest.py` fails otherwise); a new heavy test gets `@pytest.mark.slow` (a parametrized family:
`pytest.param(..., marks=pytest.mark.slow)` on all but a representative case), a tool's test `@pytest.mark.tools`,
and a new test file in one of the areas above its `pytestmark`. A fixture store that tests only read can be
written once per module and shared read-only (`tests.fixtures.checks.shared_stores`, `read_only`): a test that
writes into it fails with a permission error instead of corrupting its neighbours.

Some tests spawn `uv run --no-sync stack`, so they need the synced `.venv` of this checkout.

**Eval-set whole texts** (D790). The whole texts of the older frozen eval sets (`evals/<task>/<set>/texts/`) are
third-party documents and are not in the repository. Readers (the eval harness, the sets' own scripts, the tests)
look in a set's own `texts/` (a fresh freeze; gitignored) and then in the eval-text cache:
`$STACK_EVAL_TEXTS_DIR`, else `$XDG_CACHE_HOME/stack/eval-texts`, else `~/.cache/stack/eval-texts`, laid out
`<task>/<set_id>/<item_id>.txt.gz`. A test that needs a missing text is skipped with an `EvalTextsMissing` warning;
`stack eval` on such a set refuses before any call, naming the missing text.

**Without the archive.** The texts, and the archived eval files below, are in archives in the maintainers' private
storage (the store's bucket). Outside the maintainers, `tools/fetch_eval_texts.py` cannot read them, and:

- the suite still runs: the `evalsets` tests that need a missing text or file skip (expected), and `stack eval` on
  such a set refuses; `uv run --no-sync pytest -m "not slow and not evalsets" -q -n 2` is the selection that needs
  no eval texts and no network;
- each item in the tree pins its document's text by `doc_id`, title and the sha256 of the frozen text
  (`input.full_text_digest`, or the top-level `text_digest` of the older sets). The items carry no URL: a document
  in a released dataset has its `canonical_url` there (`docs/CONSUME.md`), from which the text can be fetched again
  and checked against the pinned digest. It matches only if the source still serves the same content and the
  conversion is the set's; the eight sets whose `items.jsonl` is archived (below) cannot be rebuilt this way;
- given a copy (a `tar.zst` of an `evals/` tree holding the texts and the archived files), `--from-tar FILE` fills
  the cache from it with the same checks.

With access, fill the cache (read-only from the archive in the store's bucket; every text checked against its
item's frozen digest; the bucket from `$STACK_STORE` or `$STACK_S3_BASE_URI`, credentials as for the store):

```bash
STACK_ENV_FILE=.env uv run --no-sync python tools/fetch_eval_texts.py      # into the cache
uv run --no-sync python tools/fetch_eval_texts.py --from-tar evals.tar.zst # from a copy of the archive
uv run --no-sync python tools/fetch_eval_texts.py --check                  # verify the cache, no network
```

The same tool fetches the eval files with long excerpts that are archived outside the repository (D793, D795:
`evals/archived.sha256` pins them; they land at `<cache>/<path under evals/>`), among them the `items.jsonl` of
eight triage, screen and affiliation sets, which `stack eval` and the tests read from the cache.
