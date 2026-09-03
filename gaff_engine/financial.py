"""U-financial — the Financial scorer for invest mode (03-engine.md §5.4).

For `mode:invest` the value slot of the Mix is filled by the Financial scorer instead
of the Value Verdict — the fourth product's own value source. Where buy reads HM Land
Registry sold comps and rent reads the asking-rent spread, invest reads the **yield**:
what the rent returns on the price, net of costs, and how that compares to the local
BTL yield median.

The arithmetic (§5.4, grounded in `data/wales_deals.json`):
    grossYieldPct   = (estRentPcm × 12) / price × 100
    annualCosts     = annualRent × (voidWeeks/52 + mgmtPct/100 + maintenancePct/100)
    netYieldPct     = (annualRent − annualCosts) / (price + estRefurbCost) × 100
    monthlyCashflow = estRentPcm − annualCosts/12          # financing not modelled (OQ 8.6)

The verdict reuses the `value_verdict@1` shape (so the Mix consumes it unchanged) but its
`steal|fair|over` tag is the **financial analogue**: a yield well ABOVE the local median is
a `steal` (a strong deal), well below is `over` (weak). This inverts the buy/rent direction
(there, low PRICE vs the street is the steal) — for a landlord, high yield is the good news.
Financing, the void model and the invest confidence curve are DEFERRED (§5.4 / OQ 8.6): this
proves the yield arithmetic and gives invest a real value slot, not a calibrated verdict.
"""

from __future__ import annotations

import json
import pathlib
import statistics
from typing import Any, Dict, List, Optional

from gaff_engine import paths
from gaff_engine.schemas import (
    Address, InvestDetails, InvestFinancials, Listing, Mode, Money, MoneyPeriod,
    PropertyType, Provenance, ProvenanceSource, ValueBand, ValueEvidence, ValueVerdict,
)

# Resolved through gaff_engine.paths. A lab checkout carries the real research
# pool (wales_deals.json); a public install carries the synthetic demo pool
# (invest_pool.json), so the scorer has something to compare against either way.
def _pool_path():
    return (paths.data_file("wales_deals.json")
            or paths.data_file("invest_pool.json"))

# Standard BTL cost assumptions (§5.4; the void/financing model is DEFERRED, OQ 8.6).
COSTS = {"voidWeeks": 3.0, "mgmtPct": 10.0, "maintenancePct": 5.0}

# The financial analogue of steal/over: yield vs the local median, in relative %.
_STRONG_AT = 12.0     # yield >= median + 12% (relative) → steal
_WEAK_AT = -12.0      # yield <= median − 12% (relative) → over
_MIN_COHORT = 3


def _round(x: float, n: int = 1) -> float:
    return round(float(x), n)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


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


def load_invest_pool() -> List[Dict[str, Any]]:
    """The BTL comparison pool the Financial scorer ranks a deal against.

    Prefers the lab's research pool when present, else the synthetic demo pool
    that ships with the package. Raises with a clear message when neither is
    available rather than failing on a missing path deep in a scoring call.
    """
    path = _pool_path()
    if path is None:
        raise RuntimeError(
            "no invest pool found. Expected wales_deals.json or invest_pool.json "
            "in one of: %s" % ", ".join(paths.data_candidates("invest_pool.json")))
    return json.load(open(path))


_PTYPE = {"flat": PropertyType.FLAT, "apartment": PropertyType.FLAT, "terraced": PropertyType.TERRACED,
          "end of terrace": PropertyType.TERRACED, "semi-detached": PropertyType.SEMI_DETACHED,
          "detached": PropertyType.DETACHED}


def _ptype(raw_type: Optional[str]) -> PropertyType:
    return _PTYPE.get(str(raw_type or "").strip().lower(), PropertyType.OTHER)


def invest_listing(raw: Dict[str, Any], *, is_demo: bool = True) -> Listing:
    """Normalise a raw Wales deal → a `listing@1` with an `invest` block. Light: the
    invest value slot reads price + est rent, not Land Registry (§5.4)."""
    pid = str(raw.get("id"))
    price = int(raw.get("price"))
    rent = int(raw.get("est_rent_pcm"))
    return Listing(
        id="listing_%s" % pid, listingKey="wales_%s" % pid, mode=Mode.INVEST,
        address=Address(display=raw.get("address"), outcode=raw.get("outcode")),
        propertyType=_ptype(raw.get("type")), beds=raw.get("beds"), baths=raw.get("baths"),
        sqft=raw.get("sqft"), keyFeatures=list(raw.get("key_features") or [])[:6],
        invest=InvestDetails(
            estRentPcm=Money(amount=rent, period=MoneyPeriod.PCM), price=Money(amount=price),
            dealFlags=list(raw.get("deal_flags") or []), tenure=raw.get("tenure"),
            grossYieldAdvertised=raw.get("gross_yield_pct")),
        provenance=Provenance(source=ProvenanceSource.DEMO if is_demo else ProvenanceSource.PASTE_LINK,
                              isDemo=is_demo, fetchedAt="2026-07-14T08:00:00Z", freshness="fresh"))


def _price(obj: Any) -> Optional[int]:
    return _g(obj, "invest.price.amount", "price")


def _rent_pcm(obj: Any) -> Optional[int]:
    return _g(obj, "invest.estRentPcm.amount", "est_rent_pcm")


def _gross_yield(price: Optional[int], rent_pcm: Optional[int]) -> Optional[float]:
    if not price or not rent_pcm:
        return None
    return _round(rent_pcm * 12 / price * 100, 1)


# ---------------------------------------------------------------------------
# §5.4 — the financials (pure arithmetic).
# ---------------------------------------------------------------------------

def financials(listing: Any, *, config: Dict[str, float] = COSTS) -> InvestFinancials:
    """Gross/net yield + monthly cashflow for a deal (§5.4). Reproduces the worked
    example (£110,000 / £1,300 pcm → 14.2% gross). Financing not modelled (OQ 8.6)."""
    price = _price(listing)
    rent = _rent_pcm(listing)
    if not price or not rent:
        raise ValueError("financials needs a price and an est rent pcm")
    refurb = int(_g(listing, "invest.estRefurbCost.amount", default=0) or 0)
    annual_rent = rent * 12
    cost_rate = config["voidWeeks"] / 52.0 + config["mgmtPct"] / 100.0 + config["maintenancePct"] / 100.0
    annual_costs = int(round(annual_rent * cost_rate))
    gross = _round(annual_rent / price * 100, 1)
    net = _round((annual_rent - annual_costs) / (price + refurb) * 100, 1)
    cashflow = int(round(rent - annual_costs / 12.0))
    return InvestFinancials(
        grossYieldPct=gross, annualRent=annual_rent, annualCosts=annual_costs,
        netYieldPct=net, monthlyCashflow=cashflow,
        voidWeeks=config["voidWeeks"], mgmtPct=config["mgmtPct"], maintenancePct=config["maintenancePct"])


def yield_cohort(listing: Any, pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The local yield comp set: same outcode (the like-for-like BTL patch), falling
    back to the same region, then the whole pool, so a thin outcode still reads."""
    outcode = _g(listing, "address.outcode")
    subj = str(_g(listing, "listingKey") or "").replace("wales_", "")

    def ok(r):
        return _price(r) and _rent_pcm(r) and str(r.get("id")) != subj

    same_area = [r for r in pool if r.get("outcode") == outcode and ok(r)]
    if len(same_area) >= _MIN_COHORT:
        return same_area
    region = _g(listing, "invest.region") or None
    same_region = [r for r in pool if region and r.get("region") == region and ok(r)]
    if len(same_region) >= _MIN_COHORT:
        return same_region
    return [r for r in pool if ok(r)]


def financial_verdict(listing: Any, pool: Optional[List[Dict[str, Any]]] = None, *,
                      config: Dict[str, float] = COSTS) -> ValueVerdict:
    """The invest value slot (§5.4): the deal's gross yield vs the local BTL median,
    in the `value_verdict@1` shape so the Mix consumes it unchanged. High yield → steal
    (the financial analogue), low → over. The financials ride in the basis + reasons."""
    pool = load_invest_pool() if pool is None else pool
    fin = financials(listing, config=config)
    price = _price(listing)
    outcode = _g(listing, "address.outcode") or "the area"

    cohort = yield_cohort(listing, pool)
    yields = sorted(y for y in (_gross_yield(_price(r), _rent_pcm(r)) for r in cohort) if y)
    median = _round(statistics.median(yields), 1) if yields else fin.grossYieldPct
    n = len(yields)
    fin.medianYieldPct = median
    fin.cohortSize = n

    # yield vs the local median (relative %); positive = a stronger deal than the patch.
    ydelta = _round((fin.grossYieldPct - median) / median * 100, 1) if median else 0.0
    if n >= _MIN_COHORT and ydelta >= _STRONG_AT:
        tag = "steal"
    elif ydelta <= _WEAK_AT:
        tag = "over"
    else:
        tag = "fair"

    score = _round(_clamp(5.0 + ydelta * 0.08, 0.0, 10.0), 1)

    # the price that would hit the median yield for this rent (the "fair BTL price").
    fair_price = int(round(fin.annualRent / median * 100)) if median else price
    lo_y, hi_y = (yields[0], yields[-1]) if n >= 2 else (median * 0.8, median * 1.2)
    band = ValueBand(low=int(round(fin.annualRent / hi_y * 100)), high=int(round(fin.annualRent / lo_y * 100)))
    span = band.high - band.low
    position = _round(_clamp((price - band.low) / span, 0.0, 1.0), 3) if span > 0 else 0.5
    conf = _round(_clamp(0.35 + 0.05 * n, 0.35, 0.75), 2)   # DEFERRED verdict → honestly modest

    evidence = [ValueEvidence(kind="ask", label="This deal — gross yield", value=fin.grossYieldPct),
                ValueEvidence(kind="comp", label="Local median gross yield (%s)" % outcode, value=median,
                              text="median of %d comparable BTL deals" % n)]

    verdict = ValueVerdict(
        tag=tag, deltaPct=ydelta, headlineDeltaPct=ydelta, fairEstimate=fair_price,
        band=band, position=position, streetMedianPerSqft=None,
        basis="gross %.1f%% · net %.1f%% · £%s pcm cashflow — vs %s median %.1f%% (BTL analysis; financing not modelled)"
              % (fin.grossYieldPct, fin.netYieldPct, "{:,}".format(fin.monthlyCashflow), outcode, median),
        evidence=evidence, confidence=conf)
    verdict.score = score
    verdict.reasons = _invest_reasons(fin, median, ydelta, tag, outcode)
    return verdict


def _invest_reasons(fin: InvestFinancials, median: float, ydelta: float, tag: str, outcode: str) -> List[str]:
    sign = "above" if ydelta > 0 else ("below" if ydelta < 0 else "on")
    out = ["%.1f%% gross yield vs the %s median %.1f%% — %s by %.0f%%."
           % (fin.grossYieldPct, outcode, median, sign, abs(ydelta)),
           "Net %.1f%% after voids, management and maintenance; £%s/mo cashflow before financing."
           % (fin.netYieldPct, "{:,}".format(fin.monthlyCashflow))]
    if tag == "steal":
        out.append("A strong yield for the patch — the numbers lead, taste is the sanity check.")
    elif tag == "over":
        out.append("The yield lags the local median; the price wants the rent it can't get.")
    return out


__all__ = [
    "COSTS", "load_invest_pool", "invest_listing", "financials", "yield_cohort",
    "financial_verdict",
]
