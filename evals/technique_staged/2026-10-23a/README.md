# technique_staged eval set 2026-10-23a: gold v3 sample (100 documents, input-only; adjudicated reference, no `gold.jsonl`)

Status: current. The production technique prompts were chosen on this sample (D708); its adjudicated reference
(`adjudication/reference-v3x.jsonl`) is the technique-presence reference of the 2026-10-27 technique round
(`docs/design/technique-quality-2026-10.md`).

A new set for `stack eval technique_staged`, frozen with prompt set `technique-staged-v3` and the production taxonomy
`taxonomy/taxonomy-v0.9.1.yaml`. 100 documents drawn from the 2026-10-23 build's screen-relevant corpus (all licence
classes, degraded texts included), **shared with `evals/model_roles/2026-10-23a`** (same item ids g3-0001..g3-0100,
same texts). There is no `gold.jsonl` (absent here, empty in the private copy of the set): the set was labelled blind by strong
agentic labellers (Opus 5.5 and Sonnet 5.5 as Claude Code subagents, Sol 6.1 through Codex CLI, two repeats each)
and adjudicated into the slug-level reference for technique presence `adjudication/reference-v3x.jsonl`
(2026-10-27; "Adjudicated reference" below), which `stack eval`'s gold scoring does not read.

## Where the set lives
The sample includes `restricted` texts, so **no text is in the repository**. This directory holds `items.jsonl` and
`manifest.json`, the sample (`sources/`), the scripts and the labelling material (`labelling/`). The complete set (the
same two files, `texts/g3-NNNN.txt.gz`, an empty `gold.jsonl`), written by `scripts/freeze.py`, is kept in a private
directory outside the repository. With it, run with `--evals <private evals directory>`:

    uv run --no-sync stack --plan configs/plans/ann-scratch.yaml eval technique_staged --set 2026-10-23a \
        --evals <private evals directory> --split all --models openai/gpt-6-luna --prompt technique-staged-v3 \
        --pin mode=single --pin role=extract_staged --pin taxonomy=taxonomy/taxonomy-v0.9.1.yaml --max-usd 2.5

(`--max-usd` must cover the no-cache reservation, $1.62.) With no gold the run is scored for cost, latency and
failures only.

## Sampling (`scripts/sampling/sample.py`, `sources/sample.jsonl`, `sources/sampling.json`)
- Frame: the `screen_relevant` rows (screen status ok, verdict relevant) of corpus build
  `20261023T161733Z_rnm6jsz5t2ena` (39,267 rows; corpus file sha256 in `sampling.json`). No licence filter (the eval
  is internal; only metadata may be published). Excluded: 278 documents whose text_usability verdict is `unusable`
  (every frame row has current text status ok and its blob present) and 468 documents already in an existing eval set
  (any `evals/*/*/items*.jsonl` row: same doc_id, former or same-raw doc_id, same text sha256, or same normalised
  title over 15 characters; 172 of them match by doc_id, the rest by text or title, e.g. triage and screen sets and
  the 2026-10-13a/17a model_roles documents from an earlier store). Eligible: 38,521.
- Source kind from doc_class: paper (preprint, paper), forum (forum_post), blog (lab_blog), card (system_card,
  model_card), report (eval_report, framework_policy, critique, other, webpage, regulatory). Allocation proportional
  to the eligible counts, minimum 3 per kind (largest remainder); within a kind, systematic sampling with a seeded
  start over the documents sorted by (year bucket, model-mention bucket, seeded random key): implicit stratification
  by year and mention count, equal inclusion probability within a kind, design weight N_k / n_k. Year bucket from
  `metadata.published_at` (<=2019, 2020-2023, 2024, 2025, 2026, none). Mention bucket = distinct strong-matched nodes
  of the LLM-free matcher (`workflows/annotate/model_roles/match.py`, `configs/models.yaml` v0.9) over the whole
  current text: 0, 1-3, 4+. Seed 20261023; item ids assigned in a seeded random order.
- Realised allocation, sample n (eligible N in parentheses):

| kind | N | n | weight | <=2019 | 2020-2023 | 2024 | 2025 | 2026 | none | mentions 0 | 1-3 | 4+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| paper | 24,345 | 60 | 405.75 | 1 (609) | 7 (2,869) | 9 (3,655) | 16 (6,340) | 27 (10,858) | 0 (14) | 7 (2,741) | 8 (4,141) | 45 (17,463) |
| forum | 12,192 | 30 | 406.4 | 4 (1,493) | 11 (4,395) | 3 (1,465) | 6 (2,246) | 6 (2,578) | 0 (15) | 18 (7,135) | 9 (3,328) | 3 (1,729) |
| blog | 1,652 | 4 | 413.0 | 1 (284) | 0 (175) | 0 (137) | 1 (210) | 1 (418) | 1 (428) | 2 (801) | 2 (523) | 0 (328) |
| card | 98 | 3 | 32.67 | 0 | 0 | 0 (1) | 0 (16) | 1 (17) | 2 (64) | 0 | 0 (21) | 3 (77) |
| report | 234 | 3 | 78.0 | 0 (10) | 1 (41) | 0 (33) | 1 (53) | 1 (85) | 0 (12) | 0 (57) | 1 (53) | 2 (124) |

  Length bands: <20k 24, 20-60k 35, 60-100k 21, >100k 20 (1,309-585,158 characters, median 52,435). Licence: open 89,
  public_web 7, restricted 4. Text usability: usable 65, degraded 35. Two texts exceed the production 300,000-character
  window (g3-0051, g3-0100; `manifest.json` `truncated_items`). Weighted estimates over the frame use `weight`
  (items.jsonl) or `sources/sample.jsonl`.
- Reproduce: `sample.py frame <STORE_DIR> <CORPUS_REL> <OUT>`, `sample.py scan <STORE_DIR> <OUT>` (about 20 minutes at
  2 processes), `sample.py select <OUT> evals`; `select` reproduces `sources/sample.jsonl` byte for byte from the frame
  and the scan (`sampling.json` has the sha256 of both inputs; checked under three hash seeds).

## Files and scripts
- `items.jsonl`: as 2026-10-18b (`input` = title, basis_role, text_digest = the blob sha256, full_text_digest,
  full_text_chars, `staged` = `main.freeze_info(text, name="technique-staged-v3", taxonomy_path=v0.9.1)`;
  `input_digest` = sha256 of the single user message; `license_class`, `source_item` `mirror:<doc_id>`, `split` dev
  51 / test 49 alternating by length within the kind) plus `stratum` {channel, channel_group, doc_type, group, band,
  year_bucket, mention_bucket} and `weight`.
- `manifest.json`: counts, `frozen_with` (taxonomy v0.9.1 path / sha256 / version, rubric, prompt digests, segmenter and
  lexical versions), the sampling summary, `input_only: true`, `gold: null`.
- `scripts/freeze.py <STORE_DIR> <FRAME_PARQUET> <private evals directory> [--write]` writes (or checks byte for
  byte) both 2026-10-23a sets: the private copies and this directory's items/manifest.
- `scripts/packets.py <private evals directory> <FRAME_PARQUET> <OUT>` builds the labelling packets in a private directory:
  per document the text, a metadata header and the production model menu; the shared files of each task; one
  isolated session directory per (task, labeller, repeat) with its batch prompts; `packet.json` with every file's
  sha256.
- `labelling/`: `GUIDE.md` (guide v2.1 blind = guide v2.0 of 2026-10-18b with the files, the blind setting and the
  per-file output changed; the content rules are v2.0's), `INSTRUCTIONS.md` (the agentic labeller procedure:
  read the whole document, inventory pass over the menu, verify quotes verbatim, write `out/<item_id>.json`, run the
  checker), `schema.json` (2026-10-18b's labeller row shape plus `item_id` / `labeller`; quote, confidence and roles
  per row), `check_labels.py` (stdlib checker for both tasks: schema, slugs / menu nodes, role invariant, verbatim
  quotes). The packet adds RULINGS.md (2026-10-18b's, sha256 `d08458ed…`), `rubric-v0.4.0.md` and the menu text of
  taxonomy v0.9.1 under a title line.
- `scripts/analysis.py score <PACKET_DIR>` (no LLM call): normalises the six strong runs (opus, sonnet, sol x 2
  repeats) and luna's eval rows of both tasks into units (technique slugs without group nodes and (slug, role)
  cells; model_roles (node, role) cells as `score.model_roles_block`, nodes, mentions), and reports repeat and
  between-labeller agreement, support counts (how many of the six runs have a unit), luna against single runs and
  >= k/6 consensus, recall by support / role / confidence, leave-one-family-out comparisons, strata, design-weighted
  estimates and document bootstrap intervals (`<PACKET_DIR>/analysis/`). `scripts/analysis.py blindness <PACKET_DIR>
  <TRANSCRIPTS_DIR> <SHARED_TMP_DIR>` checks the Claude labellers' transcripts for paths outside their session and
  for shared temporary files, and the six runs' files for identical content. Pre-adjudication only: there is no
  decision pass and no `gold.jsonl` yet.
- `scripts/analysis.py arms <PACKET_DIR> <techniques|model_roles> --baseline L --arm L=RUN_DIR[@SUBSET_RUN][,RUN_DIR]
  ...` (no LLM call) scores eval runs of luna variants against the same references (>= 4/6, 6/6, union, recall by
  support), with bootstrap intervals, paired differences against the baseline arm, failures, schema degrades and
  cost per 1k documents (standalone, and token-based first-encounter); writes `analysis/arms-<task>.{md,json}`.
  More arm forms: `RUN@OVERLAY>CHARS` (the overlay only for documents longer than CHARS, e.g. a costlier setting for
  long documents), `RUN%` (one replicate: the union of a run's repeats), `A+B` (per repeat, the union of two runs).
  `--split-file split.json` adds per-split tables (dev / test / all: P / R / F1, paired dF1 with its interval, 6/6
  recall, union precision, fresh cost, median latency, failures).
- `scripts/split.py` (no LLM call) writes `split.json` here and in `evals/model_roles/2026-10-23a/` (identical): a
  dev / test split for prompt and configuration tuning, 60 / 40, stratified by source kind x length band, seed
  20261024 (dev: 60, test: 40). Tuning reads dev only; arms are reported on dev, test and all. It is not the
  `split` field of `items.jsonl` (the freeze's default dev 51 / test 49, unchanged so the frozen digests hold).

## Results
luna, production configuration (the command above), 2026-10-23, `20261023T181337Z_all_a185d829`: 100 of 100
answered (88 ok, 12 empty), all 100 `schema_degraded` (as on 18c), $0.486 settled (estimate $0.97 expected, $1.62
reserved). Technique rows per document: median 2, mean 3.71, max 42 (371 in all); 23 documents with no row. Not
scored (no gold).

Strong-labeller round (2026-10-23, `scripts/analysis.py score`, before any adjudication): all 1,200 label files
pass the checker. Four model_roles files of sonnet-r1 (g3-0010, g3-0067, g3-0082, g3-0087) are byte copies of
sonnet-r2's (labeller field `sonnet-r2`: concurrent labeller agents reused temporary script names), so model_roles
consensus metrics use 96 documents. Strict slugs, luna against the >= 4/6 consensus: P 0.895 [0.860, 0.924], R 0.630
[0.576, 0.680], F1 0.739 (100 documents, 2,000 bootstrap resamples within source kind); against the union of the
six runs precision 0.974; recall on slugs all six runs give 0.754. model_roles cells >= 2 against >= 4/6: P 0.784,
R 0.866, F1 0.823.

## Adjudicated reference (2026-10-27)
`adjudication/` holds the slug-level adjudicated reference `reference-v3x.jsonl` for technique presence (the six runs'
disputed units decided by two blind adjudicators of different families, their splits by two arbiters, plus the
compared arms' slugs outside the runs' union; `dont_care` where they did not converge), the adjudication and
arbitration guides, the answer checker and the dev / validation split. Its README has the method, counts and
agreement (adjudicators' implied references F1 .875; labeller families .74-.83).
