# Why is a document missing (or not screened)?

A document can drop out at six points: it was never collected (collector window, coverage); gather merged or
retired it; an operator masked it; triage did not decide `fetch`; acquire has no usable text yet; or the build or
release filtered it. Work from the build's `files.corpus_everything` (every row; the plan needs `fold.diagnostic:
true`, as `dev-v2-m4` has; std-v3 has it off, so set it or read the `documents` head) with the macros loaded
(`docs/CONSUME.md`, "The helper macros"). Run from the store root.

```sql
SET VARIABLE b = (SELECT files FROM read_json('builds/*.dev-v2-m4/build.json.zst')
                  WHERE status = 'ok' ORDER BY finished_at DESC LIMIT 1);
CREATE VIEW everything AS SELECT * FROM read_parquet(getvariable('b').corpus_everything.path);
```

## 1. Find the document

By its typed keys (the reliable way; a forum post's key is `fm_post`, an arXiv paper's `arxiv` and `doi`):

```bash
uv run --no-sync python -c "from stack.urls import link_keys; print(link_keys('https://www.lesswrong.com/posts/NAHnayEuQ2PdFRXHQ/x').keys)"
# (('fm_post', 'NAHnayEuQ2PdFRXHQ'),)
```

```sql
SELECT doc_id, state, excluded_reason(d) AS why FROM everything d
WHERE list_contains(list_transform(identity.keys, k -> k.kind || ':' || k.value), 'fm_post:NAHnayEuQ2PdFRXHQ');
```

Or by a member's fetch URL (`identity.members[].canonical_url` holds the URL as listed, `www.` included):

```sql
SELECT doc_id FROM everything WHERE list_contains(list_transform(identity.members, m -> m.canonical_url), $url);
```

No row: it was not collected, or not in this build's chains. Check the collector's coverage (`stack ls
data/coverage.<p>/<series>/` for its files, the windows as in `docs/howto/inspect.md`, "5. Sources and coverage":
`failed_*` windows are retried next pass) and whether the channel is in the
plan at all. A row with `state = 'retired'` was merged into the document `identity.merged_into` names.

## 2. Read `excluded_reason`

`excluded_reason(d)` is NULL exactly when the row is in a default build (`has_text`). Otherwise, first match wins:

| Token | Meaning | Next look |
|---|---|---|
| `masked:<reason>` | an operator mask (`operator`) or an unresolved `separate` override (`review_required`) | the `identity` overrides (`docs/howto/overrides.md`), `identity.override_ids` |
| `state:retired` | merged into another document | `identity.merged_into` |
| `triage:<status>` | triage did not finish for it (`failed_transient`, `failed_permanent`, `not_attempted`, …) | §3 |
| `triage:skip` (or `triage:none`) | triage decided not to fetch | §3 |
| `state:identified` | gathered, not yet triaged | the next build's triage |
| `state:triaged` | triage said `fetch`; **not skipped, waiting** for acquire (e.g. acquire's `max_rows` cap in dev plans) | the next build or pass |
| `text:<status>` | acquired but no usable text: `no_raw` (nothing fetched), `convert_failed` (conversion or the quality gate failed), `convert_pending`, `withheld` | §4 |
| `media:txt` | the current text is not markdown: an in-band abstract (`content.current_role = 'abstract'`) | §4: the full text was not fetched |

On the dev store (build `wvcgj7me0kctw`, 1,806 rows): NULL 494, `state:triaged` 1,211, `media:txt` 82,
`text:no_raw` 18, `text:convert_failed` 1.

## 3. Triage

```sql
SELECT annotations.triage.status, annotations.triage.error_class, annotations.triage.payload.decision,
       annotations.triage.payload.wanted_by,
       list_transform(annotations.triage.payload.questions, q -> struct_pack(q := q.question, s := q.status, v := q.verdict))
FROM everything WHERE doc_id = $doc;
```

`wanted_by` names the gate consumers (`gate:` keys, e.g. `screen`) whose clauses wanted the document. A question
`skipped` means an earlier clause (a source rule or `sql:`) already decided. The LLM's reasoning is in the build's `triage_evidence` file (`files.triage_evidence`).

## 4. Acquire

```sql
SELECT content.current_text_status, content.current_role, content.current_text_quality,
       list_transform(content.roles, r -> struct_pack(role := r.content_role, lane := r.lane, status := r.status,
                                                      err := r.error_class, attempts := r.attempts, retry := r.retry_after))
FROM everything WHERE doc_id = $doc;
```

`acquire_pending(d)` is true while some role can still succeed; `acquire_failed(d)` when none can. A role
`quarantined[cap_reached]` (its fourth counted failure since its last `ok`; a rate limit is not counted, D484) or
`failed_permanent` of class `dead`, `blocked` or `no_fetchable_url` is retried after 90 days, or now with
`stack acquire reopen` (`docs/OPERATE.md`, "Failed fetches and `acquire reopen`"). The quality verdict of the current snapshot is
in `content.snapshots[].quality` (`docs/CONSUME.md`, "Text quality"). A role `skipped[gated]` was not
fetched because triage does not let the row fetch (it becomes due if a later gate lets it).

## 5. Screen and release

A row with text is in `files.corpus` whatever screen said; its `annotations.screen.status` may be
`not_attempted[limit|budget|halted|unanswered]` (not reached this build; `unanswered`: batch mode had no answer yet), `skipped[no_text]`, `failed_*`. A release then drops masked,
retired and sensitive rows and applies the licence rules: `release.json`'s `dropped: {reason: n}` counts them.
