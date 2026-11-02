# Recall benchmarks

Lists of documents the corpus should hold, made outside this project, for measuring collection recall.

| File | What it is | Tool |
|---|---|---|
| `concordia-2025-07.jsonl` | Concordia AI, *Chinese Technical AI Safety Database* (public Google Sheet, sheet "Chinese Frontier AI Safety Papers"), the 2025-07 xlsx export: 464 rows (one paper listed twice), 462 with an arXiv id, 2023-05 to 2025-05; title, URL, arXiv id, research direction, date, institutions, authors. Measured on 2026-10-30 (the Chinese-coverage work): the 2026-10-27 release holds 354 (76.3 %), the store copy of 2026-10-23 368 (79.3 %); weakest directions Systemic Safety (31 % in the release) and Evaluations (60 %). | `tools/recall_concordia.py` (`build` from a fresh export, `check` against a documents head or a release corpus; no network) |

The SR 2025 recall set (`stack.evals.sr`) is an annotation eval set, not one of these files.
