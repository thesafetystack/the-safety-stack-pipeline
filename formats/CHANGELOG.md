# Changelog: release formats

The format of a release of The Safety Stack dataset ([README.md](README.md): what is structural, the compatibility
rules, how a version is cut). Versions follow semantic versioning from the reader's side: a patch changes prose only,
a minor only adds, a major removes or changes. An entry is dated when the first release of its version is published;
its directory `<major>.<minor>/` is not edited after that.

## [1.0.0]

### Added

- `format_version` (`1.0.0`) in `release.json`, in the parquet file's key-value metadata (beside `release_id`) and in
  the dataset card's front matter.
- A schema next to each file it describes: `release.schema.json` (the manifest), `data/corpus.schema.json` (one row,
  with the exact Arrow type of every node as `x-arrow`), `config/configs.schema.json` and
  `config/overrides.schema.json` (one line).
- Closed and open lists marked: a closed list is an `enum`; an open list carries `x-open: true` and its known values
  in `x-values`.
- `tools/release-validator.py` in every release, byte-identical to this repository's.

### Changed

- The row schema moved from `schema.json` to `data/corpus.schema.json`.
- `release.json` no longer has `format` (it carried the release's date, as `release_id` does).

### Notes

- The dated releases before 1.0.0 have no `format_version` and carry no compatibility guarantee; the checker
  reports them as an unsupported format version.
