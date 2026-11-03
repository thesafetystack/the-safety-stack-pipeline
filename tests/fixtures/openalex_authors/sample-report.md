# OpenAlex author ids for the researcher roster

Generated 2026-10-13 00:00 UTC by `tools/resolve_openalex_authors.py` (round 5 (RUN) B7b, C4 of `docs/design/collection-yield.md`; coauthor evidence, T-ARX). Open: the ambiguous and not-found names below are undecided; the resolved ids are already in the roster.

- Roster: `tests (fake roster)` version 1.1.0 → 1.3.0; 6 names considered.
- Requests: autocomplete 6, details 2, orcid 1, search 1, works 1; OpenAlex cost $0.0018 (run `resolve-test`).
- Rule (names): candidate = name match (`names_match`); active = ≥ 2 works and a work in the last 3 years; one active candidate → resolved if adjacent-field share ≥ 30% or a configured institution; several → the only one at a configured institution (CS share ≥ 20%; for a common name a last known one), else, for a name that is not common, the top one when it has ≥ 5 CS works, CS share ≥ 20% and 3x the next one's CS works. Common = on the roster's `disambiguate` list or ≥ 5 active candidates. A profile named with a first initial only is not a candidate. Details in the tool's docstring.
- Rule (coauthors): the works since 2019-01-01 of every resolved roster id, those with at most 99 authorships; their name-matching authors join the candidates. Coauthor link = ≥ 2 distinct resolved roster coauthors over ≥ 2 distinct works (the name itself and its roster namesakes do not count; roster spellings sharing an id are one person; a profile that is another roster person's id is theirs). A profile counts only when ≥ 33% of its works since 2019-01-01 are shared with roster people (else a merged namesake: it neither resolves nor blocks). An ambiguous or not-found name resolves (`coauthors`) when its coauthor-linked profiles are one person: the top one (most roster coauthors; for a common name ≥ 3) and its splits, each linked and sharing ≥ 1 roster coauthor with it (common name: ≥ 2), or carrying its ORCID and sharing one; a linked profile left over, or one with a different ORCID, keeps the name ambiguous. A name with exactly one name-matching profile, not active, with CS share ≥ 20% or a configured institution, resolves (`single_inactive`). Resolved names gain splits by the same split rule; no id is removed.
- Coauthor round 1: 2 resolved ids indexed (0 without fetched works), 2 works used, 1 fetched works skipped (> 99 authorships), 1 candidates from works; 1 names newly resolved, 0 split ids added.
- Candidate cells: `id` name; works; CS share and AI share (subfields 1702, 1707) of topic counts; works in the last 3 years; last known institution; configured org; ORCID; resolved roster coauthors (most shared works first) and the distinct works shared.

## Summary

| | Before (name rule) | After (+ coauthor rule) |
|---|---|---|
| resolved (ids in the roster) | 2 | 3 |
| of which newly resolved | | 1 |
| resolved: orcid | 1 | 1 |
| resolved: single | 1 | 1 |
| resolved: coauthors | 0 | 1 |
| resolved names that gained split ids | | 0 (0 ids) |
| ambiguous (decide) | 3 | 2 |
| not found | 1 | 1 |
| not queried (budget) or error | 0 | 0 |
| ids removed by the merged-profile guard | | 0 |
| ids in the roster | 0 | 3 |
| roster names with several ids | 0 | 0 |

## Ambiguous (2)

To accept a candidate, add its id to the roster entry (`- {name: "…", openalex_ids: [A…]}`); several ids are fine when they are one person's split profiles.

| Name | C4 | Reason | Candidate 1 | Candidate 2 | Candidate 3 |
|---|---|---|---|---|---|
| Yu Cheng | yes | common name (3 active profiles); the top one dominates on CS works (270 vs 45), which is not enough for a common name | `A501` Yu Cheng; 300 works; CS 90%; AI 0%; 5 recent; Univ A | `A502` Yu Cheng; 50 works; CS 90%; AI 0%; 5 recent; Univ B | `A503` Yu \| Cheng; 10 works; CS 50%; AI 0%; 5 recent; Univ C |
| Cy Dorn |  | the only active name match works outside AI-adjacent fields (adjacent share 0% < 30%) | `A301` Cy Dorn; 40 works; CS 0%; AI 0%; 5 recent; Hospital X |  |  |

## Not found (1)

No accepted OpenAlex profile with a matching name; the name is not swept by id.

| Name | C4 | Reason | Candidate 1 | Candidate 2 | Candidate 3 |
|---|---|---|---|---|---|
| Di Evans |  | no profile with a matching name among 0 |  |  |  |

## Newly resolved by the coauthor rule (1)

Ids written to the roster in this run; candidates 1-3 are the accepted profiles (the ids column of the roster has them all).

| Name | C4 | How | Reason | Candidate 1 | Candidate 2 | Candidate 3 |
|---|---|---|---|---|---|---|
| Bo Chen |  | coauthors | the only coauthor-linked person: `A203` has 2 roster coauthors (Ann Lee, Ed Fox) over 2 works | `A203` Bo Chen; 5 works; CS 80%; AI 0%; 3 recent; Some University; 2 roster coauthors (Ann Lee, Ed Fox) over 2 works |  |  |

## Split profiles added to resolved names (0)

Same person by the split rule (shared roster coauthors or the same ORCID); accepted profile first.

None.

## Check: resolved without roster coauthors and with a low AI share (0)

Resolved by the name rule; the accepted profile shares no work since 2019-01-01 with a resolved roster person and has an AI share below 20%. Not changed: a human look.

None.

## Removed: merged profiles (0 ids, 0 names)

The guard: a profile with more than 100 works in 2025 or 2026, or with ≥ 40 works in 2026 and ≥ 4x max(works in 2025, 5), has absorbed namesakes; its id is removed from the roster (old ids too) and is no coauthor evidence. A name left without ids is ambiguous. Yearly works from `counts_by_year`; "fetched since 2025-10": its works in the coauthor works cache dated from 2025-10-01.

None.

## Resolved common names (spot-check) (0)

Resolved names that are on the `disambiguate` list or common; the accepted id is candidate 1.

None.

## Possible split profiles (0)

Resolved names with a further profile of the same name that no rule added: a second active CS profile (name rule), or a coauthor-linked one sharing the accepted profiles' roster coauthors but < 33% of its works since 2019-01-01 (coauthor rule). One person split by OpenAlex, or a namesake's profile holding some of the person's works. Add the id when it is the same person.

None.

