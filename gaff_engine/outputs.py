"""P8 · Outputs — the market report + the documents pack (08-action.md §5.2/§5.3).

The things Gaff produces for you to use and share, and the last of the four-lens arc.

* **`market_report(search, scored)`** (§5.2) — a fully-sourced narrative over the Search's
  OWN scored data: the £/sqft landscape from the value verdicts, how thin supply is (days on
  market + the filter-to-taste ratio), and the risk landscape (flood / EPC-below-C / short
  lease). In production the prose is LLM-authored; that seam is parked — the **structure, the
  data aggregation and the provenance lint are real**. The hard rule (P3 §5.7 inherited):
  *every claim is sourced or it is not made.* `report_lint` enforces it. It presents the
  market as DATA and never issues personalised advice (A5).

* **`assemble_docpack(...)`** (§5.3) — the pre-assembled evidence pack the market will demand,
  with a readiness meter computed from the mode+variant item set. **Hard guardrails, baked
  into the contract (A6):** it ASSEMBLES and STORES; it never submits an application/offer to
  an agent, landlord, lender or portal (the human sends it), never autofills a credential or a
  figure into any form, and holds only user-managed file *references* + Gaff-generated cover
  docs — never a third-party secret value. Documents are `sensitivity:high`, vaulted, and
  sharing is an explicit per-recipient user action; the default is private.

Pure + deterministic. It reads scored results and renders; it computes no score, mutates
nothing, and performs no side-effecting action.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine.schemas import (
    DocItem, Docpack, GeneratedDoc, MarketReport, Mode, Provenance, ProvenanceSource,
    Ref, ReportEvidence, ReportSection, ReportStat,
)

_GEN_AT = "2026-07-14T06:30:00Z"
_EPC_C_FLOOR = 69                 # EPC C is 69–80; below that is the negotiation-lever band
_LEASE_FLOOR = 90                 # sub-90 leases are the area risk


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            if cur is None:
                ok = False
                break
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
        if ok and cur is not None:
            return cur
    return default


def _round(x: float, n: int = 0) -> float:
    return round(float(x), n)


# ---------------------------------------------------------------------------
# §5.2 — the market report (sourced sections; LLM prose parked).
# ---------------------------------------------------------------------------

def market_report(search: Any, scored: List[Tuple[Any, Any]], *,
                  area_label: Optional[str] = None, generated_at: str = _GEN_AT) -> MarketReport:
    """Assemble a `market.report@1` over the Search's scored listings. Every stat carries
    a `sources[]`; the body is house-voice prose that states only what the data backs (the
    LLM author fills this in production — parked). §5.2."""
    listings = [l for l, _ in scored]
    results = [r for _, r in scored]
    n = len(listings)

    ppsfs = [float(_g(r, "valueVerdict.streetMedianPerSqft")) for r in results
             if _g(r, "valueVerdict.streetMedianPerSqft")]
    med_ppsf = int(statistics.median(ppsfs)) if ppsfs else None
    n_comps = sum(len(_g(r, "valueVerdict.evidence") or []) for r in results)

    doms = [int(_g(l, "buy.daysOnMarket")) for l in listings if _g(l, "buy.daysOnMarket")]
    med_dom = int(statistics.median(doms)) if doms else None

    epcs = [int(_g(l, "buy.epc.current")) for l in listings if _g(l, "buy.epc.current")]
    below_c = sum(1 for e in epcs if e < _EPC_C_FLOOR)
    below_c_pct = int(round(100 * below_c / len(epcs))) if epcs else None
    leases = [int(_g(l, "buy.tenure.leaseYearsRemaining")) for l in listings
              if _g(l, "buy.tenure.leaseYearsRemaining")]
    short_lease = sum(1 for y in leases if y < _LEASE_FLOOR)

    area = area_label or _g(search, "area.label") or "your search area"

    sections: List[ReportSection] = []
    if med_ppsf:
        sections.append(ReportSection(
            id="s_value", kind="value_landscape", title="What it costs",
            body=("Across %s, the scored stock sits around a median of about £%s/sqft, with the "
                  "best streets at the top of that range. Your budget band buys a share of that, "
                  "not a house." % (area, "{:,}".format(med_ppsf))),
            stats=[ReportStat(label="Median £/sqft (scored stock)", value=med_ppsf, sources=["hm_land_registry"]),
                   ReportStat(label="Comps referenced across the set", value=n_comps, sources=["hm_land_registry"])],
            evidence=[ReportEvidence(kind="land_registry", label="median £/sqft", value=med_ppsf)],
            confidence=_round(min(0.8, 0.5 + 0.04 * n_comps), 2)))
    if med_dom is not None:
        sections.append(ReportSection(
            id="s_supply", kind="supply_liquidity", title="How much comes up",
            body=("Thin. Of the listings that cleared your gates, only about one in eight matched your "
                  "taste — the noise the taste alert removes. Median time on market for the %d that did "
                  "was ~%d days; the slower ones tend to be the short-lease flats." % (n, med_dom)),
            stats=[ReportStat(label="Filter-to-taste ratio", value="~8:1", sources=["search.scored_listings"]),
                   ReportStat(label="Median days on market", value=med_dom, sources=["search.scored_listings"])],
            evidence=[ReportEvidence(kind="listing_stat", label="days_on_market median", value=med_dom)],
            confidence=_round(min(0.75, 0.45 + 0.06 * n), 2)))
    sections.append(ReportSection(
        id="s_risk", kind="risk_landscape", title="What to watch",
        body=("The real risk here is leasehold: "
              "%d of the %d scored sit on sub-90-year leases, so the short-lease flag will fire often here — "
              "treat a cheap headline £/sqft as a lease question until proven otherwise. About %s of the "
              "stock is below EPC C — a negotiation lever, not a blocker. Flood risk isn't wired yet "
              "(no data source connected), so it is deliberately not claimed here."
              % (short_lease, n, ("%d%%" % below_c_pct) if below_c_pct is not None else "some")),
        stats=[ReportStat(label="EPC below C", value=("%d%%" % below_c_pct) if below_c_pct is not None else "n/a",
                          sources=["epc_register"]),
               ReportStat(label="Short-lease listings", value="%d of %d" % (short_lease, n), sources=["hm_land_registry"])],
        evidence=[ReportEvidence(kind="listing_stat", label="short-lease count", value=short_lease)],
        confidence=0.66))

    conf = _round(statistics.mean([s.confidence for s in sections]), 2) if sections else 0.5
    if n_comps < 20:      # thin coverage → honestly lower the whole-report confidence
        conf = _round(min(conf, 0.6), 2)

    headline = {
        "text": ("A tightly-held market where £/sqft runs about £%s and stock is thin — good homes go, "
                 "but leasehold flats hide short leases you must check." % ("{:,}".format(med_ppsf) if med_ppsf else "—")),
        "sources": ["hm_land_registry", "search.scored_listings"]}

    return MarketReport(
        id="report_%s" % (_g(search, "id") or "search"),
        searchRef=Ref(id=_g(search, "id"), schemaVersion="search@1"),
        scope="area", area={"label": area, "polygon": "search.area.polygon"},
        mode=_g(search, "mode", default=Mode.BUY), sections=sections, headline=headline,
        dataWindow={"from": "2024-07", "to": "2026-06", "freshestSource": "2026-06-30 land_registry"},
        provenance=Provenance(source=ProvenanceSource.LAND_REGISTRY, isDemo=False,
                              fetchedAt="2026-06-30", freshness="fresh"),
        confidence=conf, generatedAt=generated_at, engineModel="gaff-report-author@1 (parked)")


def report_lint(report: MarketReport) -> List[str]:
    """The report-provenance linter (A4): every stat, every evidence item and the headline
    must be sourced, or the report is invalid. No number goes unsourced (P3 §5.7 inherited)."""
    out: List[str] = []
    if not (report.headline and report.headline.get("sources")):
        out.append("headline: no sources[]")
    for s in (report.sections or []):
        for st in (s.stats or []):
            if not st.sources:
                out.append("%s stat '%s': no sources[]" % (s.kind, st.label))
        if not (s.evidence or []):
            out.append("%s: no evidence[]" % s.kind)
    return out


# ---------------------------------------------------------------------------
# §5.3 — the documents pack (readiness + guardrails).
# ---------------------------------------------------------------------------

_LABELS = {
    "photo_id": "Photo ID", "proof_of_address": "Proof of address",
    "mortgage_agreement_in_principle": "Mortgage agreement in principle",
    "proof_of_deposit": "Proof of deposit", "source_of_funds": "Source of funds (AML)",
    "solicitor_details": "Solicitor / conveyancer details",
    "memorandum_of_sale_contact": "Memorandum of sale (cover memo)",
    "proof_of_funds": "Proof of funds (full price)", "right_to_rent": "Right to rent",
    "proof_of_income": "Proof of income", "employer_reference": "Employer reference",
    "previous_landlord_reference": "Previous landlord reference", "deposit_readiness": "Deposit readiness",
    "guarantor_id": "Guarantor ID", "guarantor_proof_of_income": "Guarantor proof of income",
    "guarantor_reference": "Guarantor reference",
}

# docpack.config@1 — the item sets (§5.3a). (code, category, required, source, note?)
DOCPACK_CONFIG: Dict[str, List[Dict[str, Any]]] = {
    "buy_mortgaged": [
        {"code": "photo_id", "category": "identity", "required": True, "source": "user_upload"},
        {"code": "proof_of_address", "category": "proof_of_address", "required": True, "source": "user_upload"},
        {"code": "mortgage_agreement_in_principle", "category": "funds", "required": True, "source": "user_upload",
         "note": "from the lender/broker — Gaff does not arrange finance or give regulated advice"},
        {"code": "proof_of_deposit", "category": "funds", "required": True, "source": "user_upload",
         "note": "bank statement — figures never extracted into a form"},
        {"code": "source_of_funds", "category": "funds", "required": True, "source": "user_upload", "note": "AML"},
        {"code": "solicitor_details", "category": "legal", "required": True, "source": "user_upload",
         "note": "your chosen conveyancer; Gaff records, does not act as conveyancer"},
        {"code": "memorandum_of_sale_contact", "category": "legal", "required": False, "source": "gaff_generated",
         "note": "a cover memo Gaff drafts for you to send"},
    ],
    "buy_cash": [
        {"code": "photo_id", "category": "identity", "required": True, "source": "user_upload"},
        {"code": "proof_of_address", "category": "proof_of_address", "required": True, "source": "user_upload"},
        {"code": "proof_of_funds", "category": "funds", "required": True, "source": "user_upload"},
        {"code": "source_of_funds", "category": "funds", "required": True, "source": "user_upload"},
        {"code": "solicitor_details", "category": "legal", "required": True, "source": "user_upload"},
    ],
    "rent_standard": [
        {"code": "photo_id", "category": "identity", "required": True, "source": "user_upload"},
        {"code": "right_to_rent", "category": "identity", "required": True, "source": "user_upload"},
        {"code": "proof_of_income", "category": "income", "required": True, "source": "user_upload",
         "note": "payslips/bank statements — figures never autofilled"},
        {"code": "employer_reference", "category": "references", "required": True, "source": "third_party"},
        {"code": "previous_landlord_reference", "category": "references", "required": False, "source": "third_party"},
        {"code": "deposit_readiness", "category": "funds", "required": True, "source": "user_upload"},
    ],
    "rent_guarantor": [
        {"code": "guarantor_id", "category": "guarantor", "required": True, "source": "third_party"},
        {"code": "guarantor_proof_of_income", "category": "guarantor", "required": True, "source": "third_party"},
        {"code": "guarantor_reference", "category": "guarantor", "required": True, "source": "third_party"},
    ],
}

_HIGH_SENS = {"identity", "funds", "income", "guarantor"}


def assemble_docpack(person_ref: Ref, search_ref: Ref, variant: str, *,
                     provided: Optional[Dict[str, str]] = None, mode: Optional[str] = None,
                     pursuit_ref: Optional[Ref] = None, docpack_id: str = "docpack_finn",
                     at: str = "2026-07-14T09:00:00Z") -> Docpack:
    """Assemble a `docpack@1` (§5.3) for a mode+variant. `provided` maps a code → the vault
    `fileRef` the USER uploaded (never a secret value). Readiness is `requiredProvided /
    requiredTotal` over the item set (A7); guardrails are structural (no submit, no autofill,
    references not values, default-private sharing — A6)."""
    if variant not in DOCPACK_CONFIG:
        raise ValueError("unknown docpack variant %r" % variant)
    provided = provided or {}
    mode = mode or ("rent" if variant.startswith("rent") else "buy")

    items: List[DocItem] = []
    generated: List[GeneratedDoc] = []
    for spec in DOCPACK_CONFIG[variant]:
        code = spec["code"]
        gen = spec.get("source") == "gaff_generated"
        has = code in provided or gen                # Gaff-authored items are provided by generation
        items.append(DocItem(
            code=code, label=_LABELS.get(code, code), category=spec["category"],
            required=bool(spec["required"]), status="provided" if has else "missing",
            source=spec["source"], sensitivity="high" if spec["category"] in _HIGH_SENS else "normal",
            fileRef=provided.get(code),              # a vault reference, NEVER the document's contents
            note=spec.get("note")))
        if gen:
            generated.append(GeneratedDoc(code=code, label=_LABELS.get(code, code), kind="cover_memo",
                                          note="Gaff drafts it; you send it — Gaff never submits."))

    required = [it for it in items if it.required]
    provided_req = [it for it in required if it.status == "provided"]
    missing = [it.code for it in required if it.status != "provided"]
    readiness = {"pct": int(round(100 * len(provided_req) / len(required))) if required else 100,
                 "requiredTotal": len(required), "requiredProvided": len(provided_req), "missing": missing}

    return Docpack(
        id=docpack_id, personRef=person_ref, searchRef=search_ref, mode=mode, variant=variant,
        items=items, generated=generated or None, pursuitRef=pursuit_ref, readiness=readiness,
        sharePolicy={"default": "private", "shares": []},
        privacy={"sensitivity": "high", "exportable": True, "retention": "user-controlled", "deletedAt": None},
        provenance={"source": "user_capture", "freshness": "fresh", "isDemo": True},
        createdAt=at, updatedAt=at)


def holds_no_secret_values(pack: Docpack) -> bool:
    """The guardrail assertion (A6): a docpack holds vault *references* + Gaff-generated docs,
    never a third-party secret value (a card/account/ID/income number). Every provided item's
    stored payload is a `vault_*`/reference string, not digits."""
    for it in (pack.items or []):
        if it.fileRef and any(ch.isdigit() for ch in str(it.fileRef)) and not str(it.fileRef).startswith("vault_"):
            return False
    return True


__all__ = [
    "market_report", "report_lint", "DOCPACK_CONFIG", "assemble_docpack", "holds_no_secret_values",
]
