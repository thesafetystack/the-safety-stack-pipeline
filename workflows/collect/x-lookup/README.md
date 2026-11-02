# `collect.x-lookup` — X posts by id, a self-thread as one document

Code: `stack/collect/xapi.py` (the module docstring is the contract); this directory's `main.py` is the stage entry.
Tests: `tests/test_collect_x.py` (fake HTTP with the response shapes the live API returned; no network).

    STACK_ENV_FILE=.env uv run --no-sync stack run collect.x-lookup --plan <plan> --in data/sources.x-lookup/<series>/ \
      --in data/coverage.x-lookup/<series>/ --in data/documents/<series>/

Plan line (required: `miss_cap`, `budget.max_usd`):

    - {stage: collect.x-lookup, prior: [sources, coverage], keys: [x_post], miss_cap: 3, budget: {max_usd: 5},
       options: {threads: true, max_thread_posts: 25, users: false, usage_check: true}}

**Implemented:** batch post lookup (`GET /2/tweets`, ≤ 100 ids); **a self-thread is one document** (round 5): the
looked-up posts that start a conversation or answer their own author are grouped by `(conversation_id, author)`,
searched once per group, and written as one row `external_id = thread:<conversation_id>:<author id>` whose keys are
the `x_post` of every member, so documents linking any member merge into it at gather and the thread text is rendered
once, in order (other authors' replies left out); a member is never looked up again; a newly linked post of a known
thread re-searches it and extends the same row (same source, same document); a self-reply inside someone else's
conversation stays a post of its own (`thread_status: foreign`). Also: quoted posts as their own rows (unless they
are thread members), reposts collapsed onto the original, per-id misses (`miss_means: withdrawn`), the run's X ledger
(per-run cap $10, lowered by `STACK_X_CAP_USD`; spend in the run record) with a reservation before every paid request, the per-run
`budget.max_usd`, retries of failed or budget-cut thread searches in later runs (the post waits as a `pending` post
row meanwhile), the project post-read counter before and after a run. Decisions: `docs/DECISIONS.md` D213 (X) and
D243-D247 (X2).

**Endpoints and tier.** Threads whose root is at most 6.5 days old use recent search (`GET /2/tweets/search/recent`,
7-day window); **older threads need full-archive search (`GET /2/tweets/search/all`)**. The project's pay-per-use
tier allows it: live on 2026-10-12, `search/all` answered 200 with `x-rate-limit-limit` 300 per 15 min (RUNLOG (X)
2026-10-12 21:10, "Account tier"), and again on 2026-10-13 (RUNLOG (X2), D247). The thread query names the
author's handle in `to:`: full-archive search ignores `to:<numeric id>` (D247). If a tier without full-archive
access is ever used, an old thread's search fails (`provider_auth` or `query_invalid`), the post is written as a
`pending` post row and the search counts against `miss_cap`.

**Rows before round 5** (one post row per looked-up post with the thread folded into its record; only in the
gitignored scratch stores `v4/store-x-scratch/` and `v4/store-links-scratch/`) are not migrated: they stay done, and
a thread row written later merges with them on `x_post` (the pointer takes the thread's newer text).

**Not implemented / not wired:**
- Relayed link rows (proposal §4.9): `xapi.relayed_urls(record)` lists the URLs; writing them as link-channel rows
  is not wired, though the shared link code it waited for (`collect.links`, D222-D225) has landed.
- **Not in any plan** (D243: importing X threads is deferred); ready to add as the plan line above.
  Keys come from the `documents` head, where `collect.links` writes `x_post` targets (channel
  `links_social`), or from `options.ids` in operator runs.
- A known thread is not polled: it grows only when a post of it that is not yet a member gets linked. A post
  that was already a document of its own before its thread was written (a quoted post, a `pending` post row, a
  pre-round-5 row) stays a member of the merged document with its single-post text (not current).
- In someone else's conversation the author's own follow-ups are not gathered (each linked post is its own
  document).
- Prices are the public list prices ($0.005 per post, $0.010 per user); the API reports no money. The ledger
  counts every distinct post returned (`includes` too), which is above what the project counter showed live
  (`data` posts only, 24 h de-duplication).
