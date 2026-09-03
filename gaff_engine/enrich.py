"""U9-live — on-demand comp enrichment for ANY street (M4, 09-data-trust §5.1e).

Phase 1's value verdict was real but only for the eight De Beauvoir streets we
pre-cached (``data/comps_enriched.json``). This is the ``enrich()`` step of the
ingestion pipeline generalised: given a Listing on *any* street, fetch its street's
HM Land Registry Price Paid comps (live, cached under ``data/comps/``) and attach
EPC £/sqft (live, cached in the user EPC cache; nothing EPC ships) — the like-for-like set the Value
scorer anchors on. Composes the two adapters U9 (:func:`landreg.comps_for_listing`)
and EPC (:func:`epc.enrich_comps`) already own; both cache to disk, so a street is
fetched once and every re-score reads the cache.

The engine stays offline-by-default (``score(..., comps=None)`` loads the reconciled
De Beauvoir cache, so the golden + the whole test suite never touch the network);
live scoring of a *new* listing passes ``comps=enrich_for_listing(listing)``
explicitly. This keeps the network boundary opt-in and the deterministic path pure
(00-frame: forensics/enrich are the network layer; the engine core is pure).
"""

from __future__ import annotations

from typing import Any, List

from gaff_engine.epc import enrich_comps
from gaff_engine.landreg import comps_for_listing


def enrich_for_listing(listing: Any, *, since_year: int = 2021, town: str = "LONDON",
                       matched_only: bool = True, offline: bool = False,
                       force: bool = False) -> List[Any]:
    """The on-demand comp set for a Listing's own street (+ nearby), EPC-enriched.

    1. :func:`landreg.comps_for_listing` — the street's Price Paid transactions
       (cached ``data/comps/<street>.json``), the subject's street tagged
       ``"same street"`` so the U3 subject-relative anchor picks it.
    2. :func:`epc.enrich_comps` — attaches ``pricePerSqft`` from the EPC register
       (cached in the user EPC cache); ``matched_only`` keeps the comps that matched a
       floor area (the set the Value scorer can actually use).

    ``offline=True`` reads only what is already cached (no network) — the path the
    tests use. Live (default) fetches any un-cached street once.
    """
    raw = comps_for_listing(listing, since_year=since_year, town=town,
                            offline=offline, force=force)
    enriched = enrich_comps(raw, offline=offline, force=force)
    if matched_only:
        enriched = [c for c in enriched if getattr(c, "pricePerSqft", None) is not None]
    return enriched


__all__ = ["enrich_for_listing"]
