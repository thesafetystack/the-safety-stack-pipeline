# configs/

The versioned YAML registry the code reads, the plans and the release configs. `tools/check_configs.py` validates
each top-level `*.yaml` against a schema in `spec/schemas/config/` (mostly of the same name; `taxonomy-v0.6.yaml`
against `taxonomy.schema.json`) and the plans, not `fetchpdf_ladder.json` or the subdirectories' other files; a
file's header says what reads it.
Secrets, personal identifiers and machine paths never live here (`.env.example`).

| Path | What it is |
|---|---|
| `channels.yaml` | The configured origins of sources: providers, forums, blogs, card pages, feeds, the concept groups and keyword families the collectors search for. |
| `llm.yaml` | LLM providers, roles (model and parameters per call), egress classes and budgets. |
| `models.yaml` | The models catalogue `annotate.model_roles` reads (a symlink to models 1.1.0 in `data-repo/`; `taxonomy/README.md`). |
| `registry.yaml`, `taxonomy-v0.6.yaml` | The subject-model and organisation registry with its alias regexes, and the registry's taxonomy pin (a v0.6 copy, not the production technique catalogue). |
| `enums.yaml` | The controlled vocabularies still read (older screen prompts' menus, the doc_type domain). |
| `policies.yaml` | Quote anchoring acceptance (`stack/anchor.py`). |
| `fetchpdf_ladder.json` | The PDF fetch ladder of acquire's open-access lane. |
| `plans/` | The plans: stages, pins, caps and options per build (`plans/README.md`; production: `batch-2026-10a`). |
| `overrides/` | Prepared override files, imported by an operator with `stack overrides add` (`docs/howto/overrides.md`); no stage reads them from here. |
| `rosters/researchers.yaml` | The researcher roster: researchers' names and their OpenAlex author ids, read by `collect.arxiv_sweep`'s author family (names) and `collect.openalex`'s author-id family (ids). |
| `releases/dataset-public.yaml`, `releases/dataset-internal.yaml` | The two dataset variants `stack export` writes and the scheduled cycle uploads (Hugging Face repository, judge eval set). |
| `releases/dataset/` | What the dataset export reads: `mapping.yaml` (every released field and how it is derived), `vocab.yaml` (a sentence per value word), `overrides-public.yaml` (the public projection of the overrides), the card and FORMAT templates `README.card.md` and `FORMAT.head.md`, and the dataset `LICENSE`. |
| `releases/public-v1.yaml`, `releases/internal-v1.yaml` | Release configs of `stack fold release` / `stack release` (the store-side release fold; `stack/fold/release.py`). |

A change to a file a plan pins (a prompt, a taxonomy or vocabulary) changes the pinned digests and re-asks the
documents in scope: `docs/howto/change-prompt-pin.md`, `docs/OPERATE.md`.
