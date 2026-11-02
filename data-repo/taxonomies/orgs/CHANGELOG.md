# Changelog: orgs

## 0.2.2 (draft, 2026-10-30)

- 0.2.0 with two sets of additions made in the pipeline repository on top of its own 0.2.0 draft, taken over here
  unchanged:
  - place names, `meta.places` (Berkeley, Columbia, Princeton, Stanford, Standford): a comma segment with one of
    these after an organisation's name is its address ("Redwood Research, Berkeley, CA"), not another organisation
    (the pipeline's matcher from version 3 on); the Institute for Advanced Study (Princeton; matched only with
    Princeton after the name); aliases `Centre for AI Safety` (cais) and `RIKEN AIP`;
  - Chinese-script aliases: 90 aliases of 56 Chinese organisations (ROR v2 names with Han characters, OpenAlex
    institution alternative names, hand additions: simplified and traditional forms, short company names) and the
    Chinese names of China, Hong Kong, Macao and Taiwan in `meta.countries`; `meta.sources.chinese_names` says where
    they come from. The pipeline's matcher tokenises Han and kana one character a token (version 4), so an alias
    matches inside an unspaced affiliation string.
- 4,841 entries (0.2.0's 4,840 and the Institute for Advanced Study). sha256
  `0ce69dab6be13ed988b6932223dca256422d0d832cc4dc7fa13b194800072ed0`.

## 0.2.0 (draft, 2026-10-29)

- First version in this repository, in the catalogue envelope (schema `orgs-0.schema.json`). Built in the pipeline
  repository from 0.1.0 (named teams and labs as entries under their parent, the FAIR rule, unit words and US
  regions, an external review's accepted findings, recall additions), then the models makers' facets taken over
  here:
  - the tag `big-tech` on the 32 makers whose models-1.0.0 `meta.maker_facets.org_type` was `big-tech` (the other
    facet types correspond to `type`);
  - the 34 facet notes, appended to the entry's `note`;
  - `arc-institute` is the Arc Institute (US, a nonprofit; the models maker of Evo). The pipeline's 0.2.0 draft had
    the ROR record of ARC Institute Indonesia under that id; it is now `arc-institute-indonesia`. No affiliation in
    the dataset named either.
  - Every maker was already tagged `model-maker`, and every maker with a facet country already had it, except
    `arc-institute` above.
- 4,840 entries. sha256 `839d07ad6f9c4157604860743fb7af47935288e7c8ad26b12e759731d49bc997` (the pipeline's draft
  before the take-over: `c4ca7465e443…`).
