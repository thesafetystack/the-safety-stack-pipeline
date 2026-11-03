# `collect.cards` — per-lab model-card / policy-doc watcher

The `kind: watch` collector (`docs/DESIGN.md` §5.6, the url row). Per configured `*_cards` channel it reads **only** the
channel's listing/seed surface(s), parses the listing in code (`stack/collect/listing.py`, shared
with `collect.blogs`), applies the include/exclude/policy regexes ported from the earlier pipeline, and writes one
`sources` row per listed item on its own chain `sources.cards`. **No item content is fetched
here** (no in-band entry) — acquisition is `acquire`'s job. The module docstring of `main.py` is
the full contract; this is the summary.

## What it writes

- **`sources.cards`** — one row per listed item that is new or whose listing record changed
  (`skip_unchanged`). `external_id` is the earlier pipeline's org-slug doc id body (`<org>-<slug>`,
  `systemcard:`/`policy:` prefix stripped; continuity with imported documents). `identity.canonical_url` = the
  item URL in its fetch form (`stack.urls.fetch_url`); the one key is `url` = `stack.urls.url_key` of it. `metadata`: `title` (anchor text,
  else the slug), `published_at` (the listing's resolved publication date, `stack/lookup/
  card_dates.py` evidence), `doc_type` ∈ {system_card, model_card, framework_policy}.
- **`coverage.cards`** — one row per listing surface scanned, `query` = the surface key; window
  `[now, now]` (a snapshot), except github-org: `[prior watermark, new watermark]` over repo
  `created_at`. `ok` only when the full listing was read; `partial` when GitHub truncated a tree
  or the new-source cap bit; `failed_*` when a listing could not be read (a block page is never
  an empty `ok`).

Publication dates the listing does not give are looked up later by `collect.card-dates-lookup`
(`stack/lookup/card_date_backfill.py`), which reads the `documents` head and the cards' stored
content and official pages.

## Discovery methods (per surface, ported from the earlier pipeline's watch configuration)

`html-index` · `sitemap` · `github-tree` · `github-org` · `pinned-pdf`. The per-lab selectors
live in `WATCH_SPECS` in `main.py` (keyed by channel id), **not** in `channels.yaml`
(`ChannelDef` forbids extra fields). See the `anthropic_cards` `[FLAG]` in `channels.yaml`.

## Options (plan stage line `options:`; the env var wins when set)

| option | env | meaning |
|-----|-----|---------|
| `max_urls` | `STACK_CARDS_LIMIT` | cap on NEW sources this run; `0` = none. A capped surface is `partial` and does not advance a github-org watermark. |
| `full` | `STACK_CARDS_FULL` | github-org watchers sweep repo history (default: bootstrap the watermark on first contact). |
| `only` | `STACK_CARDS_ONLY` | channel ids (list, or comma-separated in the env). |
| `fraction`, `max_queries` | `STACK_COLLECT_FRACTION`, `STACK_COLLECT_MAX_QUERIES` | scale knobs over the surface list. |
| `relist_after_h` | — | hours before a surface whose last attempt was `ok` (or a deterministic `partial`) is listed again in a later pass (default 12; `0` = always; D226). |
| — | `STACK_CARDS_FIXTURES` | offline replay dir (`responses.json`) instead of the network (bypasses the transport). |

## Run

```
export STACK_PLAN=configs/plans/<plan>.yaml            # no default plan; the line's options come from it
stack run collect.cards --in data/sources.cards/<series>/ --in data/coverage.cards/<series>/ --part
STACK_CARDS_ONLY=anthropic_cards STACK_CARDS_LIMIT=3 stack run collect.cards \
    --in data/sources.cards/<series>/ --in data/coverage.cards/<series>/ --part
```

The two `--in` are the stage's priors and say where its outputs go. Without them the run has no destination unless
`--series` is given, and then writes a series' first files (refused into a non-empty directory without `--restart`).
`stack build <plan> --dry-run` prints a build line's flags.

## Divergences from the earlier pipeline (deliberate)

- **No item-content / landing-page fetch** — title/date come from the LISTING only.
- **No github per-file commit-date call** — github-tree items carry no `published_at`.
- **No headless render** — a listing that hard-requires JS reads as `failed_*` or an empty html-index.
- **Dropped (no A.5 column):** `self_published` (the channel's `self_published_org`), the
  `discovery` provenance blob, the `card_date_evidence` list (its resolved date is `published_at`).
- **Listing reads are paced** by the harness transport at the channels' `rate_rps` (0.5/s per
  host); the earlier pipeline read them unpaced. `respect_robots` is not applied to listing reads (as before).
