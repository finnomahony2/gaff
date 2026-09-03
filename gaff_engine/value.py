"""U3 — the Value Verdict, THE Buy differentiator (03-engine §5.2).

Pure, deterministic functions that turn a subject Listing + a set of HM Land
Registry Price Paid comps (EPC-enriched with a £/sqft, see U9/EPC) into a real
:class:`~gaff_engine.schemas.ValueVerdict`: the fair estimate, the honest
``headlineDeltaPct`` -> ``deltaPct`` adjustment story, the ``steal|fair|over``
tag, the 0-10 value component, the gauge fields (``band``, ``position``,
``streetMedianPerSqft``) and a comp-driven confidence.

Authority: docs/spec/03-engine.md §5.2 (method) + §5.0 ``engine.config@1`` (the
tunable ``value`` block, embedded here as :data:`CONFIG` — no config artefact
exists in the repo yet, so the spec's inline bundle is the source) + §5.8
(confidence). Every number is computed by a named method; nothing is a free
parameter. No LLM, no I/O beyond the optional comps loader, no globals mutated.

Design mandate (this listing is anchor-sensitive — honest, not falsely precise):

* ANCHOR SELECTION (:func:`select_anchor`) prefers the most like-for-like,
  area-vetted set: (1) same-street comps with ``areaConfidence`` in
  {high, medium}; if fewer than :data:`MIN_ANCHOR` (3) widen to (2) the whole
  area's {high, medium}; else (3) all matched comps, confidence capped low.
  The no-sqft like-for-like path swaps the trust gate (:func:`_llf_trusted`):
  areaConfidence measures floor-area trust, irrelevant to a whole-price read,
  so any comp with a real sold price may anchor its same-street tier.
* FAIR ESTIMATE (:func:`fair_estimate`) = median £/sqft of the anchor set x
  subject sqft, with a raw inter-quartile band that :func:`value_verdict` then
  widens by ``(1 - valueConfidence)`` (§5.2b). A subject with NO floor area
  falls back to §5.2b's like-for-like sold-price estimate (``Fair_llf``,
  :func:`_llf_verdict`): median whole sold price of the same-class comps,
  confidence dampened one level, basis labelled plainly — never a silent
  NEEDS_DATA when comparable sales exist.
* CONFIDENCE (:func:`value_confidence`) rises with anchor comp COUNT, falls with
  the £/sqft spread (coefficient of variation) and with the median EPC->sale gap
  of the anchor set — so n=4 tight same-street reads ~medium, not high.
* ADJUSTMENTS (:func:`lease_adjustment`) — the "looks like a steal, actually
  fair" mechanism: a signed, sourced lease-extension cost (marriage_value_v1),
  negligible above ~90 years, rising below, and sharply below the 80-year
  marriage-value cliff. Extensible to refurb/feature adjustments.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from gaff_engine import epc
from gaff_engine import hpi
from gaff_engine import paths
from gaff_engine.schemas import (
    Comp, CompAddress, Ref, ValueBand, ValueEvidence, ValueTag, ValueVerdict,
)

# ---------------------------------------------------------------------------
# CONFIG — the value block of engine.config@1 (03-engine §5.0), inlined.
# The one place thresholds/curves live; change a number here and re-backtest
# (§7.3) — no code change. Kept as a plain dict so a future engine.config@1
# artefact can supersede it wholesale.
# ---------------------------------------------------------------------------

CONFIG: Dict[str, Any] = {
    # Comp qualification / sufficiency (§5.2a, §5.2d).
    "minCompsForVerdict": 5,      # a non-fair tag needs >= this many comps
    "minCompsHardFloor": 3,       # below this, verdict is forced fair
    "sqftTolerancePct": 25,       # both-sqft-known like-for-like window
    # Verdict thresholds + value-component curve (§5.2d).
    "stealPct": -5.0,             # deltaPct <= this -> steal
    "overPct": 5.0,               # deltaPct >= this -> over
    "valueScoreSlopePerPct": 0.27,
    "valueScoreClamp": (0.5, 9.5),
    # Confidence (§5.8): comp-count base then multiplicative modifiers.
    "confidence": {
        "bands": {"high": 0.75, "medium": 0.5},
        "byCompCount": {"5plus": 0.90, "4": 0.80, "3": 0.70, "2": 0.55, "1orLess": 0.40},
        "spreadFloor": 0.60,      # spreadFactor = clamp(1 - CV, floor, 1.0)
        "gapFreeYears": 2.0,      # EPC->sale gap under this is "fresh", no penalty
        "gapPenaltyPerYear": 0.02,
        "gapFloor": 0.85,
        "adjustmentEstimate": 0.90,  # a lease/refurb adj is an estimate -> x0.90
        # The subject's marketing sqft and its EPC area disagree beyond
        # epc.SQFT_BASIS_TOLERANCE_PCT: the £/sqft denominator is contested,
        # so the verdict is an estimate on top of an estimate -> x0.90.
        "sqftBasisConflict": 0.90,
    },
    # Lease-extension model — marriage_value_v1 (§5.2c, OQ 8.3). An ESTIMATE.
    "lease": {
        "negligibleYears": 90.0,  # at/above -> ~zero (statutory line ~90)
        "cliffYears": 80.0,       # BELOW this the marriage value applies (cited)
        "gapRatePerYear": 0.006,  # value fraction lost per year under 90
        "marriageMultiplier": 2.5,  # per-year cost steepens below the 80-yr cliff
    },
    # Gauge calibration (§5.2d / P2 §5.5.4): band.low<->0.20, band.high<->0.85.
    "gauge": {"bandLowPos": 0.20, "bandHighPos": 0.85, "posClamp": (0.02, 0.98)},
}

MIN_ANCHOR = CONFIG["minCompsHardFloor"]  # 3 — the widen trigger (design mandate)

# Property-type compatibility groups (§5.2a: never cross the two).
FLAT_GROUP = {"flat", "maisonette", "conversion", "flat-maisonette", "flat_maisonette"}
HOUSE_GROUP = {"terraced", "semi_detached", "semi-detached", "detached",
               "end_terrace", "end-terrace", "house"}

# Resolved through gaff_engine.paths so an install finds the copy that ships
# with the package, and a user's own enriched set shadows it.
DEFAULT_COMPS_PATH = (paths.data_file("comps_enriched.json")
                      or paths.data_candidates("comps_enriched.json")[-1])


# ---------------------------------------------------------------------------
# Deterministic numeric helpers (half-up rounding, mirroring composite.py).
# ---------------------------------------------------------------------------

def _round(x: float, dp: int) -> float:
    """Round to ``dp`` decimals, half-up, deterministically."""
    q = Decimal(1).scaleb(-dp)  # 10**-dp
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def _round_pounds(x: float, nearest: int = 1000) -> int:
    """Round a pound amount to the nearest ``nearest`` (default £1,000)."""
    return int(Decimal(str(x / nearest)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * nearest


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, x)))


# ---------------------------------------------------------------------------
# Duck-typed accessors — read a Comp / Listing / dict / namespace alike, so the
# functions stay pure and the tests can drive them with light dicts (the
# codebase style, cf. composite._score_weight).
# ---------------------------------------------------------------------------

def _g(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute-or-key among ``names`` (a dotted name walks in)."""
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


def _enum_value(v: Any) -> Any:
    """Unwrap an Enum to its wire value; pass through str/None."""
    return getattr(v, "value", v)


def subject_sqft(subject: Any) -> Optional[float]:
    v = _g(subject, "sqft")
    return float(v) if v is not None else None


def subject_epc_sqft(subject: Any) -> Optional[float]:
    """The subject's EPC-derived floor area (sqft), where a caller supplied one.

    The pipeline does not yet look the subject up in the EPC register itself
    (that is show_work's address-match territory), so this reads an optional
    ``epcSqft`` a caller attaches — and the sqft-basis conflict machinery
    (:func:`gaff_engine.epc.sqft_basis_check`) only fires when both figures
    genuinely exist, never on a guess.
    """
    v = _g(subject, "epcSqft", "derived.epcSqft")
    return float(v) if v is not None else None


def subject_ask(subject: Any) -> Optional[int]:
    v = _g(subject, "buy.price.amount", "askingPrice", "price")
    return int(v) if v is not None else None


def subject_ptype(subject: Any) -> Optional[str]:
    v = _enum_value(_g(subject, "propertyType"))
    return str(v).lower() if v is not None else None


def subject_tenure_type(subject: Any) -> Optional[str]:
    v = _enum_value(_g(subject, "buy.tenure.type", "tenure.type", "tenureType", "tenure"))
    return str(v).lower() if v is not None else None


def subject_lease_years(subject: Any) -> Optional[int]:
    v = _g(subject, "buy.tenure.leaseYearsRemaining", "tenure.leaseYearsRemaining",
           "leaseYearsRemaining", "leaseYears")
    return int(v) if v is not None else None


def _comp_ppsf(c: Any) -> Optional[float]:
    v = _g(c, "pricePerSqft")
    return float(v) if v is not None else None


def _comp_price(c: Any) -> Optional[float]:
    v = _g(c, "price", "soldPrice")
    return float(v) if v is not None else None


def _comp_ptype(c: Any) -> str:
    return str(_enum_value(_g(c, "propertyType", default="")) or "").lower()


def _normalise_category(v: Any) -> Optional[str]:
    """Collapse any spelling of the PPD transaction category onto the two-word
    vocabulary the qualifiers compare against: the already-normalised
    ``"standard"`` / ``"additional"``, the register's full prefLabel
    ("Additional price paid transaction") or its ``_about`` slug
    ("additionalPricePaidTransaction"). The same substring rule as
    ``landreg._txn_category``, mirrored rather than imported so the pure value
    layer keeps no dependency on the fetch adapter. An unrecognised non-empty
    string returns ``None`` (unknown): a category we cannot read must be
    COUNTED in provenance, never waved through as standard — a bare lowercase
    pass-through here once let a full-label repossession slip both the anchor
    filter and the unknown-category caveat."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if "standard" in s:
        return "standard"
    if "additional" in s:
        return "additional"
    return None


def _comp_category(c: Any) -> Optional[str]:
    """The comp's PPD transaction category: ``"standard"`` / ``"additional"`` /
    ``None`` (unknown — e.g. an enriched set built before the field was
    captured, or a spelling :func:`_normalise_category` cannot read). Unknown
    is treated as standard by the qualifiers but counted in provenance so the
    limitation stays visible."""
    return _normalise_category(_g(c, "transactionCategory"))


def _ptype_group(pt: Optional[str]) -> Optional[str]:
    if not pt:
        return None
    pt = pt.lower()
    if pt in FLAT_GROUP:
        return "flat"
    if pt in HOUSE_GROUP:
        return "house"
    return None


# ---------------------------------------------------------------------------
# Adjustment — the value-layer honesty item (§5.2c).
#
# NOTE: schemas.py has no standalone ``Adjustment`` dataclass; §5.2c models each
# Adjustment AS a ``valueVerdict.evidence[]`` item ("Each Adjustment is emitted
# as an evidence[] item with its £ value and a one-line text"). So ``Adjustment``
# here is a small INTERNAL value-layer type that :meth:`to_evidence` materialises
# into a schema ``ValueEvidence`` — it does not redefine any schema shape.
# ---------------------------------------------------------------------------

@dataclass
class Adjustment:
    """A signed, sourced correction between headline and honest delta (§5.2c)."""
    kind: str                     # "lease_adj" | "refurb_adj" | "feature_adj"
    label: str
    amountGBP: float              # positive magnitude of the £ effect
    direction: str                # "worsens" (adds cost) | "improves" (premium)
    text: str
    source: str                   # a Listing field path, e.g. tenure.leaseYearsRemaining
    isEstimate: bool = True       # lease/refurb are estimates -> lower confidence

    def signed_cost(self) -> float:
        """£ added to the effective asking price (+ worsens, − improves)."""
        return self.amountGBP if self.direction == "worsens" else -self.amountGBP

    def to_evidence(self) -> ValueEvidence:
        """Materialise as a schema ValueEvidence (§5.2c). ``value`` is the signed
        £ effect (negative = worsens the buyer's position, per the golden)."""
        return ValueEvidence(kind=self.kind, label=self.label,
                             value=float(-_round_pounds(self.amountGBP)
                                         if self.direction == "worsens"
                                         else _round_pounds(self.amountGBP)),
                             text=self.text)


# ---------------------------------------------------------------------------
# Step 1 — comp qualification + anchor selection (§5.2a + design mandate).
# ---------------------------------------------------------------------------

def qualifies(comp: Any, subject: Any) -> bool:
    """A comp can contribute a £/sqft estimate for this subject (§5.2a).

    Requires a ``pricePerSqft`` (else no £/sqft contribution) and a compatible
    property-type group (flat/maisonette/conversion interchangeable; house types
    interchangeable; never cross). Size/tenure are handled by the £/sqft
    normalisation and the lease Adjustment, not a hard filter here.

    "Additional price paid" PPD rows (repossessions, power-of-sale, transfers
    to non-private individuals) are excluded: they are not open-market prices,
    and standard AVM practice anchors on standard-category rows only. An
    UNKNOWN category (a comp set built before the field was captured) is
    treated as standard — most rows are — but counted in the verdict's
    provenance rather than silently assumed clean.
    """
    if _comp_ppsf(comp) is None:
        return False
    if _comp_category(comp) == "additional":
        return False
    sg = _ptype_group(subject_ptype(subject))
    cg = _ptype_group(_comp_ptype(comp))
    if sg is not None and cg is not None and sg != cg:
        return False
    return True


def qualifies_llf(comp: Any, subject: Any) -> bool:
    """A comp can contribute a like-for-like SOLD-PRICE estimate (§5.2b Fair_llf).

    The fallback path for a subject with no floor area: no ``pricePerSqft`` is
    required, only a positive sold ``price`` and a compatible property-type
    group (the same never-cross-flat/house rule as :func:`qualifies`). The
    same transaction-category gate applies — a repossession's whole price is
    no more an open-market signal than its £/sqft would be.
    """
    p = _comp_price(comp)
    if p is None or p <= 0:
        return False
    if _comp_category(comp) == "additional":
        return False
    sg = _ptype_group(subject_ptype(subject))
    cg = _ptype_group(_comp_ptype(comp))
    if sg is not None and cg is not None and sg != cg:
        return False
    return True


def _subject_street(subject: Any) -> Optional[str]:
    """The subject Listing's street, upper-cased — from ``address.line1`` /
    ``street`` or the first segment of ``address.display``."""
    v = _g(subject, "address.line1", "address.street", "street")
    if not v:
        disp = _g(subject, "address.display")
        if disp:
            v = str(disp).split(",")[0]
    return str(v).strip().upper() if v else None


def _comp_street(comp: Any) -> Optional[str]:
    v = _g(comp, "address.street", "street")
    return str(v).strip().upper() if v else None


def _is_same_street(comp: Any, subject: Any = None) -> bool:
    """Subject-RELATIVE: a comp is "same street" when its street matches the
    subject Listing's street — so the anchor generalises across the shortlist
    (each listing anchors on ITS own street), not on a fixed precomputed tag.
    Falls back to the ``distanceNote`` tag only when a street is unavailable."""
    subj = _subject_street(subject) if subject is not None else None
    cs = _comp_street(comp)
    if subj and cs:
        return cs == subj
    note = str(_g(comp, "distanceNote", default="") or "").strip().lower()
    return note == "same street"


def _is_trusted(comp: Any) -> bool:
    return str(_g(comp, "areaConfidence", default="") or "").lower() in ("high", "medium")


def _llf_trusted(comp: Any) -> bool:
    """Tier gate for the like-for-like path: every qualifying comp is trusted.

    ``areaConfidence`` measures EPC floor-area trust — the right tier gate when
    the estimate is a £/sqft read, and irrelevant to a whole-price read (the
    sold price is exact regardless of any floor-area match). Raw Land Registry
    sales from a user-warmed street carry ``areaConfidence`` None, so gating
    llf tiers on it made the engine's declared strongest tier (same-street)
    systematically unreachable for exactly the comps the llf path exists to
    use. Qualification (:func:`qualifies_llf`: a real sold price, compatible
    class) already did the vetting; the tier walk only ranks by nearness.
    """
    return True


def select_anchor(comps: List[Any], subject: Any,
                  qualifier: Any = qualifies,
                  trusted: Any = _is_trusted) -> Tuple[List[Any], str]:
    """Choose the anchor set + a human label (design mandate ANCHOR SELECTION).

    Order of preference, widening only when a tier is thinner than
    :data:`MIN_ANCHOR` (3):
      1. same-street comps passing ``trusted``;
      2. the whole area's ``trusted`` comps;
      3. all matched comps (confidence is capped low downstream).
    Returns ``(anchor_comps, anchor_label)``; records which tier was used.
    ``qualifier`` defaults to the £/sqft :func:`qualifies`; the no-sqft
    like-for-like path passes :func:`qualifies_llf` instead (§5.2b).
    ``trusted`` defaults to ``areaConfidence`` in {high, medium} — EPC
    floor-area trust, the right gate for a £/sqft read; the like-for-like
    path passes :func:`_llf_trusted` (every qualifying sold price counts, so
    a warmed street's own sales can anchor tier 1) and gets labels that speak
    of sold prices, not of an areaConfidence the read never uses. Confidence
    maths are unchanged either way (count x spread x staleness, §5.8).
    """
    pool = [c for c in comps if qualifier(c, subject)]
    llf = trusted is _llf_trusted

    tier1 = [c for c in pool if _is_same_street(c, subject) and trusted(c)]
    if len(tier1) >= MIN_ANCHOR:
        return tier1, ("same-street sold prices" if llf else
                       "same-street trusted comps (high/medium areaConfidence)")

    tier2 = [c for c in pool if trusted(c)]
    if len(tier2) >= MIN_ANCHOR:
        return tier2, ("area sold prices (same-street set too thin)" if llf else
                       "area trusted comps (high/medium areaConfidence; same-street set too thin)")

    # Tier 3 — all matched comps; the honest last resort (confidence capped
    # low by the §5.8 count base). On the llf path this equals tier 2 (every
    # qualifying comp is trusted) and only fires when the whole pool is thin.
    return pool, ("all matched sold prices (thin set; confidence capped by count)" if llf else
                  "all matched comps (trusted set too thin; confidence capped low)")


# ---------------------------------------------------------------------------
# Step 2 — fair estimate + raw band (§5.2b).
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: List[float], q: float) -> float:
    """Linear-interpolation percentile (numpy/Excel-INC convention)."""
    if not sorted_vals:
        raise ValueError("percentile of empty set")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo])


def fair_estimate(anchor_comps: List[Any], subject_sqft: float) -> Dict[str, Any]:
    """Fair value from the anchor set's £/sqft (§5.2b).

    ``estimate`` = median £/sqft x subject sqft (rounded to £1,000).
    ``band`` = the RAW inter-quartile (25th-75th) implied-value spread — the
    statistical band BEFORE :func:`value_verdict` widens it by
    ``(1 - valueConfidence)``. With <2 comps the band collapses to the point
    estimate (widened later). Returns ``{estimate, band, ppsfMedian, n}``.
    """
    if not anchor_comps:
        raise ValueError("fair_estimate requires at least one anchor comp")
    if subject_sqft is None or subject_sqft <= 0:
        raise ValueError("fair_estimate requires a positive subject sqft")

    ppsf = sorted(_comp_ppsf(c) for c in anchor_comps if _comp_ppsf(c) is not None)
    if not ppsf:
        raise ValueError("fair_estimate requires anchor comps with pricePerSqft")

    ppsf_median = statistics.median(ppsf)
    estimate = _round_pounds(ppsf_median * subject_sqft)

    if len(ppsf) >= 2:
        lo_ppsf = _percentile(ppsf, 0.25)
        hi_ppsf = _percentile(ppsf, 0.75)
        band = ValueBand(low=_round_pounds(lo_ppsf * subject_sqft),
                         high=_round_pounds(hi_ppsf * subject_sqft))
    else:
        band = ValueBand(low=estimate, high=estimate)  # widened by confidence later

    return {"estimate": estimate, "band": band,
            "ppsfMedian": _round(ppsf_median, 1), "n": len(ppsf)}


# ---------------------------------------------------------------------------
# Step 3 — the Adjustment layer (§5.2c). Lease modelled; refurb/feature stubbed
# extensible.
# ---------------------------------------------------------------------------

def lease_extension_cost(years: Optional[float], value: float) -> float:
    """Estimated cost to extend a short lease — marriage_value_v1 (§5.2c, OQ 8.3).

    A defensible, transparent piecewise model expressed as a fraction of the
    property ``value``:

    * ``years >= 90`` (``negligibleYears``): ~0 — at/above the statutory ~90-year
      line an extension is cheap and immaterial to the verdict.
    * ``80 <= years < 90``: a value gap widening linearly at ``gapRatePerYear``
      (0.6%/yr) per year under 90 — modest.
    * ``years < 80`` (the 80-YEAR MARRIAGE-VALUE CLIFF, cited): the freeholder is
      entitled to ~50% of the marriage value, so each year under 80 costs
      ``marriageMultiplier`` (2.5x) the base rate ON TOP of the full 90->80 gap —
      the cost rises sharply.

    This is an ESTIMATE (labelled as such, and it lowers confidence x0.90); a
    real premium depends on freeholder, ground rent and value band (OQ 8.3).
    """
    cfg = CONFIG["lease"]
    neg, cliff = cfg["negligibleYears"], cfg["cliffYears"]
    rate, mult = cfg["gapRatePerYear"], cfg["marriageMultiplier"]
    if years is None or years >= neg:
        return 0.0
    if years >= cliff:
        return value * rate * (neg - years)
    base_90_to_80 = value * rate * (neg - cliff)          # full modest gap
    cliff_cost = value * rate * mult * (cliff - years)    # steep, marriage value
    return base_90_to_80 + cliff_cost


def lease_adjustment(subject: Any) -> Optional[Adjustment]:
    """A lease-extension :class:`Adjustment` for a short-leasehold flat, else None.

    Fires only for leasehold subjects with a lease under the ~90-year line; sizes
    the cost off the subject's asking price via :func:`lease_extension_cost`. The
    89-year golden subject yields a small negative (worsening) adjustment; a
    sub-80 lease a much larger one (the cliff).
    """
    tenure = subject_tenure_type(subject)
    years = subject_lease_years(subject)
    if tenure not in ("leasehold", "share_of_freehold", "commonhold"):
        return None
    if tenure != "leasehold":
        # share-of-freehold / commonhold: no lease-extension cost.
        return None
    if years is None:
        return None
    ask = subject_ask(subject) or 0
    cost = lease_extension_cost(years, ask)
    if cost <= 0:
        return None
    cliff = CONFIG["lease"]["cliffYears"]
    cliff_note = (" — below the 80-year marriage-value cliff, so the cost rises steeply"
                  if years < cliff else "")
    text = ("%d-yr lease: est. ~£%s to extend toward a long-lease-equivalent value%s "
            "(marriage_value_v1 estimate; cite the 80-yr cliff)."
            % (years, "{:,}".format(_round_pounds(cost)), cliff_note))
    return Adjustment(kind="lease_adj",
                     label="%d-yr lease extension est." % years,
                     amountGBP=float(cost), direction="worsens", text=text,
                     source="tenure.leaseYearsRemaining", isEstimate=True)


def adjustments(subject: Any, forensics: Any = None) -> List[Adjustment]:
    """Aggregate all applicable Adjustments (§5.2c). Lease is modelled now;
    refurb (from Forensics condition) and feature adjustments plug in here as the
    slice matures — the list is intentionally extensible."""
    out: List[Adjustment] = []
    lease = lease_adjustment(subject)
    if lease is not None:
        out.append(lease)
    # Future: refurb_adj (forensics condition), feature_adj (exceptional feature).
    return out


# ---------------------------------------------------------------------------
# Confidence (§5.8) — comp-count base x spread x staleness.
# ---------------------------------------------------------------------------

def _confidence_band(scalar: float) -> str:
    b = CONFIG["confidence"]["bands"]
    if scalar >= b["high"]:
        return "high"
    if scalar >= b["medium"]:
        return "medium"
    return "low"


def _dampen_one_level(scalar: float) -> float:
    """Pull a confidence scalar down one BAND (§5.2b fallback honesty).

    The like-for-like sold-price read is coarser than a £/sqft read — it never
    corrects for size — so its confidence is dampened one level: subtract the
    high->medium band gap (0.25), which drops any high scalar to medium and any
    medium scalar to low while preserving the ordering within the set. Floored
    just above zero so a verdict still carries a nonzero honesty signal.
    """
    b = CONFIG["confidence"]["bands"]
    gap = b["high"] - b["medium"]
    return _round(max(scalar - gap, 0.05), 2)


def _count_base(n: int) -> float:
    t = CONFIG["confidence"]["byCompCount"]
    if n >= 5:
        return t["5plus"]
    return {4: t["4"], 3: t["3"], 2: t["2"]}.get(n, t["1orLess"])


def value_confidence(anchor_comps: List[Any], *,
                     spread_values: Optional[List[float]] = None,
                     spread_label: str = "£/sqft") -> Dict[str, Any]:
    """Comp-driven value confidence (§5.8) — a ConfidenceReport-compatible
    ``{scalar, band, drivers, missing}``.

    ``scalar = countBase(n) x spreadFactor x gapFactor``:
      * ``countBase`` rises with anchor COUNT (5+ ->0.90, 4->0.80, 3->0.70, ...);
      * ``spreadFactor = clamp(1 - CV, floor, 1.0)`` falls with the £/sqft
        coefficient of variation (tight set -> ~1.0, scattered -> lower);
      * ``gapFactor`` falls with the median EPC->sale gap of the anchor set
        (stale floor areas are less trustworthy).

    The lease/refurb "adjustment is an estimate" x0.90 modifier (§5.8) is applied
    by :func:`value_verdict` (it needs the subject's adjustments), keeping this
    function a pure read over the comps.

    ``spread_values`` (keyword-only) substitutes the series the spread factor is
    computed over — the no-sqft like-for-like path passes the comps' sold PRICES
    (there is no £/sqft to spread) with ``spread_label="sold-price"``; default is
    the anchor's £/sqft, unchanged behaviour.
    """
    n = len(anchor_comps)
    if spread_values is not None:
        ppsf = [float(v) for v in spread_values if v is not None]
    else:
        ppsf = [_comp_ppsf(c) for c in anchor_comps if _comp_ppsf(c) is not None]
    gaps = [float(_g(c, "epcSaleGapYears", default=0.0) or 0.0) for c in anchor_comps]

    count_base = _count_base(n)

    cfg = CONFIG["confidence"]
    if len(ppsf) >= 2 and statistics.mean(ppsf) > 0:
        cv = statistics.pstdev(ppsf) / statistics.mean(ppsf)
    else:
        cv = 0.0
    spread_factor = _clamp(1.0 - cv, cfg["spreadFloor"], 1.0)

    med_gap = statistics.median(gaps) if gaps else 0.0
    gap_factor = _clamp(1.0 - cfg["gapPenaltyPerYear"] * max(0.0, med_gap - cfg["gapFreeYears"]),
                        cfg["gapFloor"], 1.0)

    scalar = _round(_clamp(count_base * spread_factor * gap_factor, 0.0, 1.0), 2)

    drivers = [
        "%d anchor comp%s (base %.2f)" % (n, "" if n == 1 else "s", count_base),
        "%s spread CV %.2f (factor %.2f)" % (spread_label, _round(cv, 2), _round(spread_factor, 2)),
        "median EPC->sale gap %.1fy (factor %.2f)" % (_round(med_gap, 1), _round(gap_factor, 2)),
    ]
    missing: List[str] = []
    if n < CONFIG["minCompsForVerdict"]:
        missing.append("only %d comp%s (want >=%d)"
                       % (n, "" if n == 1 else "s", CONFIG["minCompsForVerdict"]))
    if cv >= 0.20:
        missing.append("wide %s spread across the anchor set" % spread_label)
    if med_gap > 5.0:
        missing.append("some anchor floor areas are from stale EPCs (>5y before sale)")

    return {"scalar": scalar, "band": _confidence_band(scalar),
            "drivers": drivers, "missing": missing,
            "n": n, "cv": _round(cv, 3), "medianGapYears": _round(med_gap, 1)}


# ---------------------------------------------------------------------------
# Step 4 — verdict, value component, position (§5.2d).
# ---------------------------------------------------------------------------

def verdict_tag(delta_pct: float, n_comps: int) -> Tuple[ValueTag, bool]:
    """Derive the ``steal|fair|over`` tag from ``deltaPct`` against the config
    thresholds, then apply the comp-sufficiency gate (§5.2d, P1 OQ 8.1).

    Returns ``(tag, capped)`` where ``capped`` is True when a non-fair raw tag
    was pulled to ``fair`` by the gate (n < minCompsForVerdict) or forced fair
    (n < minCompsHardFloor). Raw thresholds: ``deltaPct <= stealPct(-5)`` ->
    steal; ``>= overPct(+5)`` -> over; else fair.
    """
    steal, over = CONFIG["stealPct"], CONFIG["overPct"]
    if delta_pct <= steal:
        raw = ValueTag.STEAL
    elif delta_pct >= over:
        raw = ValueTag.OVER
    else:
        raw = ValueTag.FAIR

    if raw == ValueTag.FAIR:
        return raw, False
    if n_comps < CONFIG["minCompsHardFloor"]:      # < 3 -> forced fair
        return ValueTag.FAIR, True
    if n_comps < CONFIG["minCompsForVerdict"]:     # 3-4 -> capped to the fair side
        return ValueTag.FAIR, True
    return raw, False


def value_score(verdict: Any) -> float:
    """The 0-10 value component the Mix weights (§5.2d):
    ``clamp(5.0 + (-deltaPct) x slope, 0.5, 9.5)``, 1 dp. Favourable (negative)
    delta scores above 5; adverse below. Reads ``verdict.deltaPct``."""
    delta = _g(verdict, "deltaPct")
    if delta is None:
        raise ValueError("value_score needs a verdict with deltaPct set")
    lo, hi = CONFIG["valueScoreClamp"]
    raw = 5.0 + (-float(delta)) * CONFIG["valueScoreSlopePerPct"]
    return _round(_clamp(raw, lo, hi), 1)


def _widen_band(band: ValueBand, estimate: int, conf_scalar: float) -> ValueBand:
    """Widen the raw IQR band by ``(1 - valueConfidence)`` (§5.2b): the below/above
    half-widths grow by a factor ``1 + (1 - conf)`` around the estimate, so a
    thin/scattered comp set yields a wider, honester band."""
    factor = 1.0 + (1.0 - conf_scalar)
    below = (estimate - band.low) * factor
    above = (band.high - estimate) * factor
    return ValueBand(low=_round_pounds(estimate - below),
                     high=_round_pounds(estimate + above))


def _gauge_position(asking: float, band: ValueBand) -> float:
    """Place ``asking`` on the P2 gauge's fixed 20%-85% reference frame (§5.2d)."""
    g = CONFIG["gauge"]
    lo_pos, hi_pos = g["bandLowPos"], g["bandHighPos"]
    span = band.high - band.low
    if span <= 0:
        return _round((lo_pos + hi_pos) / 2.0, 3)
    pos = lo_pos + (asking - band.low) / span * (hi_pos - lo_pos)
    return _round(_clamp(pos, *g["posClamp"]), 3)


def _comp_ref(c: Any) -> Optional[Ref]:
    cid = _g(c, "transactionId", "id")
    return Ref(id=str(cid), schemaVersion="comp@1") if cid is not None else None


def _comp_addr(c: Any) -> str:
    addr = _g(c, "address")
    if addr is None:
        return "comp"
    if hasattr(addr, "display"):
        try:
            return addr.display()
        except Exception:  # pragma: no cover
            pass
    paon = _g(addr, "paon", default="")
    street = _g(addr, "street", default="")
    return (" ".join(p for p in (str(paon), str(street).title()) if p)).strip() or "comp"


def _adjust_comp(comp: Any, factor: float) -> Any:
    """A copy of ``comp`` with its £/sqft scaled by the UK-HPI ``factor`` (B2). Handles
    dict comps and Comp dataclasses; a factor of 1.0 or a missing £/sqft leaves it as-is."""
    if factor == 1.0:
        return comp
    ppsf = _comp_ppsf(comp)
    if ppsf is None:
        return comp
    adj = _round(ppsf * factor, 1)
    if isinstance(comp, dict):
        c = dict(comp)
        c["pricePerSqft"] = adj
        c["hpiFactor"] = _round(factor, 3)
        return c
    import dataclasses as _dc
    try:
        adjusted = _dc.replace(comp, pricePerSqft=adj)
    except Exception:
        return comp
    # dataclasses.replace copies declared fields only, so the PPD transaction
    # category (an instance attribute — see landreg.parse_comp) would silently
    # vanish from every HPI-moved comp, and the unknown-category provenance
    # count would overstate the limitation. Carry it across.
    cat = getattr(comp, "transactionCategory", None)
    if cat is not None:
        adjusted.transactionCategory = cat
    return adjusted


def _time_adjust_anchor(anchor: List[Any], subject: Any) -> Tuple[List[Any], bool]:
    """Nudge each anchor comp's £/sqft from its sale month to today's money via the UK
    HPI for the subject's borough + the comp's property type (B2) — so an old comp is no
    longer quoted in old money, the bias that made a fair listing read 'over'. Comps with
    no HPI data (future-dated / unmapped borough) keep factor 1.0. Returns the adjusted
    comps and whether any were actually moved."""
    region = hpi.region_for(subject)
    out, moved = [], False
    for c in anchor:
        f = hpi.hpi_factor(region, _g(c, "propertyType"), _g(c, "date"))
        if f != 1.0:
            moved = True
        out.append(_adjust_comp(c, f))
    return out, moved


def _category_provenance_bits(pool: List[Any], anchor: List[Any]) -> List[str]:
    """Honest-exclusions basis bits for the PPD transaction-category filter.

    Excluded rows must stay VISIBLE, not vanish: a verdict that quietly
    dropped a repossession looks identical to one that never saw it. So the
    basis carries (a) how many non-standard sales were excluded from the pool,
    and (b) how many ANCHOR comps have an unknown category — the honest
    limitation of a comp set built before the field was captured, where
    "unknown, treated as standard" is an assumption the reader deserves to see.
    """
    bits: List[str] = []
    excluded = sum(1 for c in pool if _comp_category(c) == "additional")
    if excluded:
        bits.append("%d non-standard sale%s excluded (PPD additional price paid:"
                    " repossession / power-of-sale / non-private transfer)"
                    % (excluded, "s" if excluded != 1 else ""))
    unknown = sum(1 for c in anchor if _comp_category(c) is None)
    if unknown:
        bits.append("transaction category unknown on %d of %d anchor comp%s"
                    " (treated as standard)"
                    % (unknown, len(anchor), "s" if len(anchor) != 1 else ""))
    return bits


def _needs_data_verdict(reason: str) -> ValueVerdict:
    """A soft, schema-valid "can't price this yet" verdict — the honest empty-state
    (P1) returned instead of raising when the subject has no floor area, no asking
    price, or no qualifying comps. Carries no numbers, a NEEDS_DATA tag, zero
    confidence, and ``reason`` as its single plain-English line. The engine drops a
    NEEDS_DATA verdict from the Mix rather than letting it crash or zero the score."""
    v = ValueVerdict(
        score=None, tag=ValueTag.NEEDS_DATA, deltaPct=None, headlineDeltaPct=None,
        fairEstimate=None, band=None, position=None, streetMedianPerSqft=None,
        basis=reason, evidence=[], confidence=0.0)
    v.reasons = [reason]
    return v


def _no_comps_verdict(comps: List[Any], subject: Any,
                      no_sqft: bool = False) -> ValueVerdict:
    """The honest NEEDS_DATA verdict when no comp qualifies (§5.2a honesty).

    Two truthfully different situations were previously collapsed into one
    "no comparable sales nearby" line, which is FALSE when cached sales exist
    but none qualify (e.g. every cached comp is a flat and the subject is a
    house, or the same-class sales carry no EPC floor area). Say which it is,
    count the causes, and name what would unlock a verdict.
    """
    if not comps:
        if no_sqft:
            return _needs_data_verdict(
                "No floor area on the listing and no comparable sales nearby yet — "
                "nothing to price this against.")
        return _needs_data_verdict(
            "No comparable sales nearby yet — nothing solid to price this against.")

    sg = _ptype_group(subject_ptype(subject))
    n = len(comps)
    non_standard = wrong_class = no_area = no_price = 0
    for c in comps:
        cg = _ptype_group(_comp_ptype(c))
        if _comp_category(c) == "additional":
            # Checked first: a repossession of the right class is still
            # excluded, and must be attributed to the category filter rather
            # than mislabelled as a class or data problem.
            non_standard += 1
        elif sg is not None and cg is not None and cg != sg:
            wrong_class += 1
        elif no_sqft and ((p := _comp_price(c)) is None or p <= 0):
            # Mirror qualifies_llf: a zero/negative sold price is just as
            # unusable as a missing one, and must be attributed to a cause
            # rather than falling through to the generic fallback clause.
            no_price += 1
        elif not no_sqft and _comp_ppsf(c) is None:
            no_area += 1

    subj_word = (subject_ptype(subject) or "subject").replace("_", " ").replace("-", " ")
    causes: List[str] = []
    if non_standard:
        causes.append("%d %s non-standard (additional price paid — repossession"
                      " / power-of-sale / non-private transfer), excluded from"
                      " fair estimates"
                      % (non_standard, "are" if non_standard != 1 else "is"))
    if wrong_class:
        causes.append("%d %s the wrong property class for this %s"
                      % (wrong_class, "are" if wrong_class != 1 else "is", subj_word))
    if no_area:
        causes.append("%d carr%s no floor area, so no £/sqft"
                      % (no_area, "y" if no_area != 1 else "ies"))
    if no_price:
        causes.append("%d carr%s no sold price"
                      % (no_price, "y" if no_price != 1 else "ies"))
    if no_sqft:
        unlock = "A same-class sale nearby would unlock a like-for-like verdict."
    elif no_area:
        # Reaching here with area-less comps means their sold prices could not
        # drive the like-for-like fallback either (value_verdict routes a
        # with-sqft subject there automatically now — the §5.2b inversion fix).
        unlock = "A same-class sale with a floor area would unlock a £/sqft verdict."
    else:
        unlock = "A same-class sale with a floor area would unlock a verdict."
    reason = ("%d sale%s exist%s on or near this street, but none are comparable — %s. %s"
              % (n, "s" if n != 1 else "", "" if n != 1 else "s",
                 "; ".join(causes) or "none match this property's class or carry usable data",
                 unlock))
    return _needs_data_verdict(reason)


def _llf_verdict(subject: Any, comps: List[Any], ask: int,
                 sqft_known: bool = False) -> ValueVerdict:
    """The like-for-like fallback (§5.2b): ``Fair_llf`` from SOLD PRICES.

    The spec promises a fair estimate that "is always available": when the
    subject has no floor area, blend weight goes 0.0/1.0 to the like-for-like
    price — the central sold price of the qualifying (same-class) comps,
    HPI-adjusted to today's money. Following the codebase's arithmetic idiom
    (median-of-anchor rather than the spec's explicit weights, cf.
    :func:`fair_estimate`), the estimate is the median adjusted sold price and
    the raw band its inter-quartile spread. It is a coarser read than £/sqft —
    it cannot correct for the subject's size — so confidence is dampened one
    level (:func:`_dampen_one_level`) and the basis says plainly why the read
    is whole-price.

    ``sqft_known`` marks the §5.2b inversion case: the SUBJECT has a floor
    area but no qualifying comp carries one, so a £/sqft read is impossible
    anyway. The arithmetic is identical; only the wording changes (blaming
    "no floor area was given" would be false — the gap is on the comp side).
    """
    anchor, anchor_label = select_anchor(comps, subject, qualifier=qualifies_llf,
                                         trusted=_llf_trusted)
    if not anchor:
        return _no_comps_verdict(comps, subject, no_sqft=not sqft_known)

    # HPI-adjust each sold PRICE to today's money (mirrors _time_adjust_anchor,
    # which scales £/sqft; here the price itself is the estimate's input).
    region = hpi.region_for(subject)
    prices: List[float] = []
    hpi_moved = False
    for c in anchor:
        f = hpi.hpi_factor(region, _g(c, "propertyType"), _g(c, "date"))
        if f != 1.0:
            hpi_moved = True
        prices.append(_comp_price(c) * f)
    prices.sort()

    n = len(prices)
    estimate = _round_pounds(statistics.median(prices))
    if estimate <= 0:
        # HM Land Registry Price Paid carries £1-class anomalous transfer
        # records; a qualifying set whose median rounds to £0 cannot anchor a
        # delta (division by the estimate) — fail soft (A2), never raise.
        return _needs_data_verdict(
            "%d comparable sale%s nearby, but the sold prices are anomalously "
            "low (the median rounds to £0 — likely transfer records, not open-"
            "market sales), so nothing solid to price this against."
            % (n, "s" if n != 1 else ""))
    if n >= 2:
        raw_band = ValueBand(low=_round_pounds(_percentile(prices, 0.25)),
                             high=_round_pounds(_percentile(prices, 0.75)))
    else:
        raw_band = ValueBand(low=estimate, high=estimate)

    conf = value_confidence(anchor, spread_values=prices, spread_label="sold-price")
    conf_scalar = _dampen_one_level(conf["scalar"])   # like-for-like: one level down

    # Adjustments -> effective asking -> honest delta (§5.2c), same as the main path.
    adjs = adjustments(subject)
    signed = sum(a.signed_cost() for a in adjs)
    effective = ask + signed
    headline_delta = _round((ask - estimate) / estimate * 100.0, 1)
    delta = _round((effective - estimate) / estimate * 100.0, 1)

    tag, capped = verdict_tag(delta, n)

    est_mod = (CONFIG["confidence"]["adjustmentEstimate"]
               if any(a.isEstimate for a in adjs) else 1.0)
    verdict_conf = _round(_clamp(conf_scalar * est_mod, 0.0, 1.0), 2)

    band = _widen_band(raw_band, estimate, conf_scalar)
    if band.high <= band.low:
        # A point raw band (single comp, or identical sold prices) defeats
        # _widen_band's multiplicative widening — zero half-widths stay zero —
        # so the (1 - valueConfidence) growth §5.2b promises never happens and
        # the gauge needle pins to the midpoint whatever the ask. Seed a
        # symmetric half-width from the confidence shortfall instead (floored
        # at £1,000 so rounding can never collapse it back to a point).
        hw = max(estimate * (1.0 - conf_scalar), 1000.0)
        band = ValueBand(low=_round_pounds(estimate - hw),
                         high=_round_pounds(estimate + hw))
    position = _gauge_position(ask, band)

    evidence: List[ValueEvidence] = [ValueEvidence(
        kind="comp", label="Anchor median sold price (%s)" % anchor_label,
        value=float(estimate), compRef=_comp_ref(anchor[0]),
        text="median sold price of %d comps: %s" % (n, anchor_label))]
    for c in anchor:
        cp = _comp_price(c)
        evidence.append(ValueEvidence(
            kind="comp",
            label="%s sold £%s" % (_comp_addr(c), "{:,}".format(int(cp or 0))),
            value=_round(cp, 0) if cp is not None else 0.0,
            compRef=_comp_ref(c),
            text="sold price · %s · %s" % (
                _g(c, "date", default="?"),
                str(_g(c, "areaConfidence", default="?") or "?") + " conf")))
    for a in adjs:
        evidence.append(a.to_evidence())

    # WHY is the £/sqft anchor empty? Two truthfully different answers hide
    # behind the inversion case: (a) no comparable sale carries a floor area
    # at all, or (b) area-carrying comparable sales exist but every one is a
    # non-standard transaction the category filter excluded. Saying (a) when
    # (b) is true is a false statement inside an honesty feature — three
    # comparable sales may well carry areas. Count case (b): class-compatible
    # comps WITH a £/sqft whose only disqualifier was the category gate
    # (qualifies has exactly three gates — ppsf, category, class — so with an
    # empty ppsf anchor, category is the only way an area-carrying comparable
    # comp can have been excluded).
    area_excluded = 0
    if sqft_known:
        sg = _ptype_group(subject_ptype(subject))
        for c in comps:
            if _comp_ppsf(c) is None or _comp_category(c) != "additional":
                continue
            cg = _ptype_group(_comp_ptype(c))
            if sg is not None and cg is not None and sg != cg:
                continue
            area_excluded += 1

    if sqft_known and area_excluded:
        why_llf = ("like-for-like price comparison — the listing has a floor "
                   "area, but the only comparable sale%s carrying one %s "
                   "non-standard transaction%s (excluded from fair estimates), "
                   "so this prices against whole sold prices, not £/sqft"
                   % (("s", "are", "s") if area_excluded != 1
                      else ("", "is a", "")))
    elif sqft_known:
        why_llf = ("like-for-like price comparison — the listing has a floor "
                   "area but no comparable sale carries one, so this prices "
                   "against whole sold prices, not £/sqft")
    else:
        why_llf = ("like-for-like price comparison — no floor area was given, "
                   "so this prices against whole sold prices, not £/sqft")
    basis_bits = [
        why_llf,
        "£%s median sold price across %d %s" % ("{:,}".format(estimate), n, anchor_label),
        "confidence dampened one level (no size correction possible)",
    ]
    if hpi_moved:
        basis_bits.append("prices time-adjusted to %s money (UK HPI, %s)"
                          % (hpi.AS_OF_MONTH, hpi.region_for(subject)))
    if adjs:
        basis_bits.append("lease-adjusted")
    if capped:
        basis_bits.append("comp-sufficiency gate: %d<%d capped to fair"
                          % (n, CONFIG["minCompsForVerdict"]))
    basis_bits.extend(_category_provenance_bits(comps, anchor))
    basis = "; ".join(basis_bits)

    verdict = ValueVerdict(
        score=None, tag=tag, deltaPct=delta, headlineDeltaPct=headline_delta,
        fairEstimate=estimate, band=band, position=position,
        streetMedianPerSqft=None,   # honestly absent: there is no £/sqft here
        basis=basis, evidence=evidence, confidence=verdict_conf)
    verdict.score = value_score(verdict)

    if sqft_known and area_excluded:
        opening = ("The only comparable sale%s with %sfloor area%s %s "
                   "non-standard transaction%s (excluded), so this is a "
                   "like-for-like read"
                   % (("s", "", "s", "are", "s") if area_excluded != 1
                      else ("", "a ", "", "is a", "")))
    elif sqft_known:
        opening = "No comparable sale carries a floor area, so this is a like-for-like read"
    else:
        opening = "No floor area given, so this is a like-for-like read"
    lines = [
        "%s: asking £%s vs a £%s "
        "median sold price across %d %s — %.0f%% %s on headline."
        % (opening, "{:,}".format(ask), "{:,}".format(estimate), n,
           anchor_label.split(" (")[0], abs(headline_delta),
           "under" if headline_delta < 0 else "over"),
    ]
    lease = next((a for a in adjs if a.kind == "lease_adj"), None)
    if lease is not None:
        lines.append("The %s (est. £%s) pulls the headline %.1f%% to an adjusted %.1f%%."
                     % (lease.label, "{:,}".format(_round_pounds(lease.amountGBP)),
                        headline_delta, delta))
    elif capped:
        lines.append("Only %d comps (want %d) — the tag is capped to fair until the "
                     "street returns more sales."
                     % (n, CONFIG["minCompsForVerdict"]))
    if sqft_known and area_excluded:
        # More non-standard sales would not help — say what actually would.
        unlock = "an open-market sale with a floor area would unlock £/sqft"
    elif sqft_known:
        unlock = "a comparable sale with a floor area would unlock £/sqft"
    else:
        unlock = "add a floor area for a £/sqft verdict"
    lines.append("%s confidence (dampened one level: a whole-price comparison can't "
                 "correct for size — %s)."
                 % (_confidence_band(verdict_conf).capitalize(), unlock))
    verdict.reasons = lines[:3]
    return verdict


def value_verdict(subject: Any, comps: List[Any]) -> ValueVerdict:
    """THE Value Verdict (§5.2) — select comps -> estimate -> adjust -> derive.

    Produces a schema-valid :class:`ValueVerdict` with the honest
    ``headlineDeltaPct`` -> ``deltaPct`` story, the gauge fields (``fairEstimate``,
    ``band``, ``position``, ``streetMedianPerSqft``), the ``steal|fair|over`` tag
    (comp-sufficiency-gated), the 0-10 ``score`` and comp-driven ``confidence``.
    Sourced ``evidence[]`` cites the actual anchor comps + the lease Adjustment;
    1-3 plain-English reason lines are attached as ``verdict.reasons`` (a
    convenience list — reasons live on ``score.result`` in the full pipeline).
    """
    sqft = subject_sqft(subject)
    ask = subject_ask(subject)
    # Fail soft, never raise (A2): a real listing missing an asking price or any
    # usable comps returns a NEEDS_DATA verdict — the honest empty-state — so
    # score() stays up on thin real stock instead of throwing. A missing floor
    # area alone is NOT an empty-state: §5.2b's like-for-like fallback prices it
    # against whole sold prices instead.
    if ask is None:
        return _needs_data_verdict("No asking price on the listing — can't judge value without one.")
    if sqft is None or sqft <= 0:
        # §5.2b like-for-like fallback: no floor area does NOT mean no verdict —
        # price against qualifying comps' whole sold prices, dampened + labelled.
        return _llf_verdict(subject, comps, ask)

    anchor, anchor_label = select_anchor(comps, subject)
    if not anchor:
        # The §5.2b INVERSION fix: a subject WITH a floor area whose comps all
        # lack one used to dead-end in NEEDS_DATA while withholding the sqft
        # produced a like-for-like verdict — supplying MORE data must never
        # yield LESS verdict. Route the case to the same fallback, worded for
        # the comp-side gap. (The spec's 0.6/0.4 ppsf+llf blend when BOTH
        # sources exist remains unimplemented — it would move the golden
        # numbers; see the 03-engine §5.2b implementation note.)
        if any(qualifies_llf(c, subject) for c in comps):
            return _llf_verdict(subject, comps, ask, sqft_known=True)
        return _no_comps_verdict(comps, subject)

    # B2: nudge each comp's £/sqft to today's money (UK HPI, per borough + property type)
    # so an old comp isn't quoted in old money. Downstream estimate/spread/evidence all
    # use the adjusted anchor.
    anchor, hpi_moved = _time_adjust_anchor(anchor, subject)
    fe = fair_estimate(anchor, sqft)
    estimate, raw_band = fe["estimate"], fe["band"]
    ppsf_median, n = fe["ppsfMedian"], fe["n"]

    conf = value_confidence(anchor)
    conf_scalar = conf["scalar"]

    # Sqft basis flag: the subject's £/sqft denominator is the STATED
    # (marketing) sqft — the property as currently marketed — but when a
    # caller also supplies an EPC-derived area and the two disagree beyond
    # tolerance, the denominator is contested and the verdict must say so and
    # trust itself less, not look rigorous over incompatible measurements.
    sq_basis = epc.sqft_basis_check(sqft, subject_epc_sqft(subject))
    sq_conflict = bool(sq_basis and sq_basis["conflict"])
    if sq_conflict:
        conf["missing"].append(
            "sqft basis conflict: marketing %s sqft vs EPC %s sqft"
            % ("{:,.0f}".format(sq_basis["statedSqft"]),
               "{:,.0f}".format(sq_basis["epcSqft"])))

    # Adjustments -> effective asking -> honest delta (§5.2c).
    adjs = adjustments(subject)
    signed = sum(a.signed_cost() for a in adjs)
    effective = ask + signed
    headline_delta = _round((ask - estimate) / estimate * 100.0, 1)
    delta = _round((effective - estimate) / estimate * 100.0, 1)

    # Tag (comp-sufficiency gated) + value component (§5.2d).
    tag, capped = verdict_tag(delta, n)

    # Verdict confidence: comp-driven scalar x adjustment-estimate modifier
    # (§5.8) x the sqft-basis-conflict modifier (a contested denominator).
    est_mod = (CONFIG["confidence"]["adjustmentEstimate"]
               if any(a.isEstimate for a in adjs) else 1.0)
    basis_mod = (CONFIG["confidence"]["sqftBasisConflict"] if sq_conflict else 1.0)
    verdict_conf = _round(_clamp(conf_scalar * est_mod * basis_mod, 0.0, 1.0), 2)

    band = _widen_band(raw_band, estimate, conf_scalar)
    position = _gauge_position(ask, band)

    listing_ppsf = _round(ask / sqft, 1) if sqft else None

    # Evidence (§5.7 rule 3: comp items carry compRef; adjustments carry £+text).
    evidence: List[ValueEvidence] = []
    if listing_ppsf is not None:
        evidence.append(ValueEvidence(kind="ppsf", label="This listing £/sqft",
                                      value=listing_ppsf))
    evidence.append(ValueEvidence(
        kind="comp", label="Anchor median £/sqft (%s)" % anchor_label, value=ppsf_median,
        compRef=_comp_ref(anchor[0]),
        text="median of %d comps: %s" % (n, anchor_label)))
    for c in anchor:
        cp = _comp_ppsf(c)
        evidence.append(ValueEvidence(
            kind="comp",
            label="%s sold £%s" % (_comp_addr(c), "{:,}".format(int(_g(c, "price", default=0) or 0))),
            value=_round(cp, 1) if cp is not None else 0.0,
            compRef=_comp_ref(c),
            text="£%s/sqft · %s · %s" % (
                "{:,.0f}".format(cp) if cp is not None else "?",
                _g(c, "date", default="?"),
                _g(c, "areaConfidence", default="?") + " conf")))
    for a in adjs:
        evidence.append(a.to_evidence())

    basis_bits = [
        "%d %s" % (n, anchor_label),
        "£%s/sqft median x %s sqft" % ("{:,.0f}".format(ppsf_median),
                                       "{:,}".format(int(sqft)) if sqft else "?"),
    ]
    if hpi_moved:
        basis_bits.append("£/sqft time-adjusted to %s money (UK HPI, %s)"
                          % (hpi.AS_OF_MONTH, hpi.region_for(subject)))
    if adjs:
        basis_bits.append("lease-adjusted")
    if capped:
        basis_bits.append("comp-sufficiency gate: %d<%d capped to fair"
                          % (n, CONFIG["minCompsForVerdict"]))
    if sq_conflict:
        basis_bits.append(
            "sqft basis conflict: marketing %s sqft vs EPC %s sqft (%.0f%% apart)"
            " — priced on the marketing figure; the EPC area may predate the"
            " current configuration"
            % ("{:,.0f}".format(sq_basis["statedSqft"]),
               "{:,.0f}".format(sq_basis["epcSqft"]), sq_basis["diffPct"]))
    basis_bits.extend(_category_provenance_bits(comps, anchor))
    basis = "; ".join(basis_bits)

    verdict = ValueVerdict(
        score=None,  # set below via value_score once deltaPct is present
        tag=tag,
        deltaPct=delta,
        headlineDeltaPct=headline_delta,
        fairEstimate=estimate,
        band=band,
        position=position,
        streetMedianPerSqft=ppsf_median,
        basis=basis,
        evidence=evidence,
        confidence=verdict_conf,
    )
    verdict.score = value_score(verdict)

    verdict.reasons = _reason_lines(subject, anchor, anchor_label, verdict, conf,
                                    adjs, capped, listing_ppsf, ppsf_median)
    return verdict


def _reason_lines(subject: Any, anchor: List[Any], anchor_label: str,
                  verdict: ValueVerdict, conf: Dict[str, Any], adjs: List[Adjustment],
                  capped: bool, listing_ppsf: Optional[float],
                  ppsf_median: float) -> List[str]:
    """1-3 honest, plain-English reason lines (design mandate EVIDENCE)."""
    lines: List[str] = []
    n = len(anchor)
    if listing_ppsf is not None:
        lines.append(
            "£%s/sqft here vs a £%s/sqft median on %d %s — about %.0f%% %s the street on headline."
            % ("{:,.0f}".format(listing_ppsf), "{:,.0f}".format(ppsf_median), n,
               anchor_label.split(" (")[0],
               abs(verdict.headlineDeltaPct),
               "under" if verdict.headlineDeltaPct < 0 else "over"))
    lease = next((a for a in adjs if a.kind == "lease_adj"), None)
    if lease is not None and capped:
        lines.append(
            "The %s and a thin comp set (%d of a wanted %d) trim the apparent %s to a fair verdict."
            % (lease.label, n, CONFIG["minCompsForVerdict"],
               "steal" if verdict.headlineDeltaPct <= CONFIG["stealPct"] else "discount"))
    elif lease is not None:
        lines.append(
            "The %s (est. £%s) pulls the headline %.1f%% to an adjusted %.1f%%."
            % (lease.label, "{:,}".format(_round_pounds(lease.amountGBP)),
               verdict.headlineDeltaPct, verdict.deltaPct))
    elif capped:
        lines.append(
            "Only %d comps (want %d) — the tag is capped to fair until the street returns more sales."
            % (n, CONFIG["minCompsForVerdict"]))
    lines.append(
        "%s confidence: %s." % (_confidence_band(verdict.confidence).capitalize(),
                                 "; ".join(conf["missing"]) if conf["missing"]
                                 else "close, trusted comps with a tight spread"))
    return lines[:3]


# ---------------------------------------------------------------------------
# Comps loader — build Comp instances from data/comps_enriched.json (U9/EPC).
# ---------------------------------------------------------------------------

_COMP_FIELDS = {
    "price", "date", "propertyType", "tenure", "newBuild", "distanceNote",
    "pricePerSqft", "sqft", "epcCertNumber", "epcDate", "epcSaleGapYears",
    "areaChanged", "epcAfterSaleOnly", "areaConfidence", "epcAreaChange",
    "schemaVersion", "source", "sourceDate", "transactionId",
}


def _comp_from_dict(d: Dict[str, Any]) -> Comp:
    kwargs = {k: v for k, v in d.items() if k in _COMP_FIELDS}
    addr = d.get("address")
    if isinstance(addr, dict):
        kwargs["address"] = CompAddress(**{k: addr.get(k) for k in (
            "paon", "saon", "street", "postcode", "district", "town", "county")})
    comp = Comp(**kwargs)
    # The PPD transaction category rides as an instance attribute (the schema
    # Comp field is owned by another workstream; landreg.parse_comp sets it the
    # same way). An enriched file that carries it must not lose it on load —
    # and one built before the capture simply reads "unknown", which the
    # qualifiers treat as standard but count in provenance. Normalised on the
    # way in (same rule as _comp_category) so a file carrying the register's
    # full label loads as the canonical two-word vocabulary, not a string the
    # filter would silently miss.
    cat = d.get("transactionCategory")
    if cat is not None:
        comp.transactionCategory = _normalise_category(cat)
    return comp


def load_enriched_comps(path: str = DEFAULT_COMPS_PATH,
                        matched_only: bool = True) -> List[Comp]:
    """Load ``data/comps_enriched.json`` into :class:`Comp` instances.

    ``matched_only`` keeps only comps that carry a ``pricePerSqft`` (the EPC
    match succeeded) — the set the value scorer can actually use.
    """
    with open(path) as f:
        blob = json.load(f)
    raw = blob.get("comps", blob) if isinstance(blob, dict) else blob
    comps = [_comp_from_dict(c) for c in raw]
    if matched_only:
        comps = [c for c in comps if c.pricePerSqft is not None]
    return comps


__all__ = [
    "CONFIG", "MIN_ANCHOR", "Adjustment", "subject_epc_sqft",
    "qualifies", "qualifies_llf", "select_anchor", "fair_estimate",
    "lease_extension_cost", "lease_adjustment", "adjustments",
    "value_confidence", "verdict_tag", "value_score", "value_verdict",
    "load_enriched_comps", "DEFAULT_COMPS_PATH",
]
