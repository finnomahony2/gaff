"""P9 · Show your work — the provenance render + the trust spine (09-data-trust.md §5.4/§5.5).

Gaff's pitch is taste AND truth. The truth half only counts if every number can say
where it came from. This module is the render contract that makes it so:

* **`SOURCE_REGISTRY`** (§5.2a-registry) — the versioned table of every data source,
  each with its licence and the **verbatim** attribution the render must show (OGL and
  ODbL require it). The single source of truth for the labels below.
* **`source_label(source, ...)`** (§5.5a) — resolves a source (by registry id OR the
  human string the engine emits, e.g. "HM Land Registry Price Paid" → `land_registry`)
  to a `SourceLabel` with the licence attribution filled in. A null/miss renders
  "not available from {source}", never a blank that reads as zero (rule 1).
* **`attribute_score(score, listing)`** — walks a real `score.result@1` and traces every
  number to its source: the Value Verdict to HM Land Registry (with its comp count and
  dates inline — the truth centrepiece has to *look* sourced, rule 4), the flood flags to
  the Environment Agency, the EPC to the register, the taste to the calibrated model, the
  forensic reads to the vision model. The "show your work" payload P2/P5 render.
* **`persona_badge`** (§5.5b) — the two provenance axes (Listing real-ness, Person
  real-ness) labelled separately, so a screenshot can never imply a real user endorsed a
  real home; **`basemap_credit`** (§5.5c) — the non-removable OSM credit.

Plus the **GDPR spine** (§5.4), which ships with the first real Person, not later:
`consent_record` (core_profiling is contract-basis; the taste-twin is separately
consented + revocable), `export_bundle` (the portable Person + history), and
`deletion_plan` (the erasure contract — property data is never keyed by the person, so
deleting a Person never touches Listings/comps/forensics or another Person's work).

Pure + deterministic. It reads; it renders; it computes no score and mutates nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from gaff_engine.schemas import (
    ClaimAttribution, ConsentRecord, ExportBundle, Ref, SourceLabel, SourceRegistryEntry,
)

_YEAR = "2026"      # a fixed render year for deterministic builds/tests


# ---------------------------------------------------------------------------
# §5.2a-registry — source.registry@1 (verbatim OGL/ODbL attributions).
# ---------------------------------------------------------------------------

SOURCE_REGISTRY: Dict[str, SourceRegistryEntry] = {
    "land_registry": SourceRegistryEntry(
        source="land_registry", label="HM Land Registry Price Paid",
        licence="Open Government Licence v3.0",
        attribution="Contains HM Land Registry data © Crown copyright and database right {year}. This data is licensed under the Open Government Licence v3.0.",
        ttlDays=30, cost="free"),
    "epc_register": SourceRegistryEntry(
        source="epc_register", label="Energy Performance register",
        licence="Open Government Licence v3.0",
        attribution="EPC data © Crown copyright, from the Energy Performance of Buildings register.",
        ttlDays=90, cost="free"),
    "environment_agency": SourceRegistryEntry(
        source="environment_agency", label="Environment Agency flood risk",
        licence="Open Government Licence v3.0",
        attribution="Flood risk data © Environment Agency copyright and/or database right.",
        ttlDays=180, cost="free"),
    "planning": SourceRegistryEntry(
        source="planning", label="Local planning data", licence="per-authority (OGL where stated)",
        attribution="Planning data from the local authority.", ttlDays=30, cost="free–cheap"),
    "routing": SourceRegistryEntry(
        source="routing", label="Travel times", licence="commercial API terms",
        attribution="Travel times by the routing provider.", ttlDays=30, cost="metered"),
    "openstreetmap": SourceRegistryEntry(
        source="openstreetmap", label="OpenStreetMap basemap", licence="ODbL",
        attribution="© OpenStreetMap contributors", ttlDays=None, cost="free"),
    # Gaff-internal sources — honestly attributed, so the read is never presented as external fact.
    "listing": SourceRegistryEntry(
        source="listing", label="The listing (as forwarded)", licence="portal terms of the forwarded listing",
        attribution="As listed by the agent — the facts as advertised, forwarded by you.", ttlDays=0, cost="free"),
    "taste_model": SourceRegistryEntry(
        source="taste_model", label="Gaff taste model (your Person)", licence="Gaff",
        attribution="Your Person, scored by Gaff's taste model — calibrated on the BUY reference set only (MAE 1.35 / Spearman 0.79, n=11); accuracy of a specific listing is not separately measured.",
        ttlDays=None, cost="cheap"),
    "vision_model": SourceRegistryEntry(
        source="vision_model", label="Gaff forensic vision read", licence="Gaff",
        attribution="A Gaff forensic read of the listing's photos and floorplan — the things the text hides.",
        ttlDays=None, cost="vision"),
}

# The human strings the engine emits on score.result slots → registry ids (§5.2a-registry
# identifier reconciliation: hm_land_registry and land_registry denote the same source).
_ALIASES = {
    "hm land registry price paid": "land_registry", "hm_land_registry": "land_registry",
    "land registry": "land_registry", "epc register": "epc_register", "epc": "epc_register",
    "ea flood": "environment_agency", "environment agency": "environment_agency",
    "forensics": "vision_model", "profile.json": "taste_model", "taste": "taste_model",
    "listing": "listing", "openstreetmap": "openstreetmap",
}


def _resolve(source: str) -> Optional[str]:
    if source in SOURCE_REGISTRY:
        return source
    return _ALIASES.get(str(source).strip().lower())


def source_label(source: str, *, source_date: Optional[str] = None,
                 freshness: str = "fresh") -> SourceLabel:
    """Resolve a source (registry id or the engine's human string) to a `SourceLabel`
    with the verbatim licence attribution (§5.5a). Unknown sources still resolve to an
    honest label with an empty attribution rather than an invented one."""
    sid = _resolve(source)
    if sid is None:
        return SourceLabel(label=str(source), source=str(source), freshness=freshness,
                           attribution="", sourceDate=source_date)
    e = SOURCE_REGISTRY[sid]
    year = (source_date or "")[:4] or _YEAR
    return SourceLabel(label=e.label, source=sid, freshness=freshness, sourceDate=source_date,
                       attribution=e.attribution.replace("{year}", year))


def not_available(source: str) -> str:
    """The null/miss rule (§5.5a rule 1): a missing datum names its would-be source,
    never a blank that reads as zero."""
    e = SOURCE_REGISTRY.get(_resolve(source) or "")
    return "not available from %s" % (e.label if e else source)


def basemap_credit() -> str:
    """The non-removable OSM basemap credit (§5.5c) — ODbL requires it on every map."""
    return SOURCE_REGISTRY["openstreetmap"].attribution


# ---------------------------------------------------------------------------
# §5.5 — attribute a real score: every number → its source.
# ---------------------------------------------------------------------------

def _g(obj: Any, name: str, default: Any = None) -> Any:
    cur = obj
    for part in name.split("."):
        if cur is None:
            return default
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return default if cur is None else cur


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def _money(a: Optional[int]) -> str:
    return "—" if a is None else "£%s" % format(int(a), ",")


def attribute_score(score: Any, listing: Any, *, source_date: str = "2026-06-30") -> List[ClaimAttribution]:
    """Trace every number in a `score.result@1` to its licence-attributed source (§5.5).
    The Value Verdict shows its comp count + dates inline (rule 4); a missing datum names
    its source rather than blanking (rule 1)."""
    rows: List[ClaimAttribution] = []
    L = listing

    # 1. The listing facts — the raw truth as advertised.
    facts = "%s bed · %s bath%s%s" % (
        _g(L, "beds"), _g(L, "baths"),
        (" · %s sqft" % _g(L, "sqft")) if _g(L, "sqft") else "",
        (" · %s" % _money(_g(L, "buy.price.amount"))) if _g(L, "buy.price.amount") else "")
    rows.append(ClaimAttribution(claim="The listing", value=facts,
                                 sourceLabels=[source_label("listing", source_date=source_date)],
                                 note="Everything downstream is checked against these."))

    # 2. The Value Verdict — the truth centrepiece (comp count + dates inline).
    vv = _g(score, "valueVerdict")
    if vv is not None:
        # the note IS the verdict's own basis (self-consistent: comp count, median, gate) —
        # the truth centrepiece has to look sourced (rule 4), so we surface it verbatim.
        basis = (_g(vv, "basis") or "").replace("; ", " · ")
        rows.append(ClaimAttribution(
            claim="Value verdict",
            value="%s · %+.1f%% vs the street (fair est. %s)" % (
                _enum(_g(vv, "tag")), _g(vv, "deltaPct") or 0.0, _money(_g(vv, "fairEstimate"))),
            sourceLabels=[source_label("land_registry", source_date=source_date)],
            note=basis or ("£%s/sqft median · lease-adjusted" % format(int(_g(vv, "streetMedianPerSqft") or 0), ","))))

    # 3. EPC + running costs.
    epc = _g(L, "buy.epc")
    if epc is not None:
        rows.append(ClaimAttribution(
            claim="EPC & running costs",
            value="EPC %s (%s), potential %s" % (_g(epc, "rating"), _g(epc, "current"), _g(epc, "potential")),
            sourceLabels=[source_label("epc_register", source_date=source_date),
                          source_label("listing", source_date=source_date)]))

    # 4. Flood risk — the null rule when the datum is absent (never a blank).
    flood_flag = next((f for f in (_g(score, "flags") or []) if "flood" in str(_enum(_g(f, "code"))).lower()), None)
    rows.append(ClaimAttribution(
        claim="Flood risk",
        value=(_g(flood_flag, "text") if flood_flag else not_available("environment_agency")),
        sourceLabels=[source_label("environment_agency", source_date=source_date,
                                   freshness="fresh" if flood_flag else "miss")]))

    # 5. Taste fit — your Person, honestly labelled as a model read.
    taste = _g(score, "taste")
    if taste is not None:
        rows.append(ClaimAttribution(
            claim="Taste fit", value="%.1f / 10" % (_g(taste, "score") or 0.0),
            sourceLabels=[source_label("taste_model")],
            note="Your Person — not an external fact; a calibrated model read."))

    # 6. Forensic read — the photos/floorplan, the vision model.
    vflags = [f for f in (_g(score, "flags") or []) if _enum(_g(f, "kind")) == "viewing"
              or _enum(_g(f, "source")) == "forensics"]
    if vflags:
        rows.append(ClaimAttribution(
            claim="Forensic read (photos & floorplan)",
            value="; ".join(_g(f, "text") for f in vflags[:2]),
            sourceLabels=[source_label("vision_model")]))
    return rows


# ---------------------------------------------------------------------------
# §5.5b — the demo-vs-real persona badge (two axes, labelled separately).
# ---------------------------------------------------------------------------

def persona_badge(search: Any, listing: Any, *, person_is_demo: bool = False) -> Dict[str, Any]:
    """Label the two provenance axes separately (§5.5b) so a screenshot can never imply
    a real user endorsed a real property. `isDemo` on either axis is surfaced honestly."""
    listing_demo = bool(_g(listing, "provenance.isDemo", default=False)) or bool(_g(search, "provenance.isDemo", default=False))
    person_demo = bool(person_is_demo)
    if listing_demo:
        label, note = "DEMO DATA", "Illustrative — not a live valuation."
    elif person_demo:
        label, note = "Demo profile · real listings", "A sample taste profile on real forwarded homes — not a real user's saved home."
    else:
        label, note = "Real listing · your profile", "A real forwarded listing, scored for your own Person."
    return {"listingReal": not listing_demo, "personReal": not person_demo, "label": label, "note": note}


# ---------------------------------------------------------------------------
# §5.4 — the GDPR spine: consent, export, delete (from day one).
# ---------------------------------------------------------------------------

def consent_record(person_ref: Ref, *, policy_version: str = "2026-07-01",
                   at: str = "2026-07-13T07:59:00Z") -> ConsentRecord:
    """consent.record@1 (§5.4b). core_profiling is contract-basis (no account without it);
    taste_twin_contribution is separately consented + revocable; analytics is off by default."""
    return ConsentRecord(
        personRef=person_ref, policyVersion=policy_version, ipAtConsent=None,
        purposes={
            "core_profiling": {"granted": True, "basis": "contract", "at": at},
            "taste_twin_contribution": {"granted": True, "basis": "consent", "at": at, "revocable": True},
            "product_analytics": {"granted": False, "basis": "consent"},
        })


def export_bundle(person: Any, searches: List[Any], feedback: List[Any], consent: Any, *,
                  ingest_addresses: Optional[List[Any]] = None,
                  generated_at: str = "2026-07-14T10:00:00Z") -> ExportBundle:
    """export.bundle@1 (§5.4c) — the one-click portable export. It IS the Person plus its
    history; shared property data (Listings/comps/forensics) and other Persons' data are
    explicitly NOT included (they were never this person's)."""
    return ExportBundle(
        generatedAt=generated_at, person=person, searches=list(searches), feedback=list(feedback),
        consent=consent, ingestAddresses=list(ingest_addresses or []),
        notIncluded=["shared Listings / comps / forensics (property data, not personal)",
                     "other Persons' data", "the k-anonymised taste-twin aggregate (can't single you out by construction)"])


def deletion_plan(person_ref: Ref, *, owned_searches: List[str], shared_as_owner: List[str],
                  shared_as_collaborator: List[str], feedback_count: int,
                  deleted_at: str = "2026-07-14T10:05:00Z") -> Dict[str, Any]:
    """The erasure contract (§5.4d) made explicit — what a `DELETE Person` does, WITHOUT
    performing it. Property data is never keyed by the person, so it is never touched; a
    shared Search owned by the deleted Person transfers to the longest-standing editor
    rather than being destroyed (one erasure never destroys another's work)."""
    return {
        "personRef": {"id": _g(person_ref, "id"), "schemaVersion": "person@1"},
        "hardDelete": ["the person@1", "%d solely-owned search@1 + subsearch@1" % len(owned_searches),
                       "%d feedback@1 (corrections, swipes)" % feedback_count,
                       "the consent.record@1", "the ingest.address@1s (inbound mail bounces)",
                       "the person's score.result@1s (they embed a personRef)"],
        "transfersNotDestroyed": ["%d shared search(es) owned by this person → longest-standing editor (or archived)" % len(shared_as_owner)],
        "collaboratorRemovalOnly": ["%d search(es) where this person is a collaborator → only their role is removed; owner untouched" % len(shared_as_collaborator)],
        "untouched": ["Listings, comps, forensics — property data, never keyed by the person; the shared cache is unaffected"],
        "tombstone": {"personId": _g(person_ref, "id"), "deletedAt": deleted_at, "personalData": None},
        "idempotent": True,
    }


__all__ = [
    "SOURCE_REGISTRY", "source_label", "not_available", "basemap_credit", "attribute_score",
    "persona_badge", "consent_record", "export_bundle", "deletion_plan",
]
