# Contributing

**Reports about the dataset** (a wrong annotation or metadata field, a missing work, a takedown, a taxonomy
suggestion) go through the issue forms of the data repository,
https://github.com/thesafetystack/the-safety-stack/issues/new/choose, or privately to info@thesafetystack.ai.
Taxonomy changes are made there too (its `CONTRIBUTING.md`), not in this repository's `data-repo/`.

**Changes to the pipeline.** Setup is in `README.md` and `docs/SETUP.md`; how the code is laid out and the working
conventions are in `AGENTS.md` (written for coding agents, and as true for people). Before a pull request:

- run the tests for what you touched (`uv run --no-sync python tools/devtest.py`) and, at the end, the gate
  (`uv run --no-sync tools/check_all.py --no-sync`; there is no CI). Without the eval-text cache the eval-set tests
  skip, which is expected;
- a change of behaviour gets a row in `docs/DECISIONS.md` and the docs it changes (`docs/README.md` is the map);
- a changed prompt is a new file and a new pin (`docs/howto/change-prompt-pin.md`): editing a pinned prompt re-asks
  every document in scope;
- keep the repository publishable: no keys, no bucket names, no local paths, no links to link-shared documents
  (`tests/test_public_tree.py`), no third-party document text (`AGENTS.md`, "Keeping the repository publishable").

The code is MIT-licensed (`LICENSE`); by contributing you agree to your contribution being released under it.
