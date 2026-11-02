"""``overrides`` (design v4.1 §8, D336): one human or generated correction, or the revoke of one.
Key ``override_id`` (unique in the chain, so the view is the union of every row). One chain,
``overrides``, written only by ``overrides.add`` (:mod:`stack.overrides`).

``target`` ``identity`` | ``fields`` | ``manual_sources`` | ``<wf>``; ``key`` the doc id, source
id or source key (an identity ``merge``/``separate``: the canonical JSON of its sorted source ids;
a manual source: ``<kind>=<value>`` of its URL's first typed key); ``item_key`` the second key part
(``<wf>``: the item, ``fields``: ``doc_id`` or ``source_id``, which id ``key`` is; else ``''``);
``kind`` ``row`` | ``field`` | ``quarantine`` | ``revoke``; ``field`` (``''`` for a whole row);
``payload`` JSON text (the value and the per-target extras); ``scope_members`` /
``scope_basis_digest`` (``<wf>`` only); ``tier`` ``human`` | ``ai_check``; ``revokes`` the id a
``revoke`` row retires."""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.blocks import LS, TS, S, fields
from stack.schemas.families import FamilySpec

SPEC = FamilySpec(
    name="overrides",
    kind="overrides",
    schema=pa.schema(
        fields(
            override_id_=S,
            target_=S,
            key_=S,
            item_key_=S,
            kind_=S,
            field_=S,
            payload_=S,
            scope_members=LS(S),
            scope_basis_digest=S,
            tier_=S,
            author=S,
            created_at_=TS,
            revokes=S,
        )
    ),
    key=("override_id",),
)
