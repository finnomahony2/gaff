"""U-rent — the Rent lens value model (05-modes §5.3 / 03-engine rent-deferred).

The rent counterpart to :mod:`gaff_engine.value`. Rent's value slot does NOT read
Land Registry sold comps — a let is not a purchase of the lease. It reads the
**asking-rent spread** on the street: the subject's £pcm against the area's other
lets (same outcode, same bed-count), a lighter "£/month vs the area" Value
Verdict. The Mix weights it only 15 (`60/25/15`, taste-dominant) because less is
at stake on a short tenancy.

Two pure functions over the real rental pool (`data/rental_candidates.json`, 173
real `RES_LET` listings — Finn's own `mission.primary`):

* :func:`rent_verdict` — the asking-rent Value Verdict (`steal|fair|over` on £pcm
  vs the area median), reusing the P1 ``value_verdict@1`` shape + the U3
  ``value_score`` mapping so the composite Mix consumes it unchanged.
* :func:`affordability` — the Rent dashboard's **lead**: £pcm vs the budget band,
  headroom, bills-in — the first thing that decides a let.

Plus :func:`rent_listing`, a light rent-specific normalise (a raw `RES_LET`
record → a ``listing@1`` with a ``rent`` block), kept separate from U10's
buy-focused :func:`gaff_engine.ingest.normalise` so that path stays untouched.
"""

from __future__ import annotations

import json
import statistics
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from gaff_engine import paths
from gaff_engine.schemas import (
    Address, Agent, GeoPoint, Listing, Media, Mode, Money, MoneyPeriod, PortalId,
    PropertyType, Provenance, ProvenanceSource, RentDetails, Station, ValueBand,
    ValueEvidence, ValueVerdict,
)
from gaff_engine.value import value_score

#: The pool file's name in the two-tier data layout. Resolved through
#: gaff_engine.paths (user cache first, then the shipped/repo data dir) rather
#: than a hardcoded repo-relative path, because the PACKAGE ships no pool —
#: rental candidates are scraped portal content, not redistributable — so a
#: pip install must be able to find a user-supplied copy in the user cache.
_POOL_FILE = "rental_candidates.json"

# Rent asking-spread thresholds (%): the light verdict's steal/over bands. Wider
# than Buy — asking rents are noisier than sold prices — and comp-sufficiency
# gated (need a real cohort before calling a steal/over).
_STEAL_AT = -8.0
_OVER_AT = 8.0
_MIN_COHORT = 3


def _round(x: float, n: int = 1) -> float:
    return float(Decimal(str(x)).quantize(Decimal("1" if n == 0 else "0." + "0" * n),
                                          rounding=ROUND_HALF_UP))


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, x)))


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            if cur is None:
                ok = False
                break
            if isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            elif hasattr(cur, part):
                cur = getattr(cur, part)
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


# ---------------------------------------------------------------------------
# The rental pool + a light rent normalise.
# ---------------------------------------------------------------------------

def load_rent_pool() -> List[Dict[str, Any]]:
    """The raw ``RES_LET`` pool the asking-rent spread reads (the lab checkout
    carries 173 real records at ``data/rental_candidates.json``).

    Resolution goes through :func:`gaff_engine.paths.data_file` so behaviour is
    unchanged in a checkout (the repo ``data/`` copy is found) while a shared
    install — which ships NO pool — can read a user-supplied file from the
    user cache instead. Raises ``FileNotFoundError`` (same failure type as the
    old hardcoded ``open``) when neither tier has one, with ``filename`` set so
    callers can name what was missing.
    """
    path = paths.data_file(_POOL_FILE)
    if path is None:
        raise FileNotFoundError(2, "no rental pool in either cache tier", _POOL_FILE)
    return json.load(open(path))


_PTYPE = {"flat": PropertyType.FLAT, "apartment": PropertyType.FLAT,
          "maisonette": PropertyType.MAISONETTE, "terraced": PropertyType.CONVERSION,
          "conversion": PropertyType.CONVERSION, "house": PropertyType.CONVERSION,
          "studio": PropertyType.FLAT}


def _ptype(raw_type: Optional[str]) -> PropertyType:
    t = (raw_type or "").lower()
    for k, v in _PTYPE.items():
        if k in t:
            return v
    return PropertyType.FLAT


def rent_listing(raw: Dict[str, Any], *, is_demo: bool = True) -> Listing:
    """A raw ``RES_LET`` record → a ``listing@1`` with a ``rent`` block (mode=rent).
    A light, rent-specific normalise — U10's buy path is untouched."""
    pcm = raw.get("pcm") or raw.get("price")
    stations = [Station(name=s.get("name"), types=s.get("types") or [],
                        distanceMiles=s.get("distanceMiles") or s.get("miles"))
                for s in (raw.get("stations") or [])[:3] if isinstance(s, dict) and s.get("name")]
    return Listing(
        id="listing_rent_%s" % raw.get("id"),
        listingKey="rent_%s" % raw.get("id"),
        portalIds=[PortalId(portal="rightmove", id=str(raw.get("id")),
                            url=raw.get("url") or "https://www.rightmove.co.uk")],
        mode=Mode.RENT,
        address=Address(display=raw.get("address") or raw.get("outcode") or "London",
                        line1=(raw.get("address") or "").split(",")[0] or None,
                        outcode=raw.get("outcode"), postcode=raw.get("outcode"),
                        ukCountry="England"),
        geo=GeoPoint(lat=raw.get("lat"), lng=raw.get("lng"), accuracy="approximate"),
        propertyType=_ptype(raw.get("type")),
        beds=int(raw.get("beds") or 0), baths=int(raw.get("baths") or 1),
        sqft=int(raw["sqft"]) if raw.get("sqft") else None,
        keyFeatures=list(raw.get("key_features") or [])[:8],
        description=raw.get("description") or "",
        images=[Media(url=raw.get("url") or "", kind="photo", caption="Reception")],
        nearestStations=stations or None,
        agent=Agent(companyName=(raw.get("agent") or {}).get("name") if isinstance(raw.get("agent"), dict) else raw.get("agent")),
        rent=RentDetails(
            rentPcm=Money(amount=int(pcm), period=MoneyPeriod.PCM) if pcm else None,
            furnished=raw.get("furnish_type"), availableFrom=raw.get("let_available"),
            letType="long_term"),
        provenance=Provenance(source=ProvenanceSource.DEMO if is_demo else ProvenanceSource.PARTNER_FEED,
                              portal="rightmove", fetchedAt="2026-07-14T08:00:00Z",
                              freshness="fresh", isDemo=is_demo,
                              completeness={"sqft": "stated" if raw.get("sqft") else "missing"}))


# ---------------------------------------------------------------------------
# The asking-rent spread + the Value Verdict.
# ---------------------------------------------------------------------------

def _pcm(obj: Any) -> Optional[int]:
    if isinstance(obj, dict):
        v = obj.get("pcm") or obj.get("price")
        return int(v) if v else None
    m = getattr(getattr(obj, "rent", None), "rentPcm", None)
    return int(m.amount) if m and m.amount else None


def rent_cohort(listing: Any, pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The asking-rent comp set: same outcode + same bed-count (the like-for-like
    area cohort). Falls back to same-bed-count across the pool if the outcode is
    too thin (< the cohort floor), so a sparse street still gets an honest read."""
    outcode = _g(listing, "address.outcode")
    beds = _g(listing, "beds")
    subj_id = _g(listing, "portalIds")
    subj_pid = str(subj_id[0].id) if subj_id else None

    def same_beds(r):
        return beds is None or int(r.get("beds") or -1) == int(beds)

    def not_self(r):
        return subj_pid is None or str(r.get("id")) != subj_pid

    same_area = [r for r in pool if r.get("outcode") == outcode and same_beds(r)
                 and _pcm(r) and not_self(r)]
    if len(same_area) >= _MIN_COHORT:
        return same_area
    return [r for r in pool if same_beds(r) and _pcm(r) and not_self(r)]


def rent_verdict(listing: Any, pool: Optional[List[Dict[str, Any]]] = None) -> ValueVerdict:
    """The light asking-rent Value Verdict (§5.3): the subject £pcm vs the area
    median. Reuses ``value_verdict@1`` + U3's ``value_score`` so the Mix consumes
    it unchanged; ``basis`` names it an asking-rent read, not Land Registry."""
    pool = load_rent_pool() if pool is None else pool
    ask = _pcm(listing)
    if ask is None:
        raise ValueError("rent_verdict needs a subject rentPcm")

    cohort = rent_cohort(listing, pool)
    outcode = _g(listing, "address.outcode") or "the area"
    same_area = [r for r in cohort if r.get("outcode") == outcode]
    label = ("%d lets in %s" % (len(same_area), outcode) if len(same_area) >= _MIN_COHORT
             else "%d %d-bed lets nearby" % (len(cohort), int(_g(listing, "beds") or 0)))
    pcms = sorted(_pcm(r) for r in cohort)
    median = int(statistics.median(pcms)) if pcms else ask
    n = len(pcms)

    delta = _round((ask - median) / median * 100.0, 1) if median else 0.0
    if n >= _MIN_COHORT and delta <= _STEAL_AT:
        tag = "steal"
    elif delta >= _OVER_AT:
        tag = "over"
    else:
        tag = "fair"

    lo, hi = (pcms[len(pcms) // 4], pcms[(3 * len(pcms)) // 4]) if n >= 4 else (
        int(median * 0.9), int(median * 1.1))
    band = ValueBand(low=min(lo, median), high=max(hi, median))
    span = band.high - band.low
    position = _round(_clamp((ask - band.low) / span, 0.0, 1.0), 3) if span > 0 else 0.5
    conf = _round(_clamp(0.4 + 0.06 * n, 0.4, 0.85), 2)   # more lets → firmer read

    ppsf = None
    subj_sqft = _g(listing, "sqft")
    if subj_sqft:
        ppsf = _round(ask / int(subj_sqft), 2)

    evidence = [ValueEvidence(kind="ask", label="This let £pcm", value=float(ask)),
                ValueEvidence(kind="comp", label="Area median asking rent (%s)" % label,
                              value=float(median), text="median £pcm of %d comparable lets" % n)]
    if ppsf is not None:
        evidence.append(ValueEvidence(kind="ppsf", label="This let £pcm/sqft", value=ppsf))

    verdict = ValueVerdict(
        tag=tag, deltaPct=delta, headlineDeltaPct=delta,
        fairEstimate=median, band=band, position=position,
        streetMedianPerSqft=(_round(median / int(subj_sqft), 2) if subj_sqft else None),
        basis="asking-rent spread: %s; median £%s pcm (no Land Registry — a let, not a purchase)"
              % (label, "{:,}".format(median)),
        evidence=evidence, confidence=conf)
    verdict.score = value_score(verdict)
    verdict.reasons = _rent_reasons(ask, median, delta, tag, label)
    return verdict


def _rent_reasons(ask: int, median: int, delta: float, tag: str, label: str) -> List[str]:
    sign = "under" if delta < 0 else ("over" if delta > 0 else "right on")
    out = ["£%s pcm vs the area median £%s — %s by %.1f%% (%s)."
           % ("{:,}".format(ask), "{:,}".format(median),
              sign if delta != 0 else "right on the median", abs(delta), label)]
    if tag == "steal":
        out.append("A genuinely cheap let for the street — worth moving fast on.")
    elif tag == "over":
        out.append("Priced above the street; there may be room to negotiate or better nearby.")
    return out


def rent_value_score(verdict: Any) -> float:
    return value_score(verdict)


# ---------------------------------------------------------------------------
# Affordability — the Rent dashboard LEAD (§5.3): £pcm vs the budget band.
# ---------------------------------------------------------------------------

def affordability(listing: Any, *, budget_max_pcm: Optional[int] = None,
                  budget_gravity_pcm: Optional[int] = None,
                  bills_rule_of_thumb: int = 250) -> Dict[str, Any]:
    """The affordability read the Rent lead renders: £pcm vs the user's budget
    band, headroom, and a bills-in estimate. Pure; reads ``listing.rent`` +
    the Search budget. ``tag`` ∈ within | stretch | over."""
    pcm = _pcm(listing)
    bills_included = bool(_g(listing, "rent.billsIncluded"))
    all_in = pcm if (pcm is None or bills_included) else pcm + bills_rule_of_thumb

    tag, headroom_pct, line = "unknown", None, None
    if pcm is not None and budget_max_pcm:
        headroom = budget_max_pcm - pcm
        headroom_pct = _round(headroom / budget_max_pcm * 100.0, 1)
        if pcm <= (budget_gravity_pcm or budget_max_pcm):
            tag = "within"
        elif pcm <= budget_max_pcm:
            tag = "stretch"
        else:
            tag = "over"
        pos = "%s%s" % ("£{:,}".format(abs(headroom)), "")
        if tag == "over":
            line = "£%s/mo over your ceiling — a real stretch." % "{:,}".format(-headroom)
        elif tag == "stretch":
            line = "Inside your ceiling but above your comfort line — %s to spare." % pos
        else:
            line = "Comfortably within budget — %s of headroom a month." % pos
    return {
        "pcm": pcm, "billsIncluded": bills_included, "allInPcm": all_in,
        "budgetMax": budget_max_pcm, "budgetGravity": budget_gravity_pcm,
        "headroomPct": headroom_pct, "tag": tag, "line": line,
    }


__all__ = [
    "load_rent_pool", "rent_listing", "rent_cohort", "rent_verdict",
    "rent_value_score", "affordability",
]
