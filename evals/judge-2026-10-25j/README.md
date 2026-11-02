# Judge eval sets 2026-10-25j (the judge-eval round): every LLM judgement of build ws1qpn8myzxah, judged by Opus 5.5, Sol 6.1 and Sonnet 5.5

Five independent samples of 100 documents, one per LLM judgement of the Stack v4 pipeline, labelled blind by three
strong agentic judges (claude-opus-5-5 and, as a third judge added 2026-10-26, claude-sonnet-5-5 via the claude CLI;
gpt-6.1-sol via the codex CLI), each confined to a read-only task directory. Production (luna) labels are recorded beside them for scoring. No text is in this
directory: the documents are third-party content and sit in the private root (`EVAL104_PRIVATE`). Stability not
promised.

Status (2026-10-26): full run done (1,800 judge outputs: 100 items x 6 call types x 3 judges), validated, scored;
labels, scores and disagreement lists are in each set directory. Opus and Sol ran 2026-10-25/26; Sonnet was added
afterwards (2026-10-26 11:19-12:13Z) on the same, unchanged task dirs.

## Method

- **Judges.** Opus: `claude -p --model claude-opus-5-5 --effort high --restricted --strict-mcp-config --tools
  Read,Grep,Glob --no-session-persistence --output-format json <prompt>` (Claude Code 2.1.308). Sol: `codex exec
  --skip-git-repo-check --ephemeral --ignore-user-config -m gpt-6.1-sol -c model_reasoning_effort="high" -s read-only
  -C /mnt/task --json <prompt>` (codex-cli 0.162.1; stdin closed; final message = the last agent_message event).
  Sonnet: the Opus command with `--model claude-sonnet-5-5`, nothing else changed (same jail, task dirs, guides,
  retry and usage-limit rules). The prompt is one short wrapper per call type, the same for every judge
  (`guides/prompt-<call>.txt`).
- **Jail and blindness.** Each call runs in a user + mount + pid namespace (`jail.sh`): the task dir mounted
  read-only as the working directory, fresh tmpfs `$HOME` and `/tmp`, only the CLI binaries and the CLI's own auth file
  bind-mounted. A task dir holds only the guide, the schema, the document (`text.txt`; triage `record.txt`), `meta.md`
  and the set's shared files; nothing about production output or the other judges. The runner checks the file list
  before each call; no Sol command named a path outside the task dir (pilot check). Residual: codex's shell can read
  its own auth file; every output is secret-scanned.
- **Whole text.** Judges read the whole current text (production read a window; `items.jsonl` `luna_window` says
  which), so a disagreement can be split into window-caused and judgement errors.
- **Validation** (`scripts/validate.py`, rules set 2026-10-26): structural misnesting with all content present is
  repaired deterministically and flagged `repaired` (2 Opus screen-relevance outputs closed `sr` before
  `category` / `agenda`: jscr-0076, jscr-0094); a length-cap overrun is accepted and flagged `over_length` (jscr-0091,
  the Opus rationale is over its 400-character maxLength in the full text, cut to 400 in the released file);
  anything else would be listed as invalid (none). All other 1,197 Opus and Sol outputs are schema-valid as
  returned. Sonnet: 600 / 600 valid, none repaired or over length in the final records. 28 attempts failed and were
  retried under the runner's rule: model_roles 12 (10 with role keys missing, 2 without a JSON object), screen 15
  (all over a maxLength: 9 usability notes, 6 relevance rationales), triage 1 (an extra key); the two
  screen-relevance calls that failed both attempts (jscr-0003, jscr-0091, over-length rationales) passed on the one
  `--retry-failed` rerun. 15 Sonnet outputs (5 each in screen, model_roles, technique_staged) put prose before the
  JSON object; the object was extracted (the runner's existing rule) and the rows carry `flags.extracted_from_text`.
- **Release capping** (`scripts/release.py`): verbatim fields (quotes, surfaces, names as written) at 300 characters at
  a word boundary with "…" (the dataset release's `cut_string`), listed per row in `flags.capped` (13 Sol model_roles
  rows had a quote over 300); reasoning fields uncapped (no rationale carries > 300 characters of contiguous document
  text: checked for every judge row and production's reasoning).

## Sets

| set | frame (population) | eligible | calls per doc | allocation (n per stratum) |
|---|---|---|---|---|
| triage | 149,644 `documents` rows in state acquired with an LLM triage verdict (28 retired rows out) | 149,541 | 1 | fields: not_relevant 81, relevant 4, borderline 2; title: not_relevant 10, relevant 2, borderline 1 |
| screen | 73,625 corpus rows with an ai_check `ok` screen (27 human, 3 quarantined out) | 72,906 | 2 (relevance S+R; quality U+D) | preprint 44, forum_post 38, paper 13, lab_blog 5 |
| affiliation | 42,397 ai_check `ok` affiliation rows | 41,944 | 1 | preprint 77, paper 23 |
| model_roles | 39,259 luna rows (`ok` + `empty`; 10 Claude stand-ins out) | 38,672 | 1 | preprint 49, forum_post 32, paper 14, lab_blog 5 |
| technique_staged | 39,258 luna rows (11 Claude stand-ins out) | 38,670 | 1 | preprint 49, forum_post 32, paper 14, lab_blog 5 |

`triage_w/` is a 200-item weighted supplement to `triage` (production positives and low-confidence `not_relevant`
oversampled, scored with design weights against the 3-judge majority); its design and results are in
`triage_w/README.md`.

Exclusions between frame and eligible (`<set>/sampling.json`): documents of every existing eval set
(`evals/*/*/items*.jsonl`, gold-v3 included; doc id, former / same-raw id, text sha256, normalised title > 15
characters), and for affiliation / model_roles / technique_staged documents whose text blob is not in the local mirror
(affiliation 42, the others 0). Screen has no blob condition (re-sampled 2026-10-25: the 2,129 screen documents
missing from the mirror, 1,874 of them screen-irrelevant LessWrong posts, were a bias); the sampled items' missing
blobs (2) were fetched read-only from the store by `scripts/fetch_blobs.py` into `EVAL104_PRIVATE/blobs-extra/`,
sha256-checked. Strata: triage = evidence basis x production verdict, corpus sets =
`doc_class.doc_type`; proportional allocation (largest remainder), so strata under 0.5 % of the frame get no item
(system cards, eval reports, framework documents ...). Within a stratum, systematic sampling over a seeded sort
(implicit stratification by year bucket and anchor channel). Self-weighting: weight = N_eligible / 100.

## Files

- `<set>/sample.jsonl`, `<set>/sampling.json` (frame counts, exclusions, allocation, seed 20261025, input sha256s).
- `<set>/items.jsonl`: ids, stratum and weight, metadata, digests of every task file, **luna's input window**
  (`luna_window`: screen tier-1/tier-2 excerpt sizes and whether the whole text was seen; affiliation's header
  window; model_roles / technique doc cap) and the input verification. Screen items carry `doc_class_rule`.
- `<set>/production.jsonl`: production labels (verbatim fields capped as above; technique `prepass` internals left out).
- `<set>/labels-opus.jsonl`, `<set>/labels-sol.jsonl`, `<set>/labels-sonnet.jsonl`: per (item, call) the label,
  validation status and flags, judge model / CLI / version, cost (Opus, Sonnet: notional `total_cost_usd`, retries
  within the record included) or tokens (Sol), turns, wall time, attempts, sha256 of the prompt, guide and schema the
  judge saw.
- `<set>/scores.md`, `<set>/scores.json` (`score.py`), `<set>/disagreements.jsonl` (items or units where production and
  the judges do not all agree, for adjudication).
- `model_roles/judge-normalized.jsonl` (added 2026-10-27, `score.py --write-model-roles-normalized`): each judge's
  model_roles answer after production's `judge.normalize` (menu over the whole text), as the models with a role and
  their strengths, whether normalize repaired it; written from the released labels, its units equal the ones from the
  validated labels (every `score.py` run checks). The dataset release reads it instead of the texts.
- `<set>/manifest.json`; `guides/`: every guide variant, the shared rubric / catalogue files, the judge schemas and
  the one wrapper prompt per call type.
- `scripts/`: `common.py` (paths from `EVAL104_DATA`, `EVAL104_PRIVATE`, `EVAL104_BLOBS_GLOB`, `EVAL104_JAIL`),
  `sample.py`, `guides.py`, `freeze.py` (`--check-tasks`: re-derive items / production and verify every task dir
  byte for byte without writing), `fetch_blobs.py`, `run_judges.py`, `validate.py`, `score.py`, `release.py`,
  `secret_scan.py` (docstrings are the reference).

## Re-scoring at release time (D728, 2026-10-27)

The dataset release scores these stored judge labels against its own build's production labels
(`stack.evals.release_quality`, offline, no LLM): production labels from the build's corpus, `documents` and
`triage_evidence` tables (screen and triage samples include documents outside the release file); an item whose
document is gone, is outside the set's population, or whose input changed since it was frozen is dropped and
counted; triage_w's strata, N_h and weights are recomputed on the build's population with the frame and exclusions
below, each item re-assigned to the stratum of its production label there. The metrics and per-set readers were
moved from `scripts/score.py` and `scripts/score_triage_w.py` into that module unchanged (both scripts import them and
reproduce their committed outputs byte for byte); re-scoring build ws1qpn8myzxah reproduces every committed
`scores.json` number except what it does not recompute (screen and affiliation window splits: they read texts or
describe this build's inputs) and Sol's model_roles repair count (3, not 16: the released labels' quotes are already
capped at 300 characters; the units are the same). `python -m stack.evals.release_quality rescore --store ROOT
--build ID --compare` prints that comparison.

## Scoring

Production is the prediction; references are each judge, the **majority** and the **consensus**. Majority (headline
since 2026-10-26): categorical = the label at least 2 of the 3 judges give (a judge with no label for the item casts
no vote; 1-1-1 is no majority and the item is left out, counted as "no majority"); sets = units at least 2 of the 3
judges have, over items where all three judges have a record, plus production's precision against the 3-judge union.
Consensus keeps its 2026-10-25 meaning (categorical: items where Opus and Sol give the same label; sets: units both
have, plus precision against their union) so the two-judge numbers stay visible beside the majority ones;
`score.py --judges opus,sol` reproduces the two-judge outputs exactly (checked: identical JSON apart from the added
per-class F1). Every pair of sources is compared (production and each judge, each judge pair). Each `scores.md`
opens with a headline table (production vs majority and vs consensus per judgement). Categorical: agreement,
Cohen's kappa, confusion tables, per-class P / R / F1 (the screen's keep view, production vs each reference, also with
95 % document-bootstrap intervals, `per_class_ci95`, added 2026-10-27). Sets: micro P / R / F1 with 95 % document-bootstrap intervals
(2,000 resamples within stratum, seed 20261026). Screen: S, R, overall, keep, R category, usability verdict, issue
codes, doc_type (model-judged rows only, production's metadata paper/preprint rule applied to the judges' answers),
self_published, primary_lang; window split by whether luna saw the whole text. Affiliation: (author, surface), (author,
org slug), authors with an affiliation, and a window split (consensus surfaces inside vs outside luna's header window).
model_roles: (node, role) >= 2 and >= 1 and node presence, judge answers through production's `judge.normalize`.
technique_staged: slug presence and (slug, role) >= 2 / >= 1, group nodes out. The samples are self-weighting.

## Guides: production definitions, adapted only to the setting

Each guide is a labeller preamble plus the current production prompt, assembled by `guides.py` with literal
replacements that must each match the production text once (a changed prompt fails the build): triage
`safety-relevance-v4` / `safety-relevance-title-v2` (by the evidence row's basis); screen `screen-v12` split into
S + R and U + D; affiliation `extract.md` (affiliation-v1); model_roles `model-roles-v3b` + `rubric-v0.6.0` + the
matcher menu (built over the whole text) + the catalogue; technique_staged `technique-staged-single-v5-a1.1` with its
gleaning turn folded in as a second pass, `rubric-v0.9-c`, the taxonomy 1.0.0 menu. Production schemas, except: screen
split in two (quality adds `rationale`), affiliation adds an optional `notes`. The older `evals/rubrics/*` are not used.

Deviations from production's input, on purpose: judges read the whole text (`text.txt` = the current text blob), not
luna's window; lines over 1,500 characters are broken at a space (Claude Code's Read truncates lines over 2,000; a
space becomes a newline, so length and offsets are unchanged; 22 / 13 / 9 / 36 lines without a space stay over 2,000
in screen / affiliation / model_roles / technique texts). Triage judges get the rendered record (`record.txt`, also
line-broken where long; 18 records) and nothing else.

## Input verification (freeze)

triage rendered_digest 100 / 100; screen tier-1 rendered_digest 99 / 100 (jscr-0011 does not verify; its title metadata
was updated after the ask, a likely cause, not confirmed: no member or revision title verifies either); model_roles 100 / 100; affiliation
96 / 100 on the current author metadata, the other 4 verified with the roster rebuilt from the evidence items (the
authors metadata changed after the ask; `roster_source`); technique_staged not checkable (the stored digest spans the
gleaning call, whose prior turn holds luna's answer): basis digest = current text sha256 for all 100.

## Running

    source <impl dir>/env.sh   # sets the four variables and PYTHONPATH (private paths, not in this repo)
    $PY scripts/run_judges.py --dry-run            # jobs left (resumable; Opus 4, Sol 8, Sonnet 4 parallel)
    $PY scripts/validate.py && $PY scripts/score.py && $PY scripts/release.py
    $PY scripts/secret_scan.py <files to copy>     # before anything goes into the repository

## Cost of the run

Opus notional `total_cost_usd` $209.33 (triage 4.46, screen 51.29, affiliation 15.79, model_roles 47.15,
technique_staged 90.64). Sol (no cost reported): 241.1M input tokens (217.6M of them cached), 1.79M output. Sonnet
notional $88.98 over all 628 attempts incl. the 12-call pilot (released rows sum to $88.77: the two calls rerun by
`--retry-failed` lost their first record's $0.21; triage 2.24, screen 18.87, affiliation 4.66, model_roles 22.35,
technique_staged 40.85); 54 minutes wall at parallel 4, no usage-limit waits.

## Limits

- Small positive counts: triage has 6 production-relevant and 3 borderline items; screen doc_type is scored on 57
  papers whose paper/preprint split the metadata fixes (the model only decides paper vs other), so its 1.000 says
  little; usability judgements differ in input (whole text vs luna's excerpt).
- Proportional allocation leaves rare doc types (cards, eval reports, framework documents) without items.
- Judges are not gold: Opus and Sol disagree on 6-29 % of categorical items and Sol lists ~40 % more techniques than
  Opus; consensus metrics count only what both find.
- The majority is not three independent votes: Opus and Sonnet are one model family and agree with each other more
  than either agrees with Sol on most judgements (screen S verdict kappa 0.88 vs 0.53 / 0.55; technique slug F1 0.83
  vs 0.73 / 0.65), so where they agree Sol is outvoted. Both use `borderline` far more than Sol or production
  (screen overall verdict: Opus 22, Sonnet 22, Sol 9, production 2 of 100), which moves the majority towards
  borderline. Read the majority beside the consensus and the per-judge columns.
- `collect.links` link selection is not covered (DESIGN.md). gitleaks is not installed: `secret_scan.py` runs its regex
  and bucket-name rules only. One screen item (jscr-0011) fails the tier-1 rendered_digest check (see above).
