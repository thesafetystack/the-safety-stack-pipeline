# Contributing

Reports are the main way to help: they go in as issues, through the forms on
[the new-issue page](https://github.com/thesafetystack/the-safety-stack/issues/new/choose). Takedowns and other private requests
go to [info@thesafetystack.ai](mailto:info@thesafetystack.ai).

| You want to | Form | What we need |
|---|---|---|
| fix a wrong annotation or metadata field | Correction | the document's `doc_id` or URL, which field or label is wrong, what it should be, and the passage or source that shows it |
| have a document or your name removed | Takedown | the `doc_id` or URL, what should be removed, and why; or email [info@thesafetystack.ai](mailto:info@thesafetystack.ai) instead of posting publicly |
| add a document the dataset misses | Missing work | a URL, DOI or arXiv id, and which part of the [inclusion criterion](README.md#what-is-included) it meets |
| change a taxonomy | Taxonomy suggestion | the slug or node id (or the proposed name), the change, and one or two documents that show the need |

What happens next:

- We acknowledge every report. We do not promise a date for resolving it.
- An accepted correction, takedown or addition is applied in the next dataset release, as an override row that
  ships with that release on Hugging Face (in `config/overrides.jsonl`); the issue is closed with a link to it.
- A taxonomy change goes into a new taxonomy version (see [taxonomies/README.md](taxonomies/README.md#versioning));
  released taxonomy files are never edited, so pull requests that edit them cannot be merged as they are. Pull
  requests to the documentation are welcome.

Code in `tools/` is MIT-licensed; everything else in this repository is CC0 1.0 (see the [README](README.md#licences)).
By contributing you agree to your contribution being released under the same terms.
