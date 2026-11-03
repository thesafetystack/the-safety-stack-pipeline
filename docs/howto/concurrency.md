# Concurrency options

Concurrency is set per stage line in a plan's `options:` (D271). Only these stages read a worker option; everything
else runs sequentially. An option a stage does not read is refused, naming the closest known one (D434, D472):
`stack build` ends `stage_refused` before its first run, `stack run` refuses before any run record. Whether an
option enters the line's config digest is the last column.

| Stage | Option | Default, max | What runs in parallel | In the config digest |
|---|---|---|---|---|
| `triage` | `workers` | 64, 1024 (D692, D716) | the LLM question calls | no (kept out by `digest_line`) |
| `annotate.<wf>` (screen, affiliation, model_*, technique_roles, technique_staged, affiliation_orgs, translation) | `workers` | 64, 1024 (D692, D716) | the per-document asks (technique_staged: a document's own calls stay sequential within its ask) | no (an annotate digest is the prompt pin's; no option enters it) |
| `acquire` (fetch) | `fetch_workers` | 1, 32 | lane calls grouped by host (http: the URL host; arxiv: `arxiv.org`; oa: `fetch_workers - 2` slots by DOI); one call in flight per group, one request in flight per host | yes (an option of the line) |
| `acquire` (convert) | `convert_workers` | 1, none | converter child processes (pymupdf4llm: 0.6-0.75 GB RSS each) | yes |
| `stack eval …` | `--workers` | 64, 1024 | eval calls | n/a |

Order and results do not depend on the worker count: admission (budget, `max_rows`, halts) is decided in `doc_id`
order in the calling thread (acquire: in its scope order, `options.order`, `doc_id` by default or `newest_first`,
D391), every LLM call reserves in its run's ledger before it is sent, and outputs are
assembled in row order. Per-host pacing, robots, 429 back-off and arXiv's 3 s rule are unchanged by `fetch_workers`.

Measured on copies of a development store (2026-10-13; no 429 anywhere): triage 15.8 → 112 → 229
rows/min at 1/8/16 workers; screen-v12 6.4 → 38.5 → 69.9; affiliation 10.1 → 72.5 → 134; fetch +25 % at 4 workers
(the dev mix is bound by one slow host); convert, 35 PDFs: 223 s / 108 s / 73 s at 1/4/8.

A stage's worker pool also stays within the container's task room less 128 threads (`stack/stage_runner.py`
`thread_room`, D786); without a readable limit the line's count holds.

The production plan `batch-2026-10a` sets `workers` 256 on triage, screen and affiliation, 1024 on technique_staged
and model_roles, 64 on translation and 8 on affiliation_orgs; `fetch_workers: 8`, `convert_workers: 8`. The older
plans set: triage, screen and affiliation `workers: 16`; `convert_workers: 4` (RAM headroom on a shared
machine); `fetch_workers` 4 in `dev-v2-m4`, 8 in `std-v2` and `std-v3`. `dev-v2`, `ann-scratch` and
`links-scratch` set none.

Between jobs there are no locks (D355, D356): a build runs its lines one after another, and the rule is one writer
per series at a time. Two jobs that publish on different chains do not conflict, but nothing checks which
chains a job will write; two that publish on one chain fork it, which the next resolve reports (`docs/OPERATE.md`,
"Two writers, forks and their repair"). Readers (`status`, `head`, `ls`, `diff`) can run anywhere at any time.

## Batch mode (`llm_mode: batch`)

`triage` and every `annotate.<wf>` line take `options.llm_mode: sync | batch` (default `sync`; any other value fails
the run before a call). `batch` asks through OpenRouter's batch API (`POST /batches`), which bills at half the sync
price; one model and one provider per batch. Code: `stack/llm/batch.py` (the driver), `stack/annotate/batchmode.py`
(the stage half). `annotate.technique_staged` takes the options too, but only a one-call ask suits batch (mode
`single` without `window`/`glean`, D640's pin): its other modes make dependent calls per document, which batch mode
was not built for (the stage's `NOTES.md`, "Not done"). Options, none in any config digest (they steer how a run asks,
not what):

| Option | Default | Meaning |
|---|---|---|
| `llm_mode` | `sync` | `batch` turns it on |
| `batch_wait_minutes` | none (hard stop 26 h) | time cap from the start of the batch phase |
| `batch_min_requests` | 100 | a pass that wants fewer requests is asked sync (small weekly runs, tests) |
| `batch_max_requests` | 2000 | requests per batch |
| `batch_max_mb` | 40 | body size per batch (one larger request goes alone) |

**Passes.** The run builds its admitted work list as in sync mode (carry/ask, `max_rows` in order). A *collect* pass
runs every admitted item's normal ask: a call already in the response cache or answered by this run's batches is
served, any other call is recorded and the item stops there. Requests go out in batches as they collect; the driver
polls (15 s, 30 s after 15 min, 60 s after 1 h) and settles each result through the sync client's checks (parse,
schema, truncation, provider, ledger, response cache). A further collect pass runs while the previous one collected
something (screen's tier 2 appears once tier 1 is answered; at most 4 passes). Then the stage's unchanged sync loop
runs as the *replay* pass and writes the evidence: answered calls come from the batch outcomes; a result that was
not usable (5xx, an error item, schema-invalid, truncated) is asked sync there, with `workers` as usual.

**Time cap, `not_attempted[unanswered]`.** An item whose batch had no answer when the batch phase ended (the time
cap, a failed or expired batch — except the results it carries for the part it finished, which settle as usual —
a submit error) is `not_attempted[unanswered]` (count `not_attempted.unanswered`,
triage `ask.not_attempted.unanswered`): no evidence, scope incomplete, so the build ends `incomplete` with reason
`unanswered` when the line's family is in the plan's `require_complete` and the line does not set
`require_complete: false` (D265; OPERATE, "`incomplete` builds and resuming"), and the next build asks those items again; whatever was answered is in the response cache. No
batch is resumed by a later run; the record lists them (`spend.batch_ids` in flight, `spend.abandoned_batch_ids`). **There is no cancel**: a batch still running at the cap is abandoned, keeps running
and is billed; the ledger reconciles its reservations at an estimate (reserved × the run's settled actual/reserved
ratio) and its answers are never read. The same holds for every batch in flight when anything raises out of the
batch phase (a code bug, Ctrl-C: the exception propagates, the run fails, nothing is left at
$0 at the run's end), and for a submit whose answer was lost to the network: it may have been accepted, so it is
never sent again (only a 5xx or 429 answer is retried); its items are `unanswered`. A batch whose poll answers 404 or
410 is lost: counted as failed, listed as abandoned, its reservations charged in full (it may have run and been
billed) and its items `unanswered` (D366, D370).

**Money.** Each request is reserved at submit time at half the sync reservation, against the stage's
`budget.max_usd` (or the per-run default). A refusal with batches in flight waits for them to settle and retries; with
nothing in flight it is the budget stop: the refused items, a suffix in work order, are `not_attempted[budget]` as in
sync. A completed batch's `usage.cost` that its per-result costs leave over is shared over its calls without a costed
result (missing, error items, 200s without a cost), so the ledger holds what the batch billed. Screen's tier-2
allowance (`screen_usd_per_1k`, `tier2_*`) applies per pass and bounds tier 2's *estimated* spend at the batch price:
every tier-2 call counts at its estimate × 0.5 in every pass, collect and replay alike, so all passes admit the same
documents (no extra pass, nothing admitted only in the replay and asked sync at full price).

**Counts** `batch.*` (`passes`, `requests`, `batches`, `answered`, `retry_sync`, `sync`, `refused_budget`,
`unanswered`, `batches_failed`/`_expired`/`_abandoned`, `submit_errors`, `wait_s`, `sync_fallback`); the batch ids
are printed to stderr and so land in the run's log. A batch request carries no `data_collection` provider setting (the batch API refuses it); the
routing pin to the sanctioned provider is kept.
