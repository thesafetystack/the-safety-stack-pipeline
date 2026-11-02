"""``AFFILIATION`` payload (A.4): item grain, one item per byline author (``item_key``)."""

from __future__ import annotations

from stack.schemas.structs import AFFIL, LS, ST, I, S

AFFILIATION_ITEM = ST(
    item_key_=S,
    author_name=S,
    author_surface=S,
    byline_index=I,
    affiliations=LS(AFFIL),
    orcid=S,
    resolution=S,
)
AFFILIATION = ST(items=LS(AFFILIATION_ITEM))

# ``AFFILIATION_ORGS`` (D736): the organisations ``stack.orgvocab`` resolves from one
# document's ``AFFILIATION`` payload, per item and per affiliation in the same order (joined to ``AFFILIATION`` by
# ``item_key`` and position; no surface is repeated here), plus the document's distinct ids (D737: no roots).
AFFIL_ORGS = ST(status_=S, org_ids_=LS(S), ambiguous_=LS(LS(S)))
AFFILIATION_ORGS_ITEM = ST(item_key_=S, byline_index=I, affiliations_=LS(AFFIL_ORGS))
AFFILIATION_ORGS = ST(items=LS(AFFILIATION_ORGS_ITEM), org_ids=LS(S), vocab_version=S)
